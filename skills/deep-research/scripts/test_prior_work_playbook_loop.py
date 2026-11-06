"""Tests for the ISSUE-572 prior-work-aware playbook and improvement loop.

Strict TDD: these tests are written before the implementation module exists.

The module under test is deterministic and offline. It validates and evaluates
three machine artifacts against the ISSUE-569 contracts:

  * prior-work-context-v1
  * implementation-or-operating-playbook-v1
  * research-improvement-record-v1

It performs no live runtime, Linear, export, credential, network, push or merge
work. It only reads dict inputs / local JSON and returns verdicts whose `state`
values come from the shared ISSUE-569 state enum.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
MODULE_PATH = SCRIPT_DIR / "prior_work_playbook_loop.py"


def load_module():
    spec = importlib.util.spec_from_file_location("prior_work_playbook_loop", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plw = load_module()


# --------------------------------------------------------------------------- #
# Fixtures — valid baseline artifacts                                          #
# --------------------------------------------------------------------------- #

def _hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _zero_hash() -> str:
    return "0" * 64


def valid_prior_work_context() -> dict:
    return {
        "schema_version": "prior-work-context-v1",
        "campaign_id": "campaign-abc",
        "query_terms": ["deep research", "improvement loop"],
        "surfaces": [
            {
                "surface": "linear",
                "required": True,
                "checked": True,
                "checked_at": "2026-09-10T00:00:00+00:00",
                "freshness": "live",
                "status": "checked",
                "blocker_or_exclusion": None,
            },
            {
                "surface": "knowledge_base",
                "required": True,
                "checked": True,
                "checked_at": "2026-09-10T00:00:00+00:00",
                "freshness": "fresh",
                "status": "checked",
                "blocker_or_exclusion": None,
            },
        ],
        "matches": [
            {
                "prior_work_id": "PW-001",
                "surface": "linear",
                "ref": "ISSUE-458",
                "title": "Deep research quality harness",
                "relationship": "extend_existing",
                "observed_state": "Done",
                "observed_at": "2026-09-10T00:00:00+00:00",
                "evidence_hash": "a" * 64,
                "owner": "linear://ISSUE-458",
                "reusable_assets": ["five-case benchmark"],
                "decision_or_gate": "no word-count proxy",
                "notes": None,
            }
        ],
        "material_gaps": [],
        "context_to_use": ["five-case benchmark"],
        "context_to_exclude": ["excluded-domain business context"],
        "status": "PASS",
        "generated_at": "2026-09-10T00:00:00+00:00",
    }


def valid_playbook(prior_work_context: dict) -> dict:
    return {
        "schema_version": "implementation-or-operating-playbook-v1",
        "campaign_id": "campaign-abc",
        "playbook_id": "PB-001",
        "target_form": "software_integration",
        "input_bundle_sha256": "b" * 64,
        "prior_work_context_sha256": _hash(prior_work_context),
        "target_outcome": "Integrate the evidence-to-output assurance controller cleanly.",
        "definition_of_done": ["outputs PASS", "reviews PASS"],
        "strategy": "extend",
        "strategy_reason": "Existing ISSUE-569 pipeline owns the route.",
        "stages": [
            {
                "stage_id": "S1",
                "name": "Wire adapter",
                "outcome": "Adapter emits prior-work context",
                "claim_ids": ["CLM-001"],
                "prior_work_ids": ["PW-001"],
                "novelty_or_duplicate_verdict": "extension",
                "owners": ["research"],
                "dependencies": [],
                "affected_systems_or_files": ["scripts/prior_work_playbook_loop.py"],
                "acceptance": ["adapter test passes"],
                "rollback": "revert commit",
                "owner_gate": None,
                "activation_authorized": False,
            }
        ],
        "controls": ["no auto mutation"],
        "tests": ["pytest scripts/test_prior_work_playbook_loop.py"],
        "operations": {
            "runbook": ["run adapter", "run builder"],
            "monitoring": ["review receipts"],
            "support_owner": "research",
            "maintenance_burden": "low",
        },
        "risks": [{"risk": "duplicate work", "mitigation": "prior-work scan", "recovery": "hold"}],
        "improvement_experiment": {
            "baseline": "manual committee synthesis",
            "hypothesis": "playbook reuse reduces rework",
            "measures": ["rework events"],
            "thresholds": ["rework events decrease"],
            "review_trigger": "after approved pilot",
        },
        "owner_gates": ["activation is proposal only"],
        "status": "PASS",
        "promotion_guard": {
            "requires_observed_evidence": True,
            "requires_independent_review": True,
            "automatic_mutation_allowed": False,
        },
    }


def valid_improvement_record() -> dict:
    return {
        "schema_version": "research-improvement-record-v1",
        "campaign_id": "campaign-abc",
        "playbook_id": "PB-001",
        "implementation_or_proof_ids": ["ISSUE-572"],
        "baseline": {
            "description": "manual committee synthesis",
            "evidence_refs": ["receipts/plan-smoke-receipt.json"],
        },
        "hypothesis": "playbook reuse reduces rework across campaigns",
        "expected_measures": [{"measure": "rework events", "threshold": "decrease vs baseline"}],
        "observed_measures": [
            {"measure": "rework events", "value": 1, "evidence_refs": ["receipts/outcome.json"]}
        ],
        "outcome_classification": "confirmed",
        "evidence_refs": ["receipts/outcome.json"],
        "prior_work_effects": [{"prior_work_id": "PW-001", "effect": "confirmed"}],
        "source_quality_implications": [],
        "improvement_proposals": [
            {
                "proposal_id": "IP-001",
                "destination": "skill",
                "change": "document reuse rule",
                "evidence_refs": ["receipts/outcome.json"],
                "no_change_reason": None,
            }
        ],
        "reviewer": "independent-reviewer",
        "owner": "research",
        "next_review_date": "2026-12-01",
        "promotion_status": "proposed",
        "promotion_guard": {
            "observed_evidence_present": True,
            "independent_review_passed": True,
            "automatic_mutation_allowed": False,
        },
    }


# --------------------------------------------------------------------------- #
# State enum                                                                   #
# --------------------------------------------------------------------------- #

def test_state_enum_is_subset_of_stage569_enum():
    # Every state the module can emit must be inside the shared ISSUE-569 enum.
    shared = {
        "blocked_invalid_input",
        "blocked_missing_prior_work_context",
        "blocked_duplicate_prior_work",
        "blocked_unverified_improvement",
        "blocked_owner_gate",
        "prior_work_validated",
        "owner_review_ready",
        "outcome_review_due",
        "improvement_reviewed",
    }
    assert plw.EMITTABLE_STATES.issubset(shared)


# --------------------------------------------------------------------------- #
# Prior-work adapter                                                           #
# --------------------------------------------------------------------------- #

def test_prior_work_valid_context_passes():
    result = plw.evaluate_prior_work_context(valid_prior_work_context())
    assert result["status"] == "PASS"
    assert result["state"] == "prior_work_validated"
    assert result["reasons"] == []


def test_prior_work_holds_when_linear_surface_missing():
    ctx = valid_prior_work_context()
    ctx["surfaces"] = [s for s in ctx["surfaces"] if s["surface"] != "linear"]
    result = plw.evaluate_prior_work_context(ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_missing_prior_work_context"
    assert any("linear" in r for r in result["reasons"])


def test_prior_work_holds_when_knowledge_base_present_but_unchecked():
    ctx = valid_prior_work_context()
    for s in ctx["surfaces"]:
        if s["surface"] == "knowledge_base":
            s["checked"] = False
            s["status"] = "blocked"
    result = plw.evaluate_prior_work_context(ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_missing_prior_work_context"
    assert any("knowledge_base" in r for r in result["reasons"])


def test_prior_work_fails_on_schema_violation():
    ctx = valid_prior_work_context()
    del ctx["campaign_id"]
    result = plw.evaluate_prior_work_context(ctx)
    assert result["status"] == "FAIL"
    assert result["state"] == "blocked_invalid_input"


def test_prior_work_reports_duplicate_do_not_build_matches():
    ctx = valid_prior_work_context()
    ctx["matches"][0]["relationship"] = "duplicate_do_not_build"
    result = plw.evaluate_prior_work_context(ctx)
    # The scan itself still validates; duplicates are surfaced for the builder gate.
    assert result["status"] == "PASS"
    assert "PW-001" in result["duplicate_prior_work_ids"]


# --------------------------------------------------------------------------- #
# Playbook builder / evaluator                                                #
# --------------------------------------------------------------------------- #

def test_playbook_valid_passes():
    ctx = valid_prior_work_context()
    result = plw.evaluate_playbook(valid_playbook(ctx), ctx)
    assert result["status"] == "PASS"
    assert result["state"] == "owner_review_ready"
    assert result["reasons"] == []


def test_playbook_holds_when_prior_work_context_missing_required_surface():
    ctx = valid_prior_work_context()
    ctx["surfaces"] = [s for s in ctx["surfaces"] if s["surface"] != "knowledge_base"]
    pb = valid_playbook(ctx)  # hash still binds to the (now-invalid) context
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_missing_prior_work_context"


def test_playbook_holds_when_context_hash_mismatch():
    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    pb["prior_work_context_sha256"] = _zero_hash()
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_missing_prior_work_context"
    assert any("hash" in r.lower() for r in result["reasons"])


def test_playbook_blocks_when_stage_builds_duplicate_prior_work():
    ctx = valid_prior_work_context()
    ctx["matches"][0]["relationship"] = "duplicate_do_not_build"
    pb = valid_playbook(ctx)
    pb["prior_work_context_sha256"] = _hash(ctx)
    # stage references the duplicate prior work as a fresh build
    pb["stages"][0]["prior_work_ids"] = ["PW-001"]
    pb["stages"][0]["novelty_or_duplicate_verdict"] = "new"
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_duplicate_prior_work"


def test_playbook_blocks_unapproved_activation():
    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    # Schema pins activation_authorized to const false, so simulate an owner-gate
    # violation via the promotion guard instead.
    pb["promotion_guard"]["automatic_mutation_allowed"] = True
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] in {"HOLD", "FAIL"}
    assert result["state"] in {"blocked_owner_gate", "blocked_invalid_input"}


def test_playbook_stage_must_cite_prior_work_ids_that_exist():
    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    pb["stages"][0]["prior_work_ids"] = ["PW-999"]  # not in context
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "HOLD"
    assert any("PW-999" in r for r in result["reasons"])


def test_playbook_fails_on_schema_violation():
    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    del pb["improvement_experiment"]
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "FAIL"
    assert result["state"] == "blocked_invalid_input"


# --- ISSUE-572 reviewer fold-in: non-dict inputs are rejected explicitly ------- #

def test_playbook_rejects_non_dict_input():
    result = plw.evaluate_playbook(["not", "a", "dict"], valid_prior_work_context())
    assert result["status"] == "FAIL"
    assert result["state"] == "blocked_invalid_input"


def test_improvement_record_rejects_non_dict_input():
    result = plw.evaluate_improvement_record("not a dict")
    assert result["status"] == "FAIL"
    assert result["state"] == "blocked_invalid_input"


# --- ISSUE-572: every material stage must cite >=1 CLM claim id and >=1 PW id --- #

def test_playbook_holds_when_material_stage_cites_no_claim_ids():
    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    # Material stage (default) with prior-work ids but no claim ids must HOLD.
    pb["stages"][0]["claim_ids"] = []
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_missing_prior_work_context"
    assert any("claim" in r.lower() and "S1" in r for r in result["reasons"])


def test_playbook_holds_when_material_stage_cites_no_prior_work_ids():
    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    pb["stages"][0]["prior_work_ids"] = []
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_missing_prior_work_context"
    assert any("prior-work" in r.lower() and "S1" in r for r in result["reasons"])


def test_playbook_allows_non_material_stage_without_citations():
    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    # An explicitly non-material stage (e.g. pure documentation) is exempt from
    # the claim/prior-work citation requirement.
    pb["stages"].append(
        {
            "stage_id": "S2",
            "name": "Write closeout note",
            "outcome": "Human-readable summary exists",
            "claim_ids": [],
            "prior_work_ids": [],
            "material": False,
            "owners": ["research"],
            "dependencies": [],
            "acceptance": ["note exists"],
            "rollback": "delete note",
            "owner_gate": None,
            "activation_authorized": False,
        }
    )
    result = plw.evaluate_playbook(pb, ctx)
    assert result["status"] == "PASS"
    assert result["state"] == "owner_review_ready"


# --------------------------------------------------------------------------- #
# Improvement-record evaluator                                                #
# --------------------------------------------------------------------------- #

def test_improvement_record_confirmed_with_evidence_passes():
    result = plw.evaluate_improvement_record(valid_improvement_record())
    assert result["status"] == "PASS"
    assert result["state"] == "improvement_reviewed"
    assert result["reasons"] == []


def test_improvement_record_blocks_improved_claim_without_observed_evidence():
    rec = valid_improvement_record()
    rec["observed_measures"] = []
    rec["outcome_classification"] = "confirmed"  # cannot be confirmed with no evidence
    rec["promotion_guard"]["observed_evidence_present"] = False
    result = plw.evaluate_improvement_record(rec)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_unverified_improvement"


def test_improvement_record_no_evidence_must_be_not_yet_measurable():
    rec = valid_improvement_record()
    rec["observed_measures"] = []
    rec["outcome_classification"] = "not_yet_measurable"
    rec["promotion_guard"]["observed_evidence_present"] = False
    rec["promotion_status"] = "no_change"
    result = plw.evaluate_improvement_record(rec)
    assert result["status"] == "PASS"
    assert result["state"] == "outcome_review_due"


def test_improvement_record_refuted_requires_correction_and_preserved_approach():
    rec = valid_improvement_record()
    rec["outcome_classification"] = "refuted"
    # no correction proposal and no preserved failed approach -> blocked
    rec["improvement_proposals"] = []
    result = plw.evaluate_improvement_record(rec)
    assert result["status"] == "HOLD"
    assert any("correction" in r.lower() or "preserve" in r.lower() for r in result["reasons"])


def test_improvement_record_refuted_with_correction_passes():
    rec = valid_improvement_record()
    rec["outcome_classification"] = "refuted"
    rec["improvement_proposals"] = [
        {
            "proposal_id": "IP-002",
            "destination": "process",
            "change": "correction: preserve failed approach and revise hypothesis",
            "evidence_refs": ["receipts/outcome.json"],
            "no_change_reason": None,
        }
    ]
    rec["prior_work_effects"] = [{"prior_work_id": "PW-001", "effect": "contradicted"}]
    result = plw.evaluate_improvement_record(rec)
    assert result["status"] == "PASS"
    assert result["state"] == "improvement_reviewed"


def test_improvement_record_blocks_automatic_mutation_owner_gate():
    rec = valid_improvement_record()
    rec["promotion_status"] = "approved"
    rec["promotion_guard"]["independent_review_passed"] = False
    result = plw.evaluate_improvement_record(rec)
    assert result["status"] == "HOLD"
    assert result["state"] == "blocked_owner_gate"


def test_improvement_record_fails_on_schema_violation():
    rec = valid_improvement_record()
    del rec["reviewer"]
    result = plw.evaluate_improvement_record(rec)
    assert result["status"] == "FAIL"
    assert result["state"] == "blocked_invalid_input"


# --------------------------------------------------------------------------- #
# Determinism + no side effects                                               #
# --------------------------------------------------------------------------- #

def test_evaluations_are_pure_and_do_not_mutate_inputs():
    ctx = valid_prior_work_context()
    ctx_before = copy.deepcopy(ctx)
    plw.evaluate_prior_work_context(ctx)
    assert ctx == ctx_before

    pb = valid_playbook(ctx)
    pb_before = copy.deepcopy(pb)
    plw.evaluate_playbook(pb, ctx)
    assert pb == pb_before

    rec = valid_improvement_record()
    rec_before = copy.deepcopy(rec)
    plw.evaluate_improvement_record(rec)
    assert rec == rec_before


def test_evaluations_are_deterministic():
    ctx = valid_prior_work_context()
    a = plw.evaluate_prior_work_context(ctx)
    b = plw.evaluate_prior_work_context(ctx)
    assert a == b


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def test_cli_evaluates_all_three_artifacts_and_writes_receipt(tmp_path):
    import subprocess
    import sys

    ctx = valid_prior_work_context()
    pb = valid_playbook(ctx)
    rec = valid_improvement_record()
    ctx_path = tmp_path / "prior-work-context.json"
    pb_path = tmp_path / "playbook.json"
    rec_path = tmp_path / "improvement-record.json"
    out_path = tmp_path / "loop-receipt.json"
    ctx_path.write_text(json.dumps(ctx), encoding="utf-8")
    pb_path.write_text(json.dumps(pb), encoding="utf-8")
    rec_path.write_text(json.dumps(rec), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--prior-work-context",
            str(ctx_path),
            "--playbook",
            str(pb_path),
            "--improvement-record",
            str(rec_path),
            "--out",
            str(out_path),
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(out_path.read_text(encoding="utf-8"))
    assert receipt["status"] == "PASS"
    assert receipt["prior_work"]["state"] == "prior_work_validated"
    assert receipt["playbook"]["state"] == "owner_review_ready"
    assert receipt["improvement_record"]["state"] == "improvement_reviewed"


def test_cli_returns_nonzero_when_blocked(tmp_path):
    import subprocess
    import sys

    ctx = valid_prior_work_context()
    ctx["surfaces"] = [s for s in ctx["surfaces"] if s["surface"] != "linear"]
    ctx_path = tmp_path / "prior-work-context.json"
    out_path = tmp_path / "loop-receipt.json"
    ctx_path.write_text(json.dumps(ctx), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--prior-work-context",
            str(ctx_path),
            "--out",
            str(out_path),
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode != 0
    receipt = json.loads(out_path.read_text(encoding="utf-8"))
    assert receipt["prior_work"]["state"] == "blocked_missing_prior_work_context"
