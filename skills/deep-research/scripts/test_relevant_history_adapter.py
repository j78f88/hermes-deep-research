#!/usr/bin/env python3
"""RED-first tests for the ISSUE-572 read-only relevant-history adapter.

The adapter builds a ``prior-work-context-v1`` artifact from injected Linear and
Knowledge base lookup runners. It performs NO live network or mutation: runners are
plain callables supplied by the caller (real ones in production, fakes here).
Requirements under test:

  * stable, deterministic PW ids derived from surface + ref;
  * freshness classification per surface;
  * access-gap classification when a runner reports a blocker;
  * a HOLD (blocked_missing_prior_work_context) when a required surface is
    inaccessible, so a silent gap can never masquerade as "no prior work";
  * the produced context validates through evaluate_prior_work_context.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
ADAPTER_PATH = SCRIPT_DIR / "relevant_history_adapter.py"
LOOP_PATH = SCRIPT_DIR / "prior_work_playbook_loop.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


rha = _load("relevant_history_adapter", ADAPTER_PATH)
plw = _load("prior_work_playbook_loop", LOOP_PATH)


# --------------------------------------------------------------------------- #
# Fake injected runners (no live network / mutation)                          #
# --------------------------------------------------------------------------- #

def fake_linear_runner(query_terms):
    return {
        "status": "ok",
        "checked_at": "2026-09-11T00:00:00+00:00",
        "matches": [
            {
                "ref": "ISSUE-458",
                "title": "Deep research quality harness",
                "relationship": "extend_existing",
                "observed_state": "Done",
                "observed_at": "2026-09-11T00:00:00+00:00",
                "owner": "linear://ISSUE-458",
                "reusable_assets": ["five-case benchmark"],
                "decision_or_gate": "no word-count proxy",
            }
        ],
    }


def fake_knowledge_base_runner(query_terms):
    return {
        "status": "ok",
        "checked_at": "2026-09-11T00:00:00+00:00",
        "matches": [
            {
                "ref": "50_projects/deep-research/decisions.md",
                "title": "Deep research decisions MOC",
                "relationship": "reuse_as_is",
                "observed_state": "current",
                "observed_at": "2026-09-11T00:00:00+00:00",
                "owner": "knowledge_base://deep-research",
                "reusable_assets": ["dual-output contract"],
                "decision_or_gate": "export gets curated outputs only",
            }
        ],
    }


def blocked_runner(query_terms):
    return {"status": "blocked", "checked_at": None, "blocker": "no linear token", "matches": []}


BASE_KW = dict(
    campaign_id="campaign-xyz",
    query_terms=["deep research", "improvement loop"],
    now="2026-09-11T00:00:00+00:00",
)


# --------------------------------------------------------------------------- #
# Happy path                                                                   #
# --------------------------------------------------------------------------- #

def test_scan_builds_valid_prior_work_context():
    ctx = rha.scan_relevant_history(
        linear_runner=fake_linear_runner,
        knowledge_base_runner=fake_knowledge_base_runner,
        **BASE_KW,
    )
    assert ctx["schema_version"] == "prior-work-context-v1"
    # The produced artifact must pass the shared evaluator.
    result = plw.evaluate_prior_work_context(ctx)
    assert result["status"] == "PASS", result["reasons"]
    assert result["state"] == "prior_work_validated"


def test_scan_assigns_stable_deterministic_pw_ids():
    a = rha.scan_relevant_history(
        linear_runner=fake_linear_runner, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW
    )
    b = rha.scan_relevant_history(
        linear_runner=fake_linear_runner, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW
    )
    ids_a = sorted(m["prior_work_id"] for m in a["matches"])
    ids_b = sorted(m["prior_work_id"] for m in b["matches"])
    assert ids_a == ids_b  # deterministic
    assert all(pw.startswith("PW-") for pw in ids_a)
    assert len(set(ids_a)) == len(ids_a)  # unique


def test_scan_pw_id_stable_across_match_order():
    # Same refs in a different discovery order must yield the same ids.
    def reversed_linear(query_terms):
        r = fake_linear_runner(query_terms)
        extra = dict(r["matches"][0])
        extra["ref"] = "ISSUE-520"
        extra["title"] = "Dual output contract"
        r["matches"] = [extra, r["matches"][0]]
        return r

    def reversed_linear2(query_terms):
        r = reversed_linear(query_terms)
        r["matches"] = list(reversed(r["matches"]))
        return r

    a = rha.scan_relevant_history(linear_runner=reversed_linear, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW)
    b = rha.scan_relevant_history(linear_runner=reversed_linear2, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW)
    id_by_ref_a = {m["ref"]: m["prior_work_id"] for m in a["matches"]}
    id_by_ref_b = {m["ref"]: m["prior_work_id"] for m in b["matches"]}
    assert id_by_ref_a == id_by_ref_b


# --------------------------------------------------------------------------- #
# Freshness classification                                                     #
# --------------------------------------------------------------------------- #

def test_scan_classifies_surface_freshness_live_for_linear():
    ctx = rha.scan_relevant_history(
        linear_runner=fake_linear_runner, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW
    )
    by_surface = {s["surface"]: s for s in ctx["surfaces"]}
    assert by_surface["linear"]["freshness"] == "live"
    assert by_surface["knowledge_base"]["freshness"] in {"fresh", "live"}


# --------------------------------------------------------------------------- #
# Access-gap classification -> HOLD                                            #
# --------------------------------------------------------------------------- #

def test_scan_records_access_gap_and_holds_when_required_surface_blocked():
    ctx = rha.scan_relevant_history(
        linear_runner=blocked_runner, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW
    )
    by_surface = {s["surface"]: s for s in ctx["surfaces"]}
    assert by_surface["linear"]["checked"] is False
    assert by_surface["linear"]["status"] == "blocked"
    assert by_surface["linear"]["blocker_or_exclusion"]
    # A material access gap on a required surface must be recorded.
    assert any(g for g in ctx["material_gaps"])
    # And the shared evaluator must HOLD, never silently PASS as "no prior work".
    result = plw.evaluate_prior_work_context(ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_missing_prior_work_context"


# --------------------------------------------------------------------------- #
# No live side effects                                                         #
# --------------------------------------------------------------------------- #

def test_scan_only_calls_injected_runners_read_only():
    calls = []

    def spy_linear(query_terms):
        calls.append(("linear", tuple(query_terms)))
        return fake_linear_runner(query_terms)

    def spy_knowledge_base(query_terms):
        calls.append(("knowledge_base", tuple(query_terms)))
        return fake_knowledge_base_runner(query_terms)

    rha.scan_relevant_history(linear_runner=spy_linear, knowledge_base_runner=spy_knowledge_base, **BASE_KW)
    surfaces_called = {c[0] for c in calls}
    assert surfaces_called == {"linear", "knowledge_base"}


def test_scan_does_not_mutate_query_terms_input():
    terms = ["deep research", "improvement loop"]
    before = copy.deepcopy(terms)
    rha.scan_relevant_history(
        linear_runner=fake_linear_runner,
        knowledge_base_runner=fake_knowledge_base_runner,
        campaign_id="c",
        query_terms=terms,
        now="2026-09-11T00:00:00+00:00",
    )
    assert terms == before


# --------------------------------------------------------------------------- #
# CLI writes an artifact without touching the network                          #
# --------------------------------------------------------------------------- #

def test_cli_writes_context_from_fixture_runners(tmp_path):
    import subprocess
    import sys

    # A fixture-driven CLI mode feeds match dicts from local JSON so the CLI
    # itself performs no network call.
    fixture = {
        "campaign_id": "campaign-xyz",
        "query_terms": ["deep research"],
        "now": "2026-09-11T00:00:00+00:00",
        "linear": fake_linear_runner([]),
        "knowledge_base": fake_knowledge_base_runner([]),
    }
    fx_path = tmp_path / "fixture.json"
    out_path = tmp_path / "prior-work-context.json"
    fx_path.write_text(json.dumps(fixture), encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(ADAPTER_PATH), "--fixture", str(fx_path), "--out", str(out_path)],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ctx = json.loads(out_path.read_text(encoding="utf-8"))
    assert ctx["schema_version"] == "prior-work-context-v1"
    assert plw.evaluate_prior_work_context(ctx)["status"] == "PASS"


# --------------------------------------------------------------------------- #
# Review-driven regressions (ISSUE-572 reviewer fold-in)                         #
# --------------------------------------------------------------------------- #

def test_pw_ids_have_large_collision_resistant_space():
    # PW ids must use a wide digest, not a 24-bit truncation, so distinct refs do
    # not collide in practice.
    ids = set()
    for i in range(5000):
        ids.add(rha._pw_id("linear", f"ISSUE-{i}"))
    assert len(ids) == 5000  # no collisions across many refs
    # And the id encodes far more than 24 bits of digest.
    sample = rha._pw_id("linear", "ISSUE-458")
    assert len(sample) >= len("PW-") + 12


def test_runner_exception_becomes_blocked_surface_not_crash():
    def raising_runner(query_terms):
        raise RuntimeError("no linear token")

    ctx = rha.scan_relevant_history(
        linear_runner=raising_runner, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW
    )
    by_surface = {s["surface"]: s for s in ctx["surfaces"]}
    assert by_surface["linear"]["checked"] is False
    assert by_surface["linear"]["status"] == "blocked"
    assert by_surface["linear"]["blocker_or_exclusion"]
    assert any(g["surface"] == "linear" for g in ctx["material_gaps"])
    result = plw.evaluate_prior_work_context(ctx)
    assert result["status"] == "HOLD"


def test_malformed_match_missing_ref_becomes_blocked_not_crash():
    def malformed_runner(query_terms):
        return {"status": "ok", "checked_at": "2026-09-11T00:00:00+00:00", "matches": [{"title": "no ref here"}]}

    ctx = rha.scan_relevant_history(
        linear_runner=malformed_runner, knowledge_base_runner=fake_knowledge_base_runner, **BASE_KW
    )
    by_surface = {s["surface"]: s for s in ctx["surfaces"]}
    # A malformed match must not crash and must not silently pass as clean.
    assert by_surface["linear"]["status"] == "blocked"
    assert any(g["surface"] == "linear" for g in ctx["material_gaps"])


def test_extra_optional_surfaces_are_supported():
    def sessions_runner(query_terms):
        return {"status": "none_found", "checked_at": "2026-09-11T00:00:00+00:00", "matches": []}

    ctx = rha.scan_relevant_history(
        linear_runner=fake_linear_runner,
        knowledge_base_runner=fake_knowledge_base_runner,
        extra_runners={"sessions": sessions_runner},
        **BASE_KW,
    )
    by_surface = {s["surface"]: s for s in ctx["surfaces"]}
    assert "sessions" in by_surface
    assert by_surface["sessions"]["required"] is False
    # An optional surface that finds nothing does not block.
    assert plw.evaluate_prior_work_context(ctx)["status"] == "PASS"
