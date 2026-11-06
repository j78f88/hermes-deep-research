#!/usr/bin/env python3
"""RED-first tests for ISSUE-569 distinct specialist-reviewer receipt schemas.

Four role-distinct receipt contracts (Evidence QA, Taxonomy reviewer, Librarian, adversarial),
each binding to the exact reviewed artifact hash. A receipt bound to a different
hash cannot count as a review. No role can substitute for another.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


rc = _load("reviewer_receipts")

HASH = "a" * 64
OTHER = "b" * 64


# --- role distinctness -------------------------------------------------------

def test_each_role_has_a_distinct_schema_version():
    versions = {
        rc.EVIDENCE_QA_SCHEMA, rc.TAXONOMY_SCHEMA, rc.LIBRARIAN_SCHEMA, rc.ADVERSARIAL_SCHEMA,
    }
    assert len(versions) == 4  # no two roles share a schema


def test_role_required_fields_are_distinct_per_role():
    # each role checks its own dimension set; no overlap collapse into one schema
    assert rc.REQUIRED_CHECKS["evidence_qa"] != rc.REQUIRED_CHECKS["taxonomy"]
    assert rc.REQUIRED_CHECKS["librarian"] != rc.REQUIRED_CHECKS["adversarial"]
    assert "claim_evidence_fidelity" in rc.REQUIRED_CHECKS["evidence_qa"]
    assert "taxonomy" in rc.REQUIRED_CHECKS["taxonomy"]
    assert "progressive_disclosure" in rc.REQUIRED_CHECKS["librarian"]
    assert "false_done" in rc.REQUIRED_CHECKS["adversarial"]


# --- exact-hash enforcement --------------------------------------------------

def test_receipt_must_bind_to_reviewed_artifact_hash():
    receipt = rc.build_receipt(
        role="evidence_qa", reviewed_sha256=HASH, reviewer="qa-agent",
        checks={c: "PASS" for c in rc.REQUIRED_CHECKS["evidence_qa"]}, verdict="PASS",
    )
    result = rc.validate_receipt(receipt, expected_sha256=HASH)
    assert result["status"] == "PASS"
    assert result["counts_as_review"] is True


def test_receipt_bound_to_wrong_hash_does_not_count():
    receipt = rc.build_receipt(
        role="evidence_qa", reviewed_sha256=OTHER, reviewer="qa-agent",
        checks={c: "PASS" for c in rc.REQUIRED_CHECKS["evidence_qa"]}, verdict="PASS",
    )
    result = rc.validate_receipt(receipt, expected_sha256=HASH)
    assert result["status"] == "blocked_stale_hash"
    assert result["counts_as_review"] is False


def test_missing_required_check_fails_closed():
    checks = {c: "PASS" for c in rc.REQUIRED_CHECKS["taxonomy"]}
    checks.pop(next(iter(checks)))
    receipt = rc.build_receipt(
        role="taxonomy", reviewed_sha256=HASH, reviewer="taxonomy-agent",
        checks=checks, verdict="PASS",
    )
    result = rc.validate_receipt(receipt, expected_sha256=HASH)
    assert result["status"] == "FAIL"
    assert result["counts_as_review"] is False


def test_a_failing_check_holds_even_if_verdict_claims_pass():
    checks = {c: "PASS" for c in rc.REQUIRED_CHECKS["librarian"]}
    a_check = next(iter(checks))
    checks[a_check] = "HOLD"
    receipt = rc.build_receipt(
        role="librarian", reviewed_sha256=HASH, reviewer="lib-agent",
        checks=checks, verdict="PASS",  # lying verdict
    )
    result = rc.validate_receipt(receipt, expected_sha256=HASH)
    assert result["status"] == "HOLD"  # derived from checks, not the claimed verdict


def test_role_mismatch_rejected():
    receipt = rc.build_receipt(
        role="evidence_qa", reviewed_sha256=HASH, reviewer="qa-agent",
        checks={c: "PASS" for c in rc.REQUIRED_CHECKS["evidence_qa"]}, verdict="PASS",
    )
    # asking to validate it AS a taxonomy receipt must reject: no role substitution
    result = rc.validate_receipt(receipt, expected_sha256=HASH, expected_role="taxonomy")
    assert result["status"] == "FAIL"
    assert "role" in " ".join(result["reasons"]).lower()


def test_provider_auth_failure_never_counts_as_review():
    receipt = rc.build_receipt(
        role="adversarial", reviewed_sha256=HASH, reviewer="adv-agent",
        checks={}, verdict="blocked_provider_auth",
    )
    result = rc.validate_receipt(receipt, expected_sha256=HASH)
    assert result["status"] == "blocked_provider_auth"
    assert result["counts_as_review"] is False


def test_committee_requires_all_four_distinct_roles_pass():
    good = {
        role: rc.build_receipt(
            role=role, reviewed_sha256=HASH, reviewer=f"{role}-agent",
            checks={c: "PASS" for c in rc.REQUIRED_CHECKS[role]}, verdict="PASS",
        )
        for role in ("evidence_qa", "taxonomy", "librarian", "adversarial")
    }
    verdict = rc.evaluate_committee(good, expected_sha256=HASH)
    assert verdict["status"] == "PASS"
    assert set(verdict["roles_passed"]) == {"evidence_qa", "taxonomy", "librarian", "adversarial"}


def test_committee_holds_when_one_role_missing():
    partial = {
        role: rc.build_receipt(
            role=role, reviewed_sha256=HASH, reviewer=f"{role}-agent",
            checks={c: "PASS" for c in rc.REQUIRED_CHECKS[role]}, verdict="PASS",
        )
        for role in ("evidence_qa", "taxonomy", "librarian")  # missing adversarial
    }
    verdict = rc.evaluate_committee(partial, expected_sha256=HASH)
    assert verdict["status"] == "HOLD"
    assert "adversarial" in verdict["missing_roles"]


def test_committee_holds_when_two_receipts_bind_to_different_hashes():
    receipts = {
        "evidence_qa": rc.build_receipt(role="evidence_qa", reviewed_sha256=HASH, reviewer="q",
                                        checks={c: "PASS" for c in rc.REQUIRED_CHECKS["evidence_qa"]}, verdict="PASS"),
        "taxonomy": rc.build_receipt(role="taxonomy", reviewed_sha256=OTHER, reviewer="r",
                                  checks={c: "PASS" for c in rc.REQUIRED_CHECKS["taxonomy"]}, verdict="PASS"),
        "librarian": rc.build_receipt(role="librarian", reviewed_sha256=HASH, reviewer="l",
                                      checks={c: "PASS" for c in rc.REQUIRED_CHECKS["librarian"]}, verdict="PASS"),
        "adversarial": rc.build_receipt(role="adversarial", reviewed_sha256=HASH, reviewer="a",
                                        checks={c: "PASS" for c in rc.REQUIRED_CHECKS["adversarial"]}, verdict="PASS"),
    }
    verdict = rc.evaluate_committee(receipts, expected_sha256=HASH)
    assert verdict["status"] != "PASS"
    assert "taxonomy" in verdict["stale_roles"]
