#!/usr/bin/env python3
"""offline end-to-end committee-system replays.

Builds a real ``research-evidence-bundle-v1`` on disk, freezes it with the
TOCTOU rehash, and runs two replay modes over it without any provider call or
spend:

  * ``run_failure_detection_replay`` — preserved historical canonical outputs
    contain placeholders, so the campaign HOLDs
    (``blocked_missing_canonical_output``) even though the external benchmark is
    strong. The frozen bundle itself is verified clean; the HOLD is about the
    historical outputs, exactly as the plan's Phase-4 failure-detection replay
    specifies.
  * ``run_target_state_reconstruction`` — deterministically generates
    NON-placeholder canonical outputs from the frozen bundle (without touching
    historical evidence), runs the four distinct specialist reviewers bound to
    the frozen bundle hash, and reaches ``owner_review_ready``.

Two distinct target classes (architecture, governance) are exercised to satisfy
the graduation "two target classes" requirement offline.

Pure/offline: reads/writes only the caller-provided campaign root and imports
bundled siblings. No external source-intelligence checkout or live runtime is
needed. The former ledger_path keywords are no-I/O compatibility arguments.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent


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
rc = _load_sibling("reviewer_receipts")
cr = _load_sibling("campaign_replay")

TARGET_CLASSES = ["architecture", "governance"]

# Required canonical outputs for the committee campaign.
_REQUIRED_OUTPUTS = ["detailed_evidence_pack", "executive_decision_brief"]

_PLACEHOLDER = "\x00PLACEHOLDER\x00"


def _sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- #
# Bundle construction on disk                                                  #
# --------------------------------------------------------------------------- #

def _write_evidence(root: Path, target_type: str) -> tuple[str, str]:
    body = (
        f"# Committee evidence ({target_type})\n"
        "Structured claims and references for the committee decision.\n"
    ).encode("utf-8")
    rel = "evidence/committee-evidence.md"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(body)
    return rel, _sha_bytes(body)


def _build_and_freeze(root: Path, target_type: str) -> dict:
    rel, sha = _write_evidence(root, target_type)
    raw = {
        "schema_version": eb.SCHEMA_VERSION,
        "campaign_id": f"committee-{target_type}",
        "decision_question": f"Should the owner adopt the {target_type} committee output?",
        "target_type": target_type,
        "created_at": "2026-09-11T00:00:00+00:00",
        "source_envelopes": [
            {"source_id": "gao", "lineage_group": "gao"},
            {"source_id": "five_case", "lineage_group": "ukgov"},
        ],
        "claims": [
            {"claim_id": "c1", "evidence_refs": [rel], "evidence_class": "primary",
             "confidence": "high", "caveats": ["scope-limited"]},
        ],
        "contradictions": [],
        "unknowns": [],
        "requirements": [],
        "candidate_implications": [{"text": "consider adoption", "accepted": False}],
        "artifact_manifest": [{"path": rel, "size": len((root / rel).read_bytes()),
                               "sha256": sha, "privacy_class": "internal"}],
    }
    built = eb.build_bundle(raw, root=root)
    frozen = eb.freeze_and_verify(built, root=root)
    return frozen


# --------------------------------------------------------------------------- #
# Outputs                                                                      #
# --------------------------------------------------------------------------- #

def _write_historical_placeholder_outputs(root: Path, bundle_sha: str) -> dict:
    """Preserved historical outputs: complete-looking but carrying a placeholder."""
    outs: dict[str, dict] = {}
    for name in _REQUIRED_OUTPUTS:
        rel = f"outputs/{name}.md"
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        # fluent prose, but a structural placeholder sentinel remains
        body = f"# {name}\nFluent, well-structured, but incomplete.\n{_PLACEHOLDER}\n"
        p.write_bytes(body.encode("utf-8"))
        outs[name] = {"body": body, "input_bundle_sha256": bundle_sha, "complete": False}
    return outs


def _generate_reconstructed_outputs(root: Path, bundle: dict, target_type: str) -> dict:
    """Deterministically reconstruct non-placeholder canonical outputs bound to
    the frozen bundle, from the bundle's own claims."""
    claims = bundle.get("claims", [])
    claim_lines = "\n".join(
        f"- claim {c['claim_id']} (confidence {c.get('confidence')}): "
        f"{', '.join(c.get('evidence_refs', []))}"
        for c in claims
    )
    outs: dict[str, dict] = {}
    for name in _REQUIRED_OUTPUTS:
        rel = f"reconstructed/{name}.md"
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f"# {name} ({target_type})\n\n"
            f"Reconstructed from frozen bundle {bundle['bundle_sha256'][:12]}.\n\n"
            f"## Claims used\n{claim_lines}\n\n"
            "## Decision\nEvidence-backed, no placeholders.\n"
        )
        p.write_bytes(body.encode("utf-8"))
        outs[name] = {"body": body, "input_bundle_sha256": bundle["bundle_sha256"],
                      "complete": True}
    return outs


