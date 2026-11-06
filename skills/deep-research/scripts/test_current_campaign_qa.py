#!/usr/bin/env python3
"""Self-contained portable current-campaign regressions; no saved lane required."""
from pathlib import Path
import hashlib
import importlib.util
import json
import subprocess
import sys

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('round2_qa_controls', HERE / 'research_qa_repair.py')
qa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qa)


def put(root, rel, value):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n' if isinstance(value, dict) else value)


def digest(root, rel):
    return hashlib.sha256((root / rel).read_bytes()).hexdigest()


def fixture(root):
    from test_acceptance_repairs import accepted
    _, review, decisions = accepted(root, 'A shorter heading is optional.')
    review['replay_mode'] = 'current_campaign'
    put(root, 'qa/plain-brief-review.json', review)
    from publication_test_support import bind_fixture_review_packet
    bind_fixture_review_packet(root)
    return review, decisions


def run(root, *args):
    return subprocess.run([sys.executable, str(HERE / 'run_publication_reviews.py'),
                           '--campaign-root', str(root), *args], capture_output=True, text=True)


def test_current_campaign_replay_does_not_invent_sovereign_history(tmp_path):
    fixture(tmp_path)
    result = qa.replay_saved(tmp_path)
    assert result['status'] == 'PASS'
    assert result['replay_kind'] == 'current_campaign'
    assert len(result['rows']) == 4
    assert all(row['pass'] for row in result['rows'])
    assert result['publication_approval'] is False
    assert qa.verify_review_packet(result['input_packet'], root=tmp_path)['status'] == 'PASS'
    assert not (tmp_path / 'receipts/qa-history').exists()


@pytest.mark.parametrize('rel', ['internal/plain-brief.md', 'internal/reference-pack.md', 'source.md'])
def test_changed_current_inputs_block_replay_and_runner(tmp_path, rel):
    fixture(tmp_path)
    put(tmp_path, rel, 'Changed bytes\n')
    result = qa.replay_saved(tmp_path)
    assert result['status'] == 'BLOCKED_HISTORICAL_INPUTS'
    assert result['rows'] == []
    proc = run(tmp_path, '--preflight-only')
    assert proc.returncode == 2, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload['status'] == 'BLOCKED_CAMPAIGN_READINESS'
    assert 'pending review inputs changed or incomplete' in ' '.join(payload['findings'])
    assert rel in ' '.join(payload['findings'])
    assert not (tmp_path / 'qa/inputs/publication-review-inputs.json').exists()


@pytest.mark.parametrize('field', ['brief_sha256', 'full_pack_sha256'])
def test_changed_current_decision_binding_blocks(tmp_path, field):
    _, decisions = fixture(tmp_path)
    decisions[field] = '0' * 64
    put(tmp_path, 'qa/editor-note-decisions.json', decisions)
    assert qa.replay_saved(tmp_path)['status'] == 'BLOCKED_HISTORICAL_INPUTS'
    assert run(tmp_path, '--preflight-only').returncode == 2


def test_current_mode_cannot_override_incomplete_historical_backup(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'orchestration/pass1-backup').mkdir(parents=True)
    result = qa.replay_saved(tmp_path)
    assert result['status'] == 'BLOCKED_HISTORICAL_INPUTS'
    assert result['historical_selection'] == 'preserved_pass1'


def test_current_mode_cannot_override_existing_generation_history(tmp_path):
    fixture(tmp_path)
    (tmp_path / 'receipts/qa-history').mkdir(parents=True)
    result = qa.replay_saved(tmp_path)
    assert result['status'] == 'BLOCKED_HISTORICAL_INPUTS'
    assert 'replay_kind' not in result


def test_preflight_only_rejects_command_without_starting_it(tmp_path):
    fixture(tmp_path)
    put(tmp_path, 'marker.py', "from pathlib import Path\nPath('started').write_text('yes')\n")
    proc = run(tmp_path, '--preflight-only', '--command', sys.executable, 'marker.py')
    assert proc.returncode == 2
    assert '--preflight-only cannot be combined' in proc.stderr
    assert not (tmp_path / 'started').exists()


def test_preflight_only_binds_mandatory_inputs_and_evidence(tmp_path):
    fixture(tmp_path)
    proc = run(tmp_path, '--preflight-only', '--input', 'internal/plain-brief.md')
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result['publication_qa_started'] is False
    packet = json.loads((tmp_path / result['input_packet']).read_text())
    assert {item['path'] for item in packet['files']} == {
        'internal/plain-brief.md', 'internal/reference-pack.md', 'source.md', 'qa/disposition.json', 'manifest.json', 'qa/plain-brief-review.json', 'qa/editor-note-decisions.json'}


def test_runner_checks_evidence_after_command_even_when_not_explicit_input(tmp_path):
    fixture(tmp_path)
    put(tmp_path, 'mutate.py', "from pathlib import Path\nPath('source.md').write_text('changed')\n")
    proc = run(tmp_path, '--command', sys.executable, 'mutate.py')
    assert proc.returncode == 2, proc.stderr
    result = json.loads(proc.stdout)
    assert result['status'] == 'BLOCKED_CHANGED_INPUT'
    assert result['publication_qa_started'] is True


def test_missing_current_evidence_blocks(tmp_path):
    _, decisions = fixture(tmp_path)
    decisions['decisions'][0]['evidence_refs'] = ['absent.md']
    put(tmp_path, 'qa/editor-note-decisions.json', decisions)
    assert qa.replay_saved(tmp_path)['status'] == 'BLOCKED_HISTORICAL_INPUTS'
    assert run(tmp_path, '--preflight-only').returncode == 2


def test_current_replay_without_explicit_mode_cannot_silently_pass(tmp_path):
    review, _ = fixture(tmp_path)
    review.pop('replay_mode')
    put(tmp_path, 'qa/plain-brief-review.json', review)
    assert qa.replay_saved(tmp_path)['status'] == 'BLOCKED_HISTORICAL_INPUTS'
