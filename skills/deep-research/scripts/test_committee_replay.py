#!/usr/bin/env python3
"""RED-first tests for ISSUE-569 offline end-to-end committee-system replays.

Two modes over a real frozen evidence bundle written to disk (offline, no
provider/spend):

  1. failure-detection replay over PRESERVED historical outputs that contain
     placeholders -> must HOLD (blocked_missing_canonical_output) even though the
     external benchmark is strong;
  2. target-state reconstruction replay that generates NON-placeholder canonical
     outputs from the frozen bundle without altering historical evidence ->
     reaches owner_review_ready with all four distinct reviewer receipts PASS.

Run for two distinct target classes (architecture, governance) to exercise the
graduation "two target classes" requirement offline.
"""
from __future__ import annotations

import hashlib
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


e2e = _load("committee_replay")


@pytest.mark.parametrize("target_type", ["architecture", "governance"])
def test_failure_detection_replay_holds_on_preserved_placeholders(tmp_path, target_type):
    result = e2e.run_failure_detection_replay(tmp_path, target_type=target_type)
    assert result["state"] == "blocked_missing_canonical_output", result
    # the frozen bundle itself is valid; the HOLD is about the historical outputs
    assert result["bundle_verified"] is True


@pytest.mark.parametrize("target_type", ["architecture", "governance"])
def test_target_state_reconstruction_creates_non_placeholder_outputs(tmp_path, target_type):
    result = e2e.run_target_state_reconstruction(tmp_path, target_type=target_type)
    assert result["state"] == "owner_review_ready", result
    # every generated canonical output is non-placeholder and bound to the frozen bundle
    for name, meta in result["outputs"].items():
        assert meta["placeholder"] is False, name
        assert meta["input_bundle_sha256"] == result["bundle_sha256"], name
    # all four distinct reviewer roles passed against the same hash
    assert set(result["reviewer_roles_passed"]) == {"evidence_qa", "taxonomy", "librarian", "adversarial"}


def test_reconstruction_does_not_alter_historical_evidence(tmp_path):
    result = e2e.run_target_state_reconstruction(tmp_path, target_type="architecture")
    # historical evidence file hash is unchanged after reconstruction
    hist = tmp_path / result["historical_evidence_path"]
    assert hashlib.sha256(hist.read_bytes()).hexdigest() == result["historical_evidence_sha256"]


def test_toctou_rehash_detects_post_freeze_tamper_in_e2e(tmp_path):
    """End-to-end proof that byte-mutating a frozen artifact after freeze is
    caught by the immediate pre-review rehash."""
    result = e2e.run_target_state_reconstruction(tmp_path, target_type="architecture",
                                                 tamper_after_freeze=True)
    assert result["state"] == "blocked_stale_hash", result


def test_two_target_classes_both_reconstruct_cleanly(tmp_path):
    summary = e2e.run_two_class_suite(tmp_path)
    assert summary["classes"] == ["architecture", "governance"]
    assert all(v == "owner_review_ready" for v in summary["reconstruction_states"].values())
    assert all(v == "blocked_missing_canonical_output" for v in summary["failure_detection_states"].values())