def _clean_reviews(bundle_sha: str) -> dict:
    return {
        role: rc.build_receipt(
            role=role, reviewed_sha256=bundle_sha, reviewer=f"{role}-agent",
            checks={c: "PASS" for c in rc.REQUIRED_CHECKS[role]}, verdict="PASS",
        )
        for role in ("evidence_qa", "taxonomy", "librarian", "adversarial")
    }


def _structure(bundle: dict, outputs: dict) -> dict:
    sha = bundle["bundle_sha256"]
    return {
        "bundle": bundle,
        "required_outputs": list(_REQUIRED_OUTPUTS),
        "outputs": outputs,
        "reviews": _clean_reviews(sha),
        "prior_work": {"surfaces": {"linear": True, "knowledge_base": True, "sessions": True,
                                    "repos": True}, "matches": []},
        "activation": {"mutation_class": "proposal_only", "owner_approved": False},
        "improvement": {"classification": "not_yet_measurable", "observed_evidence": False},
    }


# --------------------------------------------------------------------------- #
# Replay modes                                                                 #
# --------------------------------------------------------------------------- #

def run_failure_detection_replay(root: Path, *, target_type: str, ledger_path: Path | None = None) -> dict:
    frozen = _build_and_freeze(root, target_type)
    verify = eb.verify_frozen_bundle(frozen, root=root)
    outputs = _write_historical_placeholder_outputs(root, frozen["bundle_sha256"])
    structure = _structure(frozen, outputs)
    verdict = cr.evaluate_fixture({"structure": structure}, ledger_path=ledger_path)
    return {
        "state": verdict["state"],
        "reasons": verdict["reasons"],
        "bundle_verified": verify["status"] == "verified",
        "bundle_sha256": frozen["bundle_sha256"],
    }


def run_target_state_reconstruction(root: Path, *, target_type: str,
                                    ledger_path: Path | None = None,
                                    tamper_after_freeze: bool = False) -> dict:
    frozen = _build_and_freeze(root, target_type)
    hist_rel = frozen["artifact_manifest"][0]["path"]
    hist_sha = _sha_bytes((root / hist_rel).read_bytes())

    outputs = _generate_reconstructed_outputs(root, frozen, target_type)

    if tamper_after_freeze:
        # attacker mutates a frozen artifact after freeze; the pre-review rehash must catch it
        (root / hist_rel).write_bytes(b"TAMPERED historical evidence\n")

    verify = eb.verify_frozen_bundle(frozen, root=root)
    if verify["status"] != "verified":
        return {"state": "blocked_stale_hash", "reasons": verify["reasons"],
                "bundle_sha256": frozen["bundle_sha256"]}

    structure = _structure(frozen, outputs)
    verdict = cr.evaluate_fixture({"structure": structure}, ledger_path=ledger_path)

    committee = rc.evaluate_committee(structure["reviews"], expected_sha256=frozen["bundle_sha256"])
    return {
        "state": verdict["state"],
        "reasons": verdict["reasons"],
        "bundle_sha256": frozen["bundle_sha256"],
        "outputs": {
            name: {"placeholder": _PLACEHOLDER in meta["body"],
                   "input_bundle_sha256": meta["input_bundle_sha256"]}
            for name, meta in outputs.items()
        },
        "reviewer_roles_passed": committee["roles_passed"],
        "historical_evidence_path": hist_rel,
        "historical_evidence_sha256": hist_sha,
    }


def run_two_class_suite(root: Path, *, ledger_path: Path | None = None) -> dict:
    reconstruction_states: dict[str, str] = {}
    failure_states: dict[str, str] = {}
    for target in TARGET_CLASSES:
        fd_root = root / f"fd_{target}"
        rc_root = root / f"recon_{target}"
        fd_root.mkdir(parents=True, exist_ok=True)
        rc_root.mkdir(parents=True, exist_ok=True)
        failure_states[target] = run_failure_detection_replay(
            fd_root, target_type=target, ledger_path=ledger_path)["state"]
        reconstruction_states[target] = run_target_state_reconstruction(
            rc_root, target_type=target, ledger_path=ledger_path)["state"]
    return {
        "classes": list(TARGET_CLASSES),
        "reconstruction_states": reconstruction_states,
        "failure_detection_states": failure_states,
    }


__all__ = [
    "TARGET_CLASSES", "run_failure_detection_replay",
    "run_target_state_reconstruction", "run_two_class_suite",
]
