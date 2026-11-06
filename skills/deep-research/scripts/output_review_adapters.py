#!/usr/bin/env python3
"""output-producer and specialist-review adapter contracts.

Phase 2 of the ISSUE-569 evidence-to-output plan. Two contracts, both fixture-first
and provider-agnostic:

  * output producers consume a frozen evidence bundle *by hash* and emit an
    output manifest carrying ``input_bundle_sha256``, ``output_sha256``,
    ``claim_ids_used``, ``caveats_preserved``, ``confidence``, producer identity
    and status;
  * specialist reviewers inspect an exact output manifest and return a hash-bound
    receipt whose verdict is PASS / HOLD / FAIL — or, when the reviewer's provider
    fails to authenticate, ``blocked_provider_auth``.

Fail-closed rules (mirroring the plan's retry/recovery table and the graduation
criterion that "provider/auth failure cannot count as review"):

  * an output bound to a bundle hash other than the frozen one is
    ``blocked_stale_hash``;
  * an output using a claim id absent from the bundle is
    ``blocked_missing_evidence``;
  * a ProviderAuthError raised by a reviewer becomes ``blocked_provider_auth``
    with ``counts_as_review=false`` — it is never a PASS;
  * any other reviewer exception becomes ``blocked_exhausted_retry`` with
    ``counts_as_review=false``.

Cross-output reconciliation checks that every output binds to the same frozen
bundle, uses only real claim ids, preserves the caveats attached to the claims it
uses, and does not inflate confidence above the strongest claim it relies on.

All functions are pure: they never mutate their inputs and perform no network,
credential, or filesystem side effects.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import copy
from typing import Any, Callable

# Ordered weakest -> strongest so confidence inflation is comparable.
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


class ProviderAuthError(RuntimeError):
    """Raised by a reviewer runner when the provider rejects auth (401/403)."""


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _bundle_claim_ids(bundle: dict) -> set[str]:
    return {c["claim_id"] for c in bundle.get("claims", [])}


def _bundle_claims_by_id(bundle: dict) -> dict[str, dict]:
    return {c["claim_id"]: c for c in bundle.get("claims", [])}


def _confidence_rank(value: str) -> int:
    return CONFIDENCE_ORDER.get(value, -1)


# --------------------------------------------------------------------------- #
# Output producer contract                                                     #
# --------------------------------------------------------------------------- #

def fixture_producer(output_manifest: dict) -> Callable[[dict], dict]:
    """A deterministic producer that returns a fixed output manifest (a copy)."""

    def _producer(_bundle: dict) -> dict:
        return dict(output_manifest)

    return _producer


def run_output_producer(*, producer: Callable[[dict], dict], bundle: dict) -> dict:
    """Run a producer against a frozen bundle and validate the emitted manifest.

    The producer receives a deep copy of the bundle so it cannot mutate the
    caller's object; the returned manifest is likewise deep-copied.
    """
    manifest = copy.deepcopy(producer(copy.deepcopy(bundle)))  # isolate both directions
    reasons: list[str] = []

    frozen = bundle.get("bundle_sha256")
    if manifest.get("input_bundle_sha256") != frozen:
        manifest["status"] = "blocked_stale_hash"
        manifest["reasons"] = [
            f"output bound to bundle {manifest.get('input_bundle_sha256')} but frozen bundle is {frozen}"
        ]
        return manifest

    known = _bundle_claim_ids(bundle)
    for cid in manifest.get("claim_ids_used", []):
        if cid not in known:
            reasons.append(f"output uses unknown claim id {cid} (not in frozen bundle)")
    if reasons:
        manifest["status"] = "blocked_missing_evidence"
        manifest["reasons"] = reasons
        return manifest

    manifest.setdefault("status", "complete")
    manifest["reasons"] = []
    return manifest


# --------------------------------------------------------------------------- #
# Specialist review contract                                                   #
# --------------------------------------------------------------------------- #

def fixture_reviewer(*, verdict: str = "PASS", reasons: list[str] | None = None) -> Callable[[str, dict], dict]:
    """A deterministic reviewer returning a fixed verdict."""

    def _reviewer(role: str, output: dict) -> dict:
        return {"status": verdict, "reasons": list(reasons or [])}

    return _reviewer


def run_specialist_review(*, reviewer: Callable[[str, dict], dict], role: str, output: dict) -> dict:
    """Run a specialist reviewer and produce a hash-bound receipt.

    Fail-closed: a provider/auth failure yields ``blocked_provider_auth`` and is
    explicitly marked as NOT counting as a review; any other reviewer exception
    yields ``blocked_exhausted_retry`` and likewise does not count. Neither can
    ever be a PASS.
    """
    output_hash = output.get("output_sha256")
    base = {"role": role, "reviewed_output_sha256": output_hash}

    try:
        verdict = reviewer(role, output)
    except ProviderAuthError as exc:
        return {
            **base,
            "status": "blocked_provider_auth",
            "reasons": [f"provider auth failure: {exc}"],
            "counts_as_review": False,
        }
    except Exception as exc:  # noqa: BLE001 - deliberately fail-closed on any reviewer error
        return {
            **base,
            "status": "blocked_exhausted_retry",
            "reasons": [f"reviewer error: {exc}"],
            "counts_as_review": False,
        }

    status = verdict.get("status", "FAIL")

    # Hash-binding: if the reviewer declares which output it reviewed, it must be
    # THIS output. A receipt bound to a different hash cannot count as a review.
    declared_hash = verdict.get("reviewed_output_sha256")
    if declared_hash is not None and declared_hash != output_hash:
        return {
            **base,
            "status": "blocked_stale_hash",
            "reasons": [
                f"reviewer receipt hash-bound to output {declared_hash} but reviewed output is {output_hash}"
            ],
            "counts_as_review": False,
        }

    return {
        **base,
        "status": status,
        "reasons": list(verdict.get("reasons", [])),
        "counts_as_review": status in {"PASS", "HOLD", "FAIL"},
    }


# --------------------------------------------------------------------------- #
# Cross-output reconciliation                                                  #
# --------------------------------------------------------------------------- #

def check_cross_output_consistency(*, bundle: dict, outputs: list[dict]) -> dict:
    """Reconcile a set of outputs against the frozen bundle and each other."""
    reasons: list[str] = []
    frozen = bundle.get("bundle_sha256")
    known = _bundle_claim_ids(bundle)
    claims_by_id = _bundle_claims_by_id(bundle)

    for out in outputs:
        oid = out.get("output_id", "<output>")

        # 1. Every output must bind to the same frozen bundle.
        if out.get("input_bundle_sha256") != frozen:
            reasons.append(f"{oid}: bound to bundle hash {out.get('input_bundle_sha256')} != frozen {frozen}")
            continue  # further checks are meaningless against a stale bundle

        used = out.get("claim_ids_used", [])

        # 2. Only real claim ids may be used.
        for cid in used:
            if cid not in known:
                reasons.append(f"{oid}: uses unknown claim id {cid}")

        # 3. Caveats attached to used claims must be preserved.
        required_caveats: set[str] = set()
        for cid in used:
            if cid in claims_by_id:
                required_caveats.update(claims_by_id[cid].get("caveats", []))
        preserved = set(out.get("caveats_preserved", []))
        missing_caveats = required_caveats - preserved
        for caveat in sorted(missing_caveats):
            reasons.append(f"{oid}: dropped required caveat '{caveat}' for a claim it uses")

        # 4. Confidence must not exceed the strongest claim actually used.
        used_ranks = [
            _confidence_rank(claims_by_id[cid].get("confidence", ""))
            for cid in used
            if cid in claims_by_id
        ]
        if used_ranks:
            max_supported = max(used_ranks)
            declared = _confidence_rank(out.get("confidence", ""))
            if declared > max_supported:
                reasons.append(
                    f"{oid}: declares confidence '{out.get('confidence')}' higher than the "
                    "strongest claim it uses"
                )

    status = "PASS" if not reasons else "HOLD"
    return {"status": status, "reasons": reasons}


__all__ = [
    "ProviderAuthError",
    "fixture_producer",
    "run_output_producer",
    "fixture_reviewer",
    "run_specialist_review",
    "check_cross_output_consistency",
]
