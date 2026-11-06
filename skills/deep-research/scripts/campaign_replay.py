#!/usr/bin/env python3
"""fourteen-fixture behavioral replay matrix.

A single structural evaluator composes the evidence-bundle, run-state-machine,
reviewer-receipt and source-assessment modules and returns a terminal/blocked
state from the versioned enum for a given *structured campaign state*.

The evaluator inspects ONLY the ``structure`` of a fixture. It never reads the
``name``, ``label`` or ``description`` fields — those are human documentation and
must not drive verdicts (enforced by the label-scramble control test). Faults are
injected structurally: a wrong ``input_bundle_sha256``, a placeholder sentinel in
canonical output bytes, a ``ProviderAuthError`` runner, three same-lineage
sources with inflated independence, a same-correlation-id resume, an activation
proposal with a write mutation class, a missing prior-work surface, a duplicate
prior-work id, an improvement record with no observed evidence, an
automatic-mutation flag, and so on.

Ordering: the evaluator applies checks in the same order the plan's controller
stages run, so the FIRST structural failure determines the terminal state.

Pure and offline: no network, provider, credential, or live-runtime access.
All dependencies are bundled siblings. Source-assessment lifecycle projection
uses the local source_ledger module; no external checkout is needed.
The former ledger_path keywords are no-I/O compatibility arguments only.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import copy
import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent

_PLACEHOLDER_SENTINELS = ("\x00PLACEHOLDER\x00",)  # structural, not a keyword


def _load_sibling(name: str):
    if name in sys.modules and getattr(sys.modules[name], "__file__", "").startswith(str(_HERE)):
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


eb = _load_sibling("evidence_bundle")
rsm = _load_sibling("run_state_machine")
rc = _load_sibling("reviewer_receipts")
sa = _load_sibling("source_assessment")


class ProviderAuthError(RuntimeError):
    """Structural provider-auth failure (401/403) raised by a runner."""


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Structural checks (order = controller stage order)                          #
# --------------------------------------------------------------------------- #

def _check_provider(structure: dict) -> tuple[str, list[str]] | None:
    """A provider runner that raises ProviderAuthError -> blocked_provider_auth;
    a timeout with a partial artifact -> repair_required."""
    provider = structure.get("provider")
    if not provider:
        return None
    runner = provider.get("runner")
    if runner is not None:
        try:
            runner()
        except ProviderAuthError as exc:
            return "blocked_provider_auth", [f"provider auth failure: {exc}"]
        except Exception as exc:  # noqa: BLE001
            return "blocked_exhausted_retry", [f"provider error: {exc}"]
    if provider.get("terminal_reason") == "timeout" and provider.get("partial_artifact"):
        return "repair_required", ["provider timeout left a partial artifact; do not promote"]
    if provider.get("terminal_reason") in {"http_401", "http_403"}:
        return "blocked_provider_auth", ["provider returned 401/403"]
    return None


def _check_evidence(structure: dict) -> tuple[str, list[str]] | None:
    """Claim evidence refs that do not resolve to a manifest artifact ->
    blocked_missing_evidence."""
    bundle = structure.get("bundle")
    if not bundle:
        return None
    manifest_paths = {a.get("path") for a in bundle.get("artifact_manifest", [])}
    for claim in bundle.get("claims", []):
        for ref in claim.get("evidence_refs", []):
            if ref not in manifest_paths:
                return "blocked_missing_evidence", [
                    f"claim {claim.get('claim_id')} evidence {ref!r} does not resolve"
                ]
    return None


def _check_lineage(structure: dict) -> tuple[str, list[str]] | None:
    """Three same-lineage sources presented as independent -> blocked_lineage_inflation.

    Independence is computed from lineage groups, never trusted from a declared
    field: N sources sharing one lineage group count as ONE independent origin.
    """
    bundle = structure.get("bundle")
    if not bundle:
        return None
    envs = bundle.get("source_envelopes", [])
    declared = structure.get("declared_independent_count")
    if declared is None:
        return None
    groups = {e.get("lineage_group") for e in envs if e.get("lineage_group")}
    computed_independent = len(groups) if groups else len(envs)
    if declared > computed_independent:
        return "blocked_lineage_inflation", [
            f"declared {declared} independent origins but lineage groups yield "
            f"{computed_independent}"
        ]
    return None


def _check_canonical_outputs(structure: dict) -> tuple[str, list[str]] | None:
    """A required canonical output missing, or whose bytes carry a placeholder
    sentinel (structurally incomplete), -> blocked_missing_canonical_output."""
    outputs = structure.get("outputs") or {}
    for required in structure.get("required_outputs", []):
        out = outputs.get(required)
        if out is None:
            return "blocked_missing_canonical_output", [f"missing required output {required}"]
        body = out.get("body", "")
        if any(s in body for s in _PLACEHOLDER_SENTINELS) or out.get("complete") is False:
            return "blocked_missing_canonical_output", [
                f"required output {required} is structurally incomplete (placeholder)"
            ]
    return None


def _check_output_bundle_binding(structure: dict) -> tuple[str, list[str]] | None:
    """An output bound to a bundle hash other than the frozen one ->
    blocked_stale_hash."""
    frozen = (structure.get("bundle") or {}).get("bundle_sha256")
    outputs = structure.get("outputs") or {}
    for name, out in outputs.items():
        bound = out.get("input_bundle_sha256")
        if bound is not None and frozen is not None and bound != frozen:
            return "blocked_stale_hash", [
                f"output {name} bound to bundle {bound} but frozen bundle is {frozen}"
            ]
    return None


def _check_reviews(structure: dict) -> tuple[str, list[str]] | None:
    """A specialist reviewer returning HOLD (bound to the exact hash) ->
    blocked_review_hold."""
    reviews = structure.get("reviews") or {}
    if not reviews:
        return None
    expected = (structure.get("bundle") or {}).get("bundle_sha256")
    committee = rc.evaluate_committee(reviews, expected_sha256=expected)
    if committee["status"] == "PASS":
        return None
    if committee["hold_roles"] or committee["stale_roles"] or committee["missing_roles"]:
        return "blocked_review_hold", [
            f"committee not clean: hold={committee['hold_roles']} "
            f"stale={committee['stale_roles']} missing={committee['missing_roles']}"
        ]
    return "blocked_review_hold", [f"committee FAIL: {committee['fail_roles']}"]


def _check_prior_work(structure: dict) -> tuple[str, list[str]] | None:
    """A missing required prior-work surface -> blocked_missing_prior_work_context;
    a duplicate capability already completed/rejected -> blocked_duplicate_prior_work."""
    pw = structure.get("prior_work")
    if not pw:
        return None
    for surface, checked in pw.get("surfaces", {}).items():
        if not checked:
            return "blocked_missing_prior_work_context", [f"prior-work surface {surface} not checked"]
    for match in pw.get("matches", []):
        if match.get("classification") in {"duplicate_do_not_build"} and structure.get("recommends_build"):
            return "blocked_duplicate_prior_work", [
                f"playbook recommends building {match.get('id')} already {match.get('state')}"
            ]
    return None


def _check_activation(structure: dict) -> tuple[str, list[str]] | None:
    """An activation proposal that attempts a write mutation is owner-gated ->
    blocked_owner_gate (prototype stops at owner_review_ready; no write adapter)."""
    activation = structure.get("activation")
    if not activation:
        return None
    if activation.get("mutation_class") in {"write", "update", "delete", "create"} and \
            not activation.get("owner_approved"):
        return "blocked_owner_gate", [
            f"activation mutation_class {activation.get('mutation_class')} requires owner approval"
        ]
    return None


def _check_improvement(structure: dict) -> tuple[str, list[str]] | None:
    """An improvement labelled improved with no observed evidence ->
    blocked_unverified_improvement; an unvalidated automatic mutation ->
    blocked_owner_gate."""
    imp = structure.get("improvement")
    if not imp:
        return None
    if imp.get("automatic_mutation") and not imp.get("owner_approved"):
        return "blocked_owner_gate", ["improvement attempts unvalidated automatic mutation"]
    if imp.get("classification") == "confirmed" and not imp.get("observed_evidence"):
        return "blocked_unverified_improvement", [
            "improvement classified confirmed without observed outcome evidence"
        ]
    return None


def _check_resume(structure: dict) -> tuple[str, list[str]] | None:
    """A same-correlation-id resume reuses valid completed stages ->
    reuse_valid_stages_without_duplicate_output."""
    resume = structure.get("resume")
    if not resume:
        return None
    run = resume["run_state"]
    plan = rsm.resume(run, correlation_id=resume["correlation_id"],
                      completed_stage_hashes=resume["completed_stage_hashes"])
    if plan["outcome"] == "reuse_valid_stages_without_duplicate_output":
        return "reuse_valid_stages_without_duplicate_output", [
            f"reused stages {plan['reused_stages']} with no duplicate outputs"
        ]
    return None


# Ordered exactly like the controller stage progression.
_CHECKS: tuple[Callable[[dict], tuple[str, list[str]] | None], ...] = (
    _check_provider,
    _check_evidence,
    _check_lineage,
    _check_output_bundle_binding,
    _check_canonical_outputs,
    _check_reviews,
    _check_prior_work,
    _check_activation,
    _check_improvement,
    _check_resume,
)


def evaluate_fixture(fixture: dict, *, ledger_path: Path | None = None) -> dict:
    """Evaluate a fixture by STRUCTURE ONLY. Returns the first blocked/terminal
    state, else owner_review_ready for a structurally clean committee campaign.

    Fail-closed: every returned state MUST be within the shared run-state enum;
    a check that yields an out-of-enum state is a hard error, never a silent pass.
    """
    structure = fixture.get("structure", {})
    for check in _CHECKS:
        outcome = check(structure)
        if outcome is not None:
            state, reasons = outcome
            if state not in rsm.STATES:
                raise ValueError(f"check produced out-of-enum state: {state!r}")
            return {"state": state, "reasons": reasons}
    return {"state": "owner_review_ready", "reasons": ["structurally clean committee campaign"]}


# --------------------------------------------------------------------------- #
# Fixture matrix construction                                                  #
# --------------------------------------------------------------------------- #

def _clean_bundle() -> dict:
    body = b"complete canonical evidence\n"
    return {
        "bundle_sha256": "f" * 64,
        "source_envelopes": [
            {"source_id": "s1", "lineage_group": "g1"},
            {"source_id": "s2", "lineage_group": "g2"},
        ],
        "claims": [{"claim_id": "c1", "evidence_refs": ["evidence/a.md"], "confidence": "high", "caveats": []}],
        "artifact_manifest": [{"path": "evidence/a.md", "sha256": _sha(body)}],
    }


def _clean_outputs(bundle_sha: str) -> dict:
    return {
        name: {"input_bundle_sha256": bundle_sha, "body": "complete brief\n", "complete": True}
        for name in ("detailed_evidence_pack", "executive_decision_brief")
    }


def _clean_reviews(bundle_sha: str) -> dict:
    return {
        role: rc.build_receipt(
            role=role, reviewed_sha256=bundle_sha, reviewer=f"{role}-agent",
            checks={c: "PASS" for c in rc.REQUIRED_CHECKS[role]}, verdict="PASS",
        )
        for role in ("evidence_qa", "taxonomy", "librarian", "adversarial")
    }


def _clean_prior_work() -> dict:
    return {
        "surfaces": {"linear": True, "knowledge_base": True, "sessions": True, "repos": True},
        "matches": [],
    }


def _base_structure() -> dict:
    bundle = _clean_bundle()
    sha = bundle["bundle_sha256"]
    return {
        "bundle": bundle,
        "required_outputs": ["detailed_evidence_pack", "executive_decision_brief"],
        "outputs": _clean_outputs(sha),
        "reviews": _clean_reviews(sha),
        "prior_work": _clean_prior_work(),
        "activation": {"mutation_class": "proposal_only", "owner_approved": False},
        "improvement": {"classification": "not_yet_measurable", "observed_evidence": False},
    }


def build_positive_fixture(*, ledger_path: Path | None = None) -> dict:
    return {"name": "positive_committee", "label": "positive", "description": "clean campaign",
            "structure": _base_structure()}


def build_fixture_matrix(*, ledger_path: Path | None = None) -> list[dict]:
    fixtures: list[dict] = []

    def add(name: str, description: str, mutate: Callable[[dict], None]) -> None:
        s = _base_structure()
        mutate(s)
        fixtures.append({"name": name, "label": name, "description": description, "structure": s})

    # 1. canonical full pack contains a placeholder while a substitute is complete
    def _f1(s):
        s["outputs"]["detailed_evidence_pack"]["body"] = "intro\x00PLACEHOLDER\x00rest"
    add("canonical_full_pack_has_placeholder", "canonical output carries a placeholder sentinel", _f1)

    # 2. missing executive brief
    def _f2(s):
        s["outputs"].pop("executive_decision_brief")
    add("missing_executive_brief", "required canonical output absent", _f2)

    # 3. output generated from a different bundle hash
    def _f3(s):
        s["outputs"]["executive_decision_brief"]["input_bundle_sha256"] = "0" * 64
    add("output_from_different_bundle_hash", "output bound to a stale bundle hash", _f3)

    # 4. source evidence missing / unresolved
    def _f4(s):
        s["bundle"]["claims"][0]["evidence_refs"] = ["evidence/missing.md"]
    add("source_evidence_missing", "claim evidence does not resolve", _f4)

    # 5. reviewer returns HOLD
    def _f5(s):
        checks = {c: "PASS" for c in rc.REQUIRED_CHECKS["adversarial"]}
        first = next(iter(checks))
        checks[first] = "HOLD"
        s["reviews"]["adversarial"] = rc.build_receipt(
            role="adversarial", reviewed_sha256=s["bundle"]["bundle_sha256"],
            reviewer="adv", checks=checks, verdict="PASS")
    add("reviewer_returns_hold", "a specialist reviewer holds", _f5)

    # 6. provider returns 401/403
    def _f6(s):
        def _runner():
            raise ProviderAuthError("401 Unauthorized")
        s["provider"] = {"runner": _runner}
    add("provider_401_403", "provider auth failure", _f6)

    # 7. provider timeout with partial artifact
    def _f7(s):
        s["provider"] = {"terminal_reason": "timeout", "partial_artifact": "outputs/partial.md"}
    add("provider_timeout_partial_artifact", "provider timeout leaves a partial artifact", _f7)

    # 8. same-lineage sources falsely presented as independent
    def _f8(s):
        s["bundle"]["source_envelopes"] = [
            {"source_id": "s1", "lineage_group": "shared"},
            {"source_id": "s2", "lineage_group": "shared"},
            {"source_id": "s3", "lineage_group": "shared"},
        ]
        s["declared_independent_count"] = 3
    add("same_lineage_false_independence", "same-lineage inflated to independent", _f8)

    # 9. interrupted run resumes without duplication
    def _f9(s):
        run = rsm.RunState().append(rsm.Transition(
            correlation_id="corr-9", from_state=None, to_state="accepted",
            occurred_at="2026-09-11T00:00:00+00:00", actor="c", reason="accepted"))
        run = run.append(rsm.Transition(
            correlation_id="corr-9", from_state="accepted", to_state="research_running",
            occurred_at="2026-09-11T01:00:00+00:00", actor="c", reason="run"))
        run = run.append(rsm.Transition(
            correlation_id="corr-9", from_state="research_running", to_state="bundle_validated",
            occurred_at="2026-09-11T02:00:00+00:00", actor="c", reason="validated",
            artifact_hashes={"bundle": "a" * 64}))
        run = run.append(rsm.Transition(
            correlation_id="corr-9", from_state="bundle_validated", to_state="bundle_frozen",
            occurred_at="2026-09-11T03:00:00+00:00", actor="c", reason="frozen",
            artifact_hashes={"bundle": "a" * 64}))
        s["resume"] = {"run_state": run, "correlation_id": "corr-9",
                       "completed_stage_hashes": {"bundle": "a" * 64}}
    add("interrupted_run_resumes", "same correlation-id resume", _f9)

    # 10. activation proposal attempts an unapproved write
    def _f10(s):
        s["activation"] = {"mutation_class": "write", "owner_approved": False}
    add("activation_unapproved_write", "activation attempts an unapproved write", _f10)

    # 11. playbook without required prior-work context
    def _f11(s):
        s["prior_work"]["surfaces"]["knowledge_base"] = False
    add("playbook_without_prior_work", "prior-work surface not checked", _f11)

    # 12. playbook recommends a duplicate capability already completed/rejected
    def _f12(s):
        s["prior_work"]["matches"] = [
            {"id": "ISSUE-500", "state": "Done", "classification": "duplicate_do_not_build"}]
        s["recommends_build"] = True
    add("playbook_duplicate_capability", "playbook duplicates completed work", _f12)

    # 13. implementation labelled improved without observed evidence
    def _f13(s):
        s["improvement"] = {"classification": "confirmed", "observed_evidence": False}
    add("improved_without_observed_evidence", "improvement confirmed without evidence", _f13)

    # 14. improvement proposal attempts unvalidated automatic mutation
    def _f14(s):
        s["improvement"] = {"classification": "not_yet_measurable",
                            "automatic_mutation": True, "owner_approved": False}
    add("improvement_unvalidated_auto_mutation", "improvement auto-mutates without approval", _f14)

    return fixtures


__all__ = [
    "ProviderAuthError", "evaluate_fixture", "build_fixture_matrix",
    "build_positive_fixture",
]
