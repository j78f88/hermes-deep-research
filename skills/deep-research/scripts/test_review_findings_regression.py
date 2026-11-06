#!/usr/bin/env python3
"""RED regression tests for independent-review findings on the ISSUE-569 slice.

Each test encodes a concrete defect the two-stage review flagged. They must fail
against the pre-fix code and pass after the targeted repair.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_SI = Path("SOURCE_INTELLIGENCE_ROOT")


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rsm = _load("run_state_machine")
rc = _load("reviewer_receipts")
sa = _load("source_assessment")
cr = _load("campaign_replay")


# --- Finding 1: resume must not emit a state outside the enum ----------------

def test_resume_partial_outcome_state_is_within_the_enum():
    run = rsm.RunState().append(rsm.Transition(
        correlation_id="c", from_state=None, to_state="accepted",
        occurred_at="2026-09-11T00:00:00+00:00", actor="a", reason="x"))
    run = run.append(rsm.Transition(
        correlation_id="c", from_state="accepted", to_state="research_running",
        occurred_at="2026-09-11T01:00:00+00:00", actor="a", reason="x"))
    run = run.append(rsm.Transition(
        correlation_id="c", from_state="research_running", to_state="bundle_validated",
        occurred_at="2026-09-11T02:00:00+00:00", actor="a", reason="x",
        artifact_hashes={"bundle": "a" * 64}))
    plan = rsm.resume(run, correlation_id="c", completed_stage_hashes={"bundle": "b" * 64})
    # a drifted hash must NOT reuse the stage, and the outcome must be a declared state
    assert plan["outcome"] in rsm.STATES, plan["outcome"]


# --- Finding 2: prior-work stages cannot be bypassed ------------------------

def test_bundle_frozen_cannot_jump_straight_to_outputs_running():
    run = rsm.RunState().append(rsm.Transition(
        correlation_id="c", from_state=None, to_state="accepted",
        occurred_at="2026-09-11T00:00:00+00:00", actor="a", reason="x"))
    run = run.append(rsm.Transition(
        correlation_id="c", from_state="accepted", to_state="research_running",
        occurred_at="2026-09-11T01:00:00+00:00", actor="a", reason="x"))
    run = run.append(rsm.Transition(
        correlation_id="c", from_state="research_running", to_state="bundle_validated",
        occurred_at="2026-09-11T02:00:00+00:00", actor="a", reason="x"))
    run = run.append(rsm.Transition(
        correlation_id="c", from_state="bundle_validated", to_state="bundle_frozen",
        occurred_at="2026-09-11T03:00:00+00:00", actor="a", reason="x"))
    with pytest.raises(rsm.StateError):
        run.append(rsm.Transition(
            correlation_id="c", from_state="bundle_frozen", to_state="outputs_running",
            occurred_at="2026-09-11T04:00:00+00:00", actor="a", reason="x"))
    # the valid path goes through prior_work_scanning first
    ok = run.append(rsm.Transition(
        correlation_id="c", from_state="bundle_frozen", to_state="prior_work_scanning",
        occurred_at="2026-09-11T04:00:00+00:00", actor="a", reason="x"))
    assert ok.current_state() == "prior_work_scanning"


# --- Finding 3: non-PASS reviewer receipts must not count as a review --------

def test_hold_receipt_does_not_count_as_review():
    checks = {c: "PASS" for c in rc.REQUIRED_CHECKS["evidence_qa"]}
    checks[next(iter(checks))] = "HOLD"
    receipt = rc.build_receipt(role="evidence_qa", reviewed_sha256="a" * 64,
                               reviewer="q", checks=checks, verdict="PASS")
    result = rc.validate_receipt(receipt, expected_sha256="a" * 64)
    assert result["status"] == "HOLD"
    assert result["counts_as_review"] is False


def test_fail_receipt_does_not_count_as_review():
    checks = {c: "PASS" for c in rc.REQUIRED_CHECKS["taxonomy"]}
    checks[next(iter(checks))] = "FAIL"
    receipt = rc.build_receipt(role="taxonomy", reviewed_sha256="a" * 64,
                               reviewer="r", checks=checks, verdict="PASS")
    result = rc.validate_receipt(receipt, expected_sha256="a" * 64)
    assert result["status"] == "FAIL"
    assert result["counts_as_review"] is False


def test_committee_fail_role_yields_hold_not_pass():
    receipts = {}
    for role in ("evidence_qa", "taxonomy", "librarian", "adversarial"):
        checks = {c: "PASS" for c in rc.REQUIRED_CHECKS[role]}
        if role == "adversarial":
            checks[next(iter(checks))] = "FAIL"
        receipts[role] = rc.build_receipt(role=role, reviewed_sha256="a" * 64,
                                          reviewer=role, checks=checks, verdict="PASS")
    verdict = rc.evaluate_committee(receipts, expected_sha256="a" * 64)
    assert verdict["status"] == "HOLD"  # any non-PASS reviewer => campaign HOLD


# --- Finding 4: evaluate_fixture must fail-closed on out-of-enum state -------

def test_evaluate_fixture_rejects_a_check_returning_out_of_enum_state(monkeypatch):
    bad_check = lambda structure: ("not_a_real_state", ["injected"])  # noqa: E731
    monkeypatch.setattr(cr, "_CHECKS", (bad_check,))
    with pytest.raises(Exception):
        cr.evaluate_fixture({"structure": {}}, ledger_path=_SI)


# --- Finding 5: owner_surface canonicalized in the event representation ------

def test_as_dict_canonicalizes_owner_surface():
    ev = sa.SourceAssessmentEvent(
        source_id="s1", source_version="v1", dimension="usefulness",
        old_value="candidate", new_value="accepted", evidence_refs=["e1"],
        assessor="qa", method_version="m1", observed_at="2026-09-11T00:00:00+00:00",
        reason="use", lifecycle_transition=True, owner_surface="LINEAR://ISSUE-569")
    ev.validate()
    assert ev.as_dict()["owner_surface"] == "linear://issue-569"
