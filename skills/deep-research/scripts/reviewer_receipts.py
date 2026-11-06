#!/usr/bin/env python3
"""distinct specialist-reviewer receipt schemas and exact-hash contracts.

Four role-distinct receipt schemas (Evidence QA, Taxonomy reviewer, Librarian, adversarial),
each binding to the exact reviewed artifact hash. No role can substitute for
another; all four must PASS against the SAME artifact hash for a committee PASS.

Fail-closed rules (from the plan's "Assurance processes" and the graduation
criterion that provider/auth failure cannot count as review):

  * a receipt whose ``reviewed_sha256`` != the expected hash is
    ``blocked_stale_hash`` and does not count;
  * a missing required check for the role is ``FAIL`` and does not count;
  * the verdict is DERIVED from the checks (a lying ``verdict=PASS`` cannot
    override a failing check);
  * a ``blocked_provider_auth`` / ``blocked_exhausted_retry`` verdict never counts;
  * validating a receipt against the wrong role is ``FAIL`` (no role substitution).

Pure: no network, credential, or filesystem side effects.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import copy
from typing import Any

EVIDENCE_QA_SCHEMA = "research-evidence-qa-receipt-v1"
TAXONOMY_SCHEMA = "research-taxonomy-receipt-v1"
LIBRARIAN_SCHEMA = "research-librarian-receipt-v1"
ADVERSARIAL_SCHEMA = "research-adversarial-receipt-v1"

ROLE_SCHEMA = {
    "evidence_qa": EVIDENCE_QA_SCHEMA,
    "taxonomy": TAXONOMY_SCHEMA,
    "librarian": LIBRARIAN_SCHEMA,
    "adversarial": ADVERSARIAL_SCHEMA,
}

# Role-distinct required check dimensions (no role's set is a copy of another).
REQUIRED_CHECKS: dict[str, frozenset[str]] = {
    "evidence_qa": frozenset({
        "claim_evidence_fidelity", "source_class", "lineage", "freshness",
        "contradictions", "confidence",
    }),
    "taxonomy": frozenset({
        "taxonomy", "ownership", "duplicates", "supersession", "destination",
    }),
    "librarian": frozenset({
        "reader_jobs", "information_architecture", "progressive_disclosure",
        "navigation", "export_usefulness",
    }),
    "adversarial": frozenset({
        "false_done", "omission", "correlated_evidence", "authority_leakage",
        "retry_bypass",
    }),
}

_NON_COUNTING = {"blocked_provider_auth", "blocked_exhausted_retry", "blocked_stale_hash"}
_CHECK_VALUES = {"PASS", "HOLD", "FAIL"}
_ALL_ROLES = ("evidence_qa", "taxonomy", "librarian", "adversarial")


def build_receipt(*, role: str, reviewed_sha256: str, reviewer: str,
                  checks: dict[str, str], verdict: str,
                  reasons: list[str] | None = None) -> dict:
    if role not in ROLE_SCHEMA:
        raise ValueError(f"unknown reviewer role: {role}")
    return {
        "schema_version": ROLE_SCHEMA[role],
        "role": role,
        "reviewer": reviewer,
        "reviewed_sha256": reviewed_sha256,
        "checks": dict(checks),
        "declared_verdict": verdict,
        "reasons": list(reasons or []),
    }


def _derive_status(checks: dict[str, str]) -> str:
    values = set(checks.values())
    if "FAIL" in values:
        return "FAIL"
    if "HOLD" in values:
        return "HOLD"
    return "PASS"


def validate_receipt(receipt: dict, *, expected_sha256: str,
                     expected_role: str | None = None) -> dict:
    role = receipt.get("role")
    reasons: list[str] = []

    if role not in ROLE_SCHEMA:
        return {"status": "FAIL", "counts_as_review": False,
                "reasons": [f"unknown role {role!r}"], "role": role}

    # No role substitution.
    if expected_role is not None and role != expected_role:
        return {"status": "FAIL", "counts_as_review": False,
                "reasons": [f"role mismatch: receipt role {role!r} != expected {expected_role!r}"],
                "role": role}

    if receipt.get("schema_version") != ROLE_SCHEMA[role]:
        reasons.append(f"schema_version mismatch for role {role}")

    declared = receipt.get("declared_verdict", "FAIL")

    # Provider/auth or retry-exhaustion verdicts never count.
    if declared in _NON_COUNTING:
        return {"status": declared, "counts_as_review": False,
                "reasons": [f"non-counting verdict {declared}"], "role": role}

    # Exact-hash binding.
    if receipt.get("reviewed_sha256") != expected_sha256:
        return {"status": "blocked_stale_hash", "counts_as_review": False,
                "reasons": [
                    f"receipt bound to {receipt.get('reviewed_sha256')} but expected {expected_sha256}"
                ], "role": role}

    # Required checks for this role must all be present and valid.
    checks = receipt.get("checks") or {}
    required = REQUIRED_CHECKS[role]
    missing = required - set(checks)
    if missing:
        return {"status": "FAIL", "counts_as_review": False,
                "reasons": reasons + [f"missing required checks: {sorted(missing)}"], "role": role}
    bad_values = {k: v for k, v in checks.items() if v not in _CHECK_VALUES}
    if bad_values:
        return {"status": "FAIL", "counts_as_review": False,
                "reasons": reasons + [f"invalid check values: {bad_values}"], "role": role}

    # Verdict is derived from checks (lying declared_verdict cannot override).
    # Only a clean PASS counts as a completed review; HOLD/FAIL means the campaign
    # must HOLD and the review does not count as satisfied.
    status = _derive_status({k: checks[k] for k in required})
    return {
        "status": status,
        "counts_as_review": status == "PASS",
        "reasons": reasons + list(receipt.get("reasons", [])),
        "role": role,
    }


def evaluate_committee(receipts: dict[str, dict], *, expected_sha256: str) -> dict:
    """All four distinct roles must PASS against the same artifact hash."""
    roles_passed: list[str] = []
    stale_roles: list[str] = []
    hold_roles: list[str] = []
    fail_roles: list[str] = []
    missing_roles = [r for r in _ALL_ROLES if r not in receipts]

    for role in _ALL_ROLES:
        if role not in receipts:
            continue
        result = validate_receipt(receipts[role], expected_sha256=expected_sha256,
                                  expected_role=role)
        if result["status"] == "PASS":
            roles_passed.append(role)
        elif result["status"] == "blocked_stale_hash":
            stale_roles.append(role)
        elif result["status"] == "HOLD":
            hold_roles.append(role)
        else:
            fail_roles.append(role)

    # Per the plan's assurance contract: reviewer HOLD, FAIL, missing, stale or
    # inaccessible all mean campaign HOLD. Only an all-PASS committee passes.
    if missing_roles or stale_roles or hold_roles or fail_roles:
        status = "HOLD"
    else:
        status = "PASS"

    return {
        "status": status,
        "roles_passed": roles_passed,
        "missing_roles": missing_roles,
        "stale_roles": stale_roles,
        "hold_roles": hold_roles,
        "fail_roles": fail_roles,
    }


__all__ = [
    "EVIDENCE_QA_SCHEMA", "TAXONOMY_SCHEMA", "LIBRARIAN_SCHEMA", "ADVERSARIAL_SCHEMA",
    "ROLE_SCHEMA", "REQUIRED_CHECKS", "build_receipt", "validate_receipt",
    "evaluate_committee",
]
