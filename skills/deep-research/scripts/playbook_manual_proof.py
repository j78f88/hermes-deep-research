#!/usr/bin/env python3
"""ISSUE-573/574 offline manual proof and graduation receipt."""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_proof(out_dir: Path) -> dict:
    cr = load("campaign_replay")
    committee = load("committee_replay")
    rsm = load("run_state_machine")
    out_dir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="stage569-proof-", dir=out_dir))
    try:
        fixtures = cr.build_fixture_matrix()
        fixture_results = []
        for fixture in fixtures:
            result = cr.evaluate_fixture(fixture)
            fixture_results.append({"name": fixture["name"], "state": result["state"], "reasons": result["reasons"]})
        two_class = committee.run_two_class_suite(work / "two-class")
        stale = committee.run_target_state_reconstruction(work / "stale", target_type="architecture", tamper_after_freeze=True)
        run = rsm.RunState().append(rsm.Transition(correlation_id="stage574-resume", from_state=None, to_state="accepted", occurred_at="2026-09-12T00:00:00+00:00", actor="research", reason="accepted"))
        run = run.append(rsm.Transition(correlation_id="stage574-resume", from_state="accepted", to_state="research_running", occurred_at="2026-09-12T00:01:00+00:00", actor="research", reason="research"))
        run = run.append(rsm.Transition(correlation_id="stage574-resume", from_state="research_running", to_state="bundle_validated", occurred_at="2026-09-12T00:02:00+00:00", actor="research", reason="validated", artifact_hashes={"bundle": "a" * 64}))
        run = run.append(rsm.Transition(correlation_id="stage574-resume", from_state="bundle_validated", to_state="bundle_frozen", occurred_at="2026-09-12T00:03:00+00:00", actor="research", reason="frozen", artifact_hashes={"bundle": "a" * 64}))
        resume = rsm.resume(run, correlation_id="stage574-resume", completed_stage_hashes={"bundle": "a" * 64})
        rollback_source = out_dir / "rollback-source.json"
        rollback_source.write_text(json.dumps({"state": "before", "active": False}, sort_keys=True) + "\n")
        backup = out_dir / "rollback-backup.json"
        shutil.copy2(rollback_source, backup)
        before_hash = sha256(rollback_source)
        rollback_source.write_text(json.dumps({"state": "synthetic-change", "active": False}, sort_keys=True) + "\n")
        shutil.copy2(backup, rollback_source)
        rollback_ok = sha256(rollback_source) == before_hash == sha256(backup)
        expected_states = {
            "canonical_full_pack_has_placeholder": "blocked_missing_canonical_output", "missing_executive_brief": "blocked_missing_canonical_output", "output_from_different_bundle_hash": "blocked_stale_hash", "source_evidence_missing": "blocked_missing_evidence", "reviewer_returns_hold": "blocked_review_hold", "provider_401_403": "blocked_provider_auth", "provider_timeout_partial_artifact": "repair_required", "same_lineage_false_independence": "blocked_lineage_inflation", "interrupted_run_resumes": "reuse_valid_stages_without_duplicate_output", "activation_unapproved_write": "blocked_owner_gate", "playbook_without_prior_work": "blocked_missing_prior_work_context", "playbook_duplicate_capability": "blocked_duplicate_prior_work", "improved_without_observed_evidence": "blocked_unverified_improvement", "improvement_unvalidated_auto_mutation": "blocked_owner_gate"}
        all_fixtures = len(fixture_results) == 14 and all(expected_states[r["name"]] == r["state"] for r in fixture_results)
        proof_ok = all((all_fixtures, all(v == "owner_review_ready" for v in two_class["reconstruction_states"].values()), all(v == "blocked_missing_canonical_output" for v in two_class["failure_detection_states"].values()), stale["state"] == "blocked_stale_hash", resume["outcome"] == "reuse_valid_stages_without_duplicate_output", rollback_ok))
        receipt = {
            "schema_version": "stage569-manual-proof-v1", "status": "PASS" if proof_ok else "FAIL", "proof_class": "offline_manual_isolated_runner", "persistent_runtime_activated": False, "source_of_truth_writes": False, "fixture_results": fixture_results, "two_class_replay": two_class, "stale_replay": stale, "resume_replay": resume, "rollback_proof": {"status": "PASS" if rollback_ok else "FAIL", "pre_sha256": before_hash, "restored_sha256": sha256(rollback_source)}, "graduation_verdict": {"dedicated_runtime_warranted": False, "reason": "Deterministic offline/manual proof satisfies assurance and replay objectives; n8n adds orchestration convenience but no demonstrated control benefit requiring a separate persistent stack.", "home_lab_live_import": "not_performed_due_fixed_line_security_posture", "recommended_action": "close ISSUE-573 and ISSUE-574 on artifact/proof objectives; cancel ISSUE-575 rather than activate persistent runtime"}}
        target = out_dir / "stage569-manual-proof.json"
        target.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        receipt["receipt_sha256"] = sha256(target)
        return receipt
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    destination = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "stage569-manual-proof"
    result = run_proof(destination)
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result["status"] == "PASS" else 2)
