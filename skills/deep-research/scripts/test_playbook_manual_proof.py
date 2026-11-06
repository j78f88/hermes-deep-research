from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "playbook_manual_proof.py"


def load_module():
    spec = importlib.util.spec_from_file_location("playbook_manual_proof", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manual_proof_covers_success_failure_stale_resume_and_rollback(tmp_path):
    module = load_module()
    result = module.run_proof(tmp_path)

    assert result["status"] == "PASS"
    assert result["proof_class"] == "offline_manual_isolated_runner"
    assert result["persistent_runtime_activated"] is False
    assert result["source_of_truth_writes"] is False
    assert len(result["fixture_results"]) == 14
    assert result["two_class_replay"]["classes"] == ["architecture", "governance"]
    assert set(result["two_class_replay"]["reconstruction_states"].values()) == {"owner_review_ready"}
    assert set(result["two_class_replay"]["failure_detection_states"].values()) == {"blocked_missing_canonical_output"}
    assert result["stale_replay"]["state"] == "blocked_stale_hash"
    assert result["resume_replay"]["outcome"] == "reuse_valid_stages_without_duplicate_output"
    assert result["rollback_proof"]["status"] == "PASS"
    assert result["graduation_verdict"]["dedicated_runtime_warranted"] is False

    receipt = json.loads((tmp_path / "stage569-manual-proof.json").read_text())
    assert receipt["status"] == "PASS"
    assert receipt["graduation_verdict"]["home_lab_live_import"] == "not_performed_due_fixed_line_security_posture"


def test_manual_proof_keeps_temporary_work_under_output_root(tmp_path, monkeypatch):
    module = load_module()
    original_mkdtemp = module.tempfile.mkdtemp
    created = []

    def bounded_mkdtemp(*args, **kwargs):
        assert Path(kwargs.get("dir", "")).resolve() == tmp_path.resolve()
        result = original_mkdtemp(*args, **kwargs)
        created.append(Path(result))
        return result

    monkeypatch.setattr(module.tempfile, "mkdtemp", bounded_mkdtemp)
    assert module.run_proof(tmp_path)["status"] == "PASS"
    assert created and all(not path.exists() for path in created)


def test_copied_replay_subset_runs_without_home_checkout_or_network(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    scripts = tmp_path / "portable-scripts"
    scripts.mkdir()
    for name in ("source_ledger", "source_assessment", "evidence_bundle",
                 "run_state_machine", "reviewer_receipts", "campaign_replay",
                 "committee_replay", "playbook_manual_proof"):
        shutil.copy2(SCRIPT.parent / f"{name}.py", scripts)
    home = tmp_path / "empty-home"
    home.mkdir()
    environment = {
        "PATH": os.defpath, "HOME": str(home), "TMPDIR": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "SOURCE_INTELLIGENCE_ROOT": str(tmp_path / "missing-external-checkout"),
    }
    code = r'''
import importlib.util, json, pathlib, runpy, socket, sys

def offline(*args, **kwargs):
    raise AssertionError("offline replay attempted network access")

socket.socket.connect = offline
socket.socket.connect_ex = offline
socket.create_connection = offline
socket.getaddrinfo = offline
scripts = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("source_assessment", scripts / "source_assessment.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
event = module.SourceAssessmentEvent(
    source_id="s1", source_version="v1", dimension="usefulness", old_value=None,
    new_value="candidate", evidence_refs=["e1"], assessor="qa", method_version="m1",
    observed_at="2026-09-11T00:00:00+00:00", reason="new signal",
    lifecycle_transition=True, no_action_reason="await corroboration")
assert module.project_through_ledger([event], case_id="s1")["s1"].state == "candidate"
sys.argv = [str(scripts / "playbook_manual_proof.py"), sys.argv[2]]
runpy.run_path(sys.argv[0], run_name="__main__")
'''
    output = tmp_path / "proof-output"
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", code, str(scripts), str(output)],
        cwd=home, env=environment, capture_output=True, text=True, check=True,
    )
    returned = json.loads(result.stdout)
    saved_path = output / "stage569-manual-proof.json"
    saved = json.loads(saved_path.read_text())
    assert returned["status"] == saved["status"] == "PASS"
    assert returned["fixture_results"] == saved["fixture_results"]
    import hashlib
    assert returned["receipt_sha256"] == hashlib.sha256(saved_path.read_bytes()).hexdigest()
    assert not list(scripts.rglob("__pycache__"))
