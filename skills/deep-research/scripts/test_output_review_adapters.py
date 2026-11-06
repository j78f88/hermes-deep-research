#!/usr/bin/env python3
"""RED-first tests for ISSUE-572 output-producer and specialist-review adapters.

Covers Phase 2 of the ISSUE-569 plan:
  * one contract for output producers, one for specialist reviewers;
  * deterministic fixture adapters (no provider, no network);
  * cross-output claim / caveat / confidence reconciliation;
  * fail-closed provider/auth receipt semantics (blocked_provider_auth is never
    a PASS and cannot substitute for a real review).
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ADAPTER_PATH = SCRIPT_DIR / "output_review_adapters.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


ora = _load("output_review_adapters", ADAPTER_PATH)

BUNDLE_HASH = "a" * 64


def frozen_bundle() -> dict:
    return {
        "bundle_sha256": BUNDLE_HASH,
        "claims": [
            {"claim_id": "CLM-001", "confidence": "high", "caveats": ["single vendor source"]},
            {"claim_id": "CLM-002", "confidence": "medium", "caveats": []},
        ],
    }


def detailed_output() -> dict:
    return {
        "output_id": "detailed_evidence_pack-v1",
        "input_bundle_sha256": BUNDLE_HASH,
        "output_sha256": "b" * 64,
        "claim_ids_used": ["CLM-001", "CLM-002"],
        "caveats_preserved": ["single vendor source"],
        "confidence": "medium",
        "producer": "fixture-detailed",
        "status": "complete",
    }


def executive_output() -> dict:
    return {
        "output_id": "executive_decision_brief-v1",
        "input_bundle_sha256": BUNDLE_HASH,
        "output_sha256": "c" * 64,
        "claim_ids_used": ["CLM-001"],
        "caveats_preserved": ["single vendor source"],
        "confidence": "medium",
        "producer": "fixture-exec",
        "status": "complete",
    }


# --------------------------------------------------------------------------- #
# Output producer contract                                                     #
# --------------------------------------------------------------------------- #

def test_fixture_producer_emits_hash_bound_output():
    out = ora.run_output_producer(
        producer=ora.fixture_producer(executive_output()),
        bundle=frozen_bundle(),
    )
    assert out["status"] == "complete"
    assert out["input_bundle_sha256"] == BUNDLE_HASH


def test_producer_output_bound_to_wrong_bundle_hash_is_rejected():
    bad = executive_output()
    bad["input_bundle_sha256"] = "d" * 64  # not the frozen bundle
    out = ora.run_output_producer(producer=ora.fixture_producer(bad), bundle=frozen_bundle())
    assert out["status"] == "blocked_stale_hash"


def test_producer_using_unknown_claim_id_is_rejected():
    bad = executive_output()
    bad["claim_ids_used"] = ["CLM-999"]  # not in the bundle
    out = ora.run_output_producer(producer=ora.fixture_producer(bad), bundle=frozen_bundle())
    assert out["status"] == "blocked_missing_evidence"
    assert any("CLM-999" in r for r in out["reasons"])


# --------------------------------------------------------------------------- #
# Specialist review contract                                                   #
# --------------------------------------------------------------------------- #

def test_fixture_reviewer_pass_receipt_is_hash_bound():
    receipt = ora.run_specialist_review(
        reviewer=ora.fixture_reviewer(verdict="PASS"),
        role="evidence_qa",
        output=executive_output(),
    )
    assert receipt["status"] == "PASS"
    assert receipt["role"] == "evidence_qa"
    assert receipt["reviewed_output_sha256"] == executive_output()["output_sha256"]


def test_reviewer_hold_receipt_is_not_pass():
    receipt = ora.run_specialist_review(
        reviewer=ora.fixture_reviewer(verdict="HOLD", reasons=["claim CLM-001 caveat dropped"]),
        role="adversarial",
        output=executive_output(),
    )
    assert receipt["status"] == "HOLD"
    assert receipt["reasons"]


def test_provider_auth_failure_is_fail_closed_and_not_a_review():
    # A reviewer that raises a provider/auth error must yield blocked_provider_auth,
    # never a PASS, and must be marked as NOT a completed review.
    def auth_failing_reviewer(role, output):
        raise ora.ProviderAuthError("401 Unauthorized")

    receipt = ora.run_specialist_review(
        reviewer=auth_failing_reviewer, role="taxonomy", output=executive_output()
    )
    assert receipt["status"] == "blocked_provider_auth"
    assert receipt["counts_as_review"] is False
    assert receipt["status"] != "PASS"


def test_generic_provider_error_does_not_pass():
    def broken_reviewer(role, output):
        raise RuntimeError("timeout")

    receipt = ora.run_specialist_review(reviewer=broken_reviewer, role="librarian", output=executive_output())
    assert receipt["status"] != "PASS"
    assert receipt["counts_as_review"] is False


# --------------------------------------------------------------------------- #
# Cross-output reconciliation                                                  #
# --------------------------------------------------------------------------- #

def test_cross_output_consistency_passes_for_aligned_outputs():
    result = ora.check_cross_output_consistency(
        bundle=frozen_bundle(), outputs=[detailed_output(), executive_output()]
    )
    assert result["status"] == "PASS"
    assert result["reasons"] == []


def test_cross_output_flags_dropped_caveat_in_executive_brief():
    exec_out = executive_output()
    exec_out["caveats_preserved"] = []  # dropped the required caveat for CLM-001
    result = ora.check_cross_output_consistency(
        bundle=frozen_bundle(), outputs=[detailed_output(), exec_out]
    )
    assert result["status"] == "HOLD"
    assert any("caveat" in r.lower() for r in result["reasons"])


def test_cross_output_flags_inflated_confidence_vs_bundle():
    exec_out = executive_output()
    exec_out["confidence"] = "high"  # higher than the max claim confidence used (CLM-001 high, ok)
    # Make it clearly inflated: use only CLM-002 (medium) but claim high.
    exec_out["claim_ids_used"] = ["CLM-002"]
    exec_out["caveats_preserved"] = []
    result = ora.check_cross_output_consistency(
        bundle=frozen_bundle(), outputs=[detailed_output(), exec_out]
    )
    assert result["status"] == "HOLD"
    assert any("confidence" in r.lower() for r in result["reasons"])


def test_cross_output_flags_output_bound_to_wrong_bundle():
    exec_out = executive_output()
    exec_out["input_bundle_sha256"] = "e" * 64
    result = ora.check_cross_output_consistency(
        bundle=frozen_bundle(), outputs=[detailed_output(), exec_out]
    )
    assert result["status"] == "HOLD"
    assert any("bundle" in r.lower() or "hash" in r.lower() for r in result["reasons"])


# --------------------------------------------------------------------------- #
# Purity                                                                       #
# --------------------------------------------------------------------------- #

def test_adapters_do_not_mutate_inputs():
    bundle = frozen_bundle()
    outputs = [detailed_output(), executive_output()]
    bundle_before = copy.deepcopy(bundle)
    outputs_before = copy.deepcopy(outputs)
    ora.check_cross_output_consistency(bundle=bundle, outputs=outputs)
    assert bundle == bundle_before
    assert outputs == outputs_before


# --- ISSUE-572 reviewer fold-in ---------------------------------------------- #

def test_run_output_producer_does_not_mutate_bundle():
    # A producer that tries to mutate the bundle must not affect the caller's copy.
    def mutating_producer(bundle):
        bundle["claims"].append({"claim_id": "CLM-666"})
        return executive_output()

    bundle = frozen_bundle()
    before = copy.deepcopy(bundle)
    ora.run_output_producer(producer=mutating_producer, bundle=bundle)
    assert bundle == before


def test_reviewer_returning_wrong_output_hash_is_rejected():
    # A reviewer that binds its PASS to a DIFFERENT output hash must not count as
    # a valid review of this output.
    def spoofing_reviewer(role, output):
        return {"status": "PASS", "reviewed_output_sha256": "f" * 64, "reasons": []}

    receipt = ora.run_specialist_review(
        reviewer=spoofing_reviewer, role="evidence_qa", output=executive_output()
    )
    assert receipt["status"] != "PASS"
    assert receipt["counts_as_review"] is False
    assert any("hash" in r.lower() or "bind" in r.lower() for r in receipt["reasons"])


def test_reviewer_matching_output_hash_is_accepted():
    def honest_reviewer(role, output):
        return {"status": "PASS", "reviewed_output_sha256": output["output_sha256"], "reasons": []}

    receipt = ora.run_specialist_review(
        reviewer=honest_reviewer, role="evidence_qa", output=executive_output()
    )
    assert receipt["status"] == "PASS"
    assert receipt["counts_as_review"] is True
