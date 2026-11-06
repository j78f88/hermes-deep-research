#!/usr/bin/env python3
"""research-evidence-bundle-v1: canonical serialization, builder,
freeze-and-verify with an immediate pre-review/pre-promotion TOCTOU rehash.

Design contract (from ARCHITECTURE-AND-IMPLEMENTATION-PLAN.md "Evidence bundle
v1" and implementation tasks 5):

  * a bundle is a plain dict with the required top-level fields;
  * ``canonical_serialize`` produces a deterministic byte string that is
    independent of dict key insertion order but preserves list order (list order
    is semantic), and *excludes* the volatile identity fields ``bundle_sha256``
    and ``freeze_receipt`` so the hash binds the body, not itself;
  * ``build_bundle`` validates required fields and referential integrity (every
    claim evidence ref resolves in one hop to a manifest artifact) without
    mutating its input;
  * ``freeze_and_verify`` rehashes every manifest artifact from disk, seals the
    bundle by computing ``bundle_sha256`` over the canonical body, and records a
    ``freeze_receipt``;
  * ``verify_frozen_bundle`` performs the immediate TOCTOU rehash: it recomputes
    the bundle hash AND rehashes every manifest artifact from disk, returning
    ``blocked_stale_hash`` if either the bundle body or any artifact byte changed
    after freeze.

Pure and read-only apart from the artifact files the caller already wrote; this
module never mutates Linear, export, source registry, or runtime state.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "research-evidence-bundle-v1"

REQUIRED_FIELDS = (
    "schema_version",
    "campaign_id",
    "decision_question",
    "target_type",
    "created_at",
    "source_envelopes",
    "claims",
    "contradictions",
    "unknowns",
    "requirements",
    "candidate_implications",
    "artifact_manifest",
)

# Fields excluded from the canonical body so the hash binds the body, not itself.
_IDENTITY_FIELDS = ("bundle_sha256", "freeze_receipt")


class BundleValidationError(ValueError):
    """Raised when a bundle fails schema/referential validation at build time."""


class BundleFreezeError(RuntimeError):
    """Raised when freeze-time manifest rehash disagrees with recorded hashes."""


# --------------------------------------------------------------------------- #
# Canonical serialization                                                      #
# --------------------------------------------------------------------------- #

def _canonical_body(bundle: dict) -> dict:
    return {k: v for k, v in bundle.items() if k not in _IDENTITY_FIELDS}


def canonical_serialize(bundle: dict) -> bytes:
    """Deterministic bytes: sorted keys (order-independent), preserved list order,
    excluding volatile identity fields. UTF-8, no ASCII escaping, compact."""
    body = _canonical_body(bundle)
    return json.dumps(
        body,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def compute_bundle_sha256(bundle: dict) -> str:
    return hashlib.sha256(canonical_serialize(bundle)).hexdigest()


# --------------------------------------------------------------------------- #
# Disk hashing                                                                 #
# --------------------------------------------------------------------------- #

def _sha256_file(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _confined(root: Path, rel: str) -> tuple[Path, str | None]:
    r = Path(str(rel or "").replace("\\", "/"))
    if not str(rel) or r.is_absolute() or ".." in r.parts:
        return root / "__invalid__", f"unsafe_manifest_path {rel!r}"
    root_resolved = root.resolve()
    current = root
    for part in r.parts:
        current = current / part
        if current.is_symlink():
            return current, f"symlink_manifest_path {rel}"
    candidate = root / r
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError:
        return candidate, f"manifest_path_escape {rel}"
    return candidate, None


# --------------------------------------------------------------------------- #
# Builder                                                                      #
# --------------------------------------------------------------------------- #

def _manifest_paths(bundle: dict) -> set[str]:
    return {
        a["path"]
        for a in bundle.get("artifact_manifest", [])
        if isinstance(a, dict) and isinstance(a.get("path"), str)
    }


def build_bundle(raw: dict, *, root: Path, verify_manifest: bool = True) -> dict:
    """Validate a raw bundle and return a normalized copy (no input mutation)."""
    if not isinstance(raw, dict):
        raise BundleValidationError("bundle must be an object")
    bundle = copy.deepcopy(raw)

    for field in REQUIRED_FIELDS:
        if field not in bundle:
            raise BundleValidationError(f"missing required field: {field}")
    if bundle["schema_version"] != SCHEMA_VERSION:
        raise BundleValidationError(
            f"schema_version must be {SCHEMA_VERSION!r}, got {bundle['schema_version']!r}"
        )
    for list_field in ("source_envelopes", "claims", "contradictions", "unknowns",
                       "requirements", "candidate_implications", "artifact_manifest"):
        if not isinstance(bundle[list_field], list):
            raise BundleValidationError(f"{list_field} must be a list")

    manifest_paths = _manifest_paths(bundle)

    # Referential integrity: every claim evidence ref resolves in one hop.
    for claim in bundle["claims"]:
        if not isinstance(claim, dict) or "claim_id" not in claim:
            raise BundleValidationError("each claim requires a claim_id")
        for ref in claim.get("evidence_refs", []):
            if ref not in manifest_paths:
                raise BundleValidationError(
                    f"claim {claim['claim_id']} references evidence {ref!r} "
                    "not present in artifact_manifest"
                )

    # Manifest artifacts must exist and (optionally) match recorded hashes.
    for art in bundle["artifact_manifest"]:
        if not isinstance(art, dict) or "path" not in art or "sha256" not in art:
            raise BundleValidationError("each manifest entry requires path and sha256")
        path, unsafe = _confined(root, art["path"])
        if unsafe:
            raise BundleValidationError(unsafe)
        if verify_manifest:
            disk = _sha256_file(path)
            if disk is None:
                raise BundleValidationError(f"manifest artifact missing on disk: {art['path']}")
            if disk != art["sha256"]:
                raise BundleValidationError(
                    f"manifest hash mismatch at build for {art['path']}"
                )

    bundle.pop("bundle_sha256", None)
    bundle.pop("freeze_receipt", None)
    return bundle


# --------------------------------------------------------------------------- #
# Freeze + verify (TOCTOU close)                                               #
# --------------------------------------------------------------------------- #

def freeze_and_verify(bundle: dict, *, root: Path, frozen_at: str = "freeze") -> dict:
    """Rehash every manifest artifact from disk, seal the bundle, record receipt.

    Raises ``BundleFreezeError`` if any artifact's on-disk hash disagrees with its
    recorded manifest hash at freeze time.
    """
    sealed = copy.deepcopy(bundle)
    sealed.pop("bundle_sha256", None)
    sealed.pop("freeze_receipt", None)

    verified_artifacts = []
    for art in sealed.get("artifact_manifest", []):
        path, unsafe = _confined(root, art["path"])
        if unsafe:
            raise BundleFreezeError(unsafe)
        disk = _sha256_file(path)
        if disk is None:
            raise BundleFreezeError(f"manifest artifact missing at freeze: {art['path']}")
        if disk != art["sha256"]:
            raise BundleFreezeError(
                f"manifest hash mismatch at freeze for {art['path']} "
                f"(recorded {art['sha256']}, disk {disk})"
            )
        verified_artifacts.append({"path": art["path"], "sha256": disk})

    bundle_sha = compute_bundle_sha256(sealed)
    sealed["bundle_sha256"] = bundle_sha
    sealed["freeze_receipt"] = {
        "status": "frozen",
        "frozen_at": frozen_at,
        "bundle_sha256": bundle_sha,
        "manifest_verified": True,
        "verified_artifacts": verified_artifacts,
    }
    return sealed


def verify_frozen_bundle(bundle: dict, *, root: Path) -> dict:
    """Immediate pre-review/pre-promotion TOCTOU rehash.

    Recomputes the bundle body hash AND rehashes every manifest artifact from
    disk. Returns ``{"status": "verified"}`` only when the bundle body and every
    artifact byte are unchanged since freeze; otherwise ``blocked_stale_hash``
    with specific reasons.
    """
    reasons: list[str] = []
    recorded = bundle.get("bundle_sha256")
    if not recorded:
        return {"status": "blocked_stale_hash", "reasons": ["bundle is not frozen (no bundle_sha256)"]}

    recomputed = compute_bundle_sha256(bundle)
    if recomputed != recorded:
        reasons.append(
            f"bundle_sha256 drift: recorded {recorded} but body now hashes to {recomputed}"
        )

    receipt = bundle.get("freeze_receipt") or {}
    frozen_map = {a["path"]: a["sha256"] for a in receipt.get("verified_artifacts", [])}
    for art in bundle.get("artifact_manifest", []):
        path, unsafe = _confined(root, art["path"])
        if unsafe:
            reasons.append(unsafe)
            continue
        disk = _sha256_file(path)
        expected = frozen_map.get(art["path"], art.get("sha256"))
        if disk is None:
            reasons.append(f"manifest artifact missing at verify: {art['path']}")
        elif disk != expected:
            reasons.append(
                f"artifact byte drift for {art['path']} (frozen {expected}, disk {disk})"
            )

    if reasons:
        return {"status": "blocked_stale_hash", "reasons": reasons}
    return {"status": "verified", "reasons": [], "bundle_sha256": recorded}


def bundle_reference(bundle: dict) -> str | None:
    """The hash an output/reviewer must bind to."""
    return bundle.get("bundle_sha256")


__all__ = [
    "SCHEMA_VERSION",
    "REQUIRED_FIELDS",
    "BundleValidationError",
    "BundleFreezeError",
    "canonical_serialize",
    "compute_bundle_sha256",
    "build_bundle",
    "freeze_and_verify",
    "verify_frozen_bundle",
    "bundle_reference",
]
