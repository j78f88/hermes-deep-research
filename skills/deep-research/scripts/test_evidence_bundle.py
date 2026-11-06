#!/usr/bin/env python3
"""RED-first tests for ISSUE-569 research-evidence-bundle-v1.

Covers canonical serialization determinism, builder, freeze_and_verify() with an
immediate pre-review/pre-promotion TOCTOU rehash, and exact manifest/hash binding.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


eb = _load("evidence_bundle")


def _artifact(tmp_path: Path, rel: str, body: bytes) -> tuple[str, str]:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return rel, hashlib.sha256(body).hexdigest()


def _minimal_bundle(tmp_path: Path) -> dict:
    rel, sha = _artifact(tmp_path, "evidence/a.md", b"canonical evidence body\n")
    return {
        "schema_version": "research-evidence-bundle-v1",
        "campaign_id": "camp-001",
        "decision_question": "Should the owner build X?",
        "target_type": "architecture",
        "created_at": "2026-09-11T00:00:00+00:00",
        "source_envelopes": [{"source_id": "s1", "source_version": "v1", "lineage_group": "lg1"}],
        "claims": [
            {"claim_id": "c1", "evidence_refs": ["evidence/a.md"], "evidence_class": "primary",
             "confidence": "high", "caveats": ["scope-limited"]},
        ],
        "contradictions": [],
        "unknowns": [],
        "requirements": [],
        "candidate_implications": [{"text": "consider extend", "accepted": False}],
        "artifact_manifest": [
            {"path": rel, "size": 24, "sha256": sha, "privacy_class": "internal"},
        ],
    }


# --- canonical serialization -------------------------------------------------

def test_canonical_serialization_is_key_order_independent(tmp_path):
    b1 = _minimal_bundle(tmp_path)
    b2 = json.loads(json.dumps(b1))
    # Reverse the key insertion order of the top-level dict and a nested claim.
    b2 = {k: b2[k] for k in reversed(list(b2.keys()))}
    b2["claims"][0] = {k: b2["claims"][0][k] for k in reversed(list(b2["claims"][0].keys()))}
    assert eb.canonical_serialize(b1) == eb.canonical_serialize(b2)


def test_canonical_serialization_preserves_list_order(tmp_path):
    b = _minimal_bundle(tmp_path)
    b["claims"].append({"claim_id": "c2", "evidence_refs": ["evidence/a.md"],
                        "evidence_class": "primary", "confidence": "low", "caveats": []})
    reordered = copy.deepcopy(b)
    reordered["claims"] = list(reversed(reordered["claims"]))
    # List order is semantic -> different canonical form.
    assert eb.canonical_serialize(b) != eb.canonical_serialize(reordered)


def test_canonical_hash_excludes_bundle_sha_and_freeze_receipt(tmp_path):
    b = _minimal_bundle(tmp_path)
    h1 = eb.compute_bundle_sha256(b)
    polluted = dict(b, bundle_sha256="deadbeef", freeze_receipt={"frozen_at": "x"})
    h2 = eb.compute_bundle_sha256(polluted)
    assert h1 == h2


# --- builder -----------------------------------------------------------------

def test_build_bundle_rejects_claim_with_unknown_evidence_ref(tmp_path):
    b = _minimal_bundle(tmp_path)
    b["claims"][0]["evidence_refs"] = ["evidence/missing.md"]
    with pytest.raises(eb.BundleValidationError):
        eb.build_bundle(b, root=tmp_path)


def test_build_bundle_rejects_missing_required_field(tmp_path):
    b = _minimal_bundle(tmp_path)
    del b["decision_question"]
    with pytest.raises(eb.BundleValidationError):
        eb.build_bundle(b, root=tmp_path)


def test_build_bundle_does_not_mutate_input(tmp_path):
    b = _minimal_bundle(tmp_path)
    before = copy.deepcopy(b)
    eb.build_bundle(b, root=tmp_path)
    assert b == before


# --- freeze_and_verify -------------------------------------------------------

def test_freeze_and_verify_seals_and_binds_hashes(tmp_path):
    b = eb.build_bundle(_minimal_bundle(tmp_path), root=tmp_path)
    frozen = eb.freeze_and_verify(b, root=tmp_path)
    assert frozen["bundle_sha256"] == eb.compute_bundle_sha256(frozen)
    assert frozen["freeze_receipt"]["status"] == "frozen"
    assert frozen["freeze_receipt"]["manifest_verified"] is True
    # every manifest artifact hash matches the on-disk file
    for art in frozen["artifact_manifest"]:
        p = tmp_path / art["path"]
        assert hashlib.sha256(p.read_bytes()).hexdigest() == art["sha256"]


def test_freeze_detects_manifest_hash_mismatch_at_freeze_time(tmp_path):
    raw = _minimal_bundle(tmp_path)
    raw["artifact_manifest"][0]["sha256"] = "0" * 64  # wrong recorded hash
    b = eb.build_bundle(raw, root=tmp_path, verify_manifest=False)
    with pytest.raises(eb.BundleFreezeError):
        eb.freeze_and_verify(b, root=tmp_path)


def test_toctou_rehash_detects_byte_mutation_after_freeze(tmp_path):
    """The core TOCTOU close: a byte mutation of a manifest artifact AFTER freeze
    must be caught by the immediate pre-review/pre-promotion rehash."""
    b = eb.build_bundle(_minimal_bundle(tmp_path), root=tmp_path)
    frozen = eb.freeze_and_verify(b, root=tmp_path)
    # verify passes right after freeze
    ok = eb.verify_frozen_bundle(frozen, root=tmp_path)
    assert ok["status"] == "verified"
    # attacker mutates the artifact bytes on disk
    art_path = tmp_path / frozen["artifact_manifest"][0]["path"]
    art_path.write_bytes(b"canonical evidence body TAMPERED\n")
    tampered = eb.verify_frozen_bundle(frozen, root=tmp_path)
    assert tampered["status"] == "blocked_stale_hash"
    assert any("evidence/a.md" in r for r in tampered["reasons"])


def test_toctou_rehash_detects_bundle_body_mutation(tmp_path):
    b = eb.build_bundle(_minimal_bundle(tmp_path), root=tmp_path)
    frozen = eb.freeze_and_verify(b, root=tmp_path)
    # mutate a claim's confidence after freeze; bundle_sha256 no longer matches
    tampered = copy.deepcopy(frozen)
    tampered["claims"][0]["confidence"] = "low"
    result = eb.verify_frozen_bundle(tampered, root=tmp_path)
    assert result["status"] == "blocked_stale_hash"
    assert any("bundle_sha256" in r for r in result["reasons"])


def test_output_bound_to_frozen_bundle_hash(tmp_path):
    b = eb.build_bundle(_minimal_bundle(tmp_path), root=tmp_path)
    frozen = eb.freeze_and_verify(b, root=tmp_path)
    assert eb.bundle_reference(frozen) == frozen["bundle_sha256"]
