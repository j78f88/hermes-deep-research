#!/usr/bin/env python3
"""New runner regressions. No runner test suite existed in the source snapshot."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

HERE = Path(__file__).resolve().parent
CANDIDATE = next((parent for parent in HERE.parents if parent.name == 'candidate'), None)
FIXTURE_ROOT = Path(os.environ.get('DEEP_RESEARCH_TEST_FIXTURE_ROOT', str(HERE / 'tests/fixtures')))


def copy_inputs(tmp_path: Path, campaign: str, *, repaired: bool = False) -> Path:
    # Explicit synthetic regressions, never optional private campaign records.
    source = HERE / 'fixtures/public-qa/synthetic-campaign'
    assert source.is_dir(), 'Required public regression fixture is missing'
    if campaign == 'workflow-adoption':
        from test_acceptance_repairs import accepted, put
        import research_qa_repair as qa
        root = tmp_path / campaign
        _, review, _ = accepted(root)
        review.update(fixture_only=True, publication_approval=False)
        put(root, 'qa/plain-brief-review.json', review)
        pending = qa.build_pending_editor_decisions(root, review=review, previous_decisions={})
        pending['status'] = 'reviewed'
        put(root, 'qa/editor-note-decisions.json', pending)
        return root
    root = tmp_path / campaign
    root.mkdir(parents=True, exist_ok=True)
    (root / 'manifest.json').write_text(json.dumps({'report_purpose': 'factual', 'output_layout': 'brief-and-reference-v2'}))
    rels = ['internal/plain-brief.md', 'internal/reference-pack.md', 'qa/plain-brief-review.json']
    decisions_rel = 'qa/editor-note-decisions-repaired.json' if repaired else 'qa/editor-note-decisions.json'
    decisions_source = HERE / 'fixtures/sovereign-decisions-repaired.json' if repaired else source / decisions_rel
    if repaired and not decisions_source.is_file():
        decisions_source = FIXTURE_ROOT / 'sovereign-decisions-repaired.json'
    decisions = json.loads(decisions_source.read_text())
    target_decisions = root / decisions_rel
    target_decisions.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(decisions_source, target_decisions)
    for item in decisions['decisions']:
        rels.extend(item.get('evidence_refs', []))
    for rel in dict.fromkeys(rels):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)
    return root


def run_runner(root: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HERE / 'run_publication_reviews.py'),
                           '--campaign-root', str(root), *extra],
                          capture_output=True, text=True, timeout=30)


def test_invalid_decisions_block_before_command_starts(tmp_path: Path) -> None:
    root = copy_inputs(tmp_path, 'sovereign')
    marker = root / 'command-started.txt'
    command = root / 'marker.py'
    command.write_text("from pathlib import Path\nPath('command-started.txt').write_text('started')\n")
    proc = run_runner(root, '--command', sys.executable, str(command))
    assert proc.returncode == 2, proc.stderr
    result = json.loads(proc.stdout)
    assert result['publication_qa_allowed'] is False
    assert result['findings']
    assert not marker.exists()
    assert not (root / 'qa/inputs/publication-review-inputs.json').exists()


def test_historical_repaired_labels_without_current_acceptance_are_blocked(tmp_path: Path) -> None:
    root = copy_inputs(tmp_path, 'sovereign', repaired=True)
    decisions = json.loads((root / 'qa/editor-note-decisions-repaired.json').read_text())
    inputs = ['internal/plain-brief.md', 'internal/reference-pack.md',
              'qa/plain-brief-review.json', 'qa/editor-note-decisions-repaired.json']
    inputs.extend(ref for item in decisions['decisions'] for ref in item['evidence_refs'])
    extra = ['--decisions', 'qa/editor-note-decisions-repaired.json']
    for rel in dict.fromkeys(inputs):
        extra.extend(['--input', rel])
    proc = run_runner(root, *extra)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result['publication_qa_allowed'] is False
    assert result['findings']
    assert not (root / 'qa/inputs/publication-review-inputs.json').exists()


def test_workflow_next_campaign_preflight_accepts_explicit_test_inputs(tmp_path: Path) -> None:
    root = copy_inputs(tmp_path, 'workflow-adoption')
    review = json.loads((root / 'qa/plain-brief-review.json').read_text())
    assert review['fixture_only'] is True
    assert review['publication_approval'] is False
    proc = run_runner(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result['status'] == 'PASS'
    assert result['publication_qa_started'] is False
    packet = json.loads((root / result['input_packet']).read_text())
    assert {item['path'] for item in packet['files']} == {
        'manifest.json', 'qa/plain-brief-review.json', 'qa/editor-note-decisions.json',
        'internal/plain-brief.md', 'internal/reference-pack.md'}


def test_stale_repaired_decisions_still_block(tmp_path: Path) -> None:
    root = copy_inputs(tmp_path, 'sovereign', repaired=True)
    path = root / 'qa/editor-note-decisions-repaired.json'
    payload = json.loads(path.read_text())
    payload['brief_sha256'] = '0' * 64
    path.write_text(json.dumps(payload))
    proc = run_runner(root, '--decisions', 'qa/editor-note-decisions-repaired.json')
    assert proc.returncode == 2
    assert json.loads(proc.stdout)['publication_qa_allowed'] is False


def test_runner_rejects_changed_input_after_local_command(tmp_path: Path) -> None:
    root = copy_inputs(tmp_path, 'workflow-adoption')
    command = root / 'change-input.py'
    command.write_text("from pathlib import Path\nPath('internal/plain-brief.md').write_text('changed')\n")
    proc = run_runner(root, '--command', sys.executable, str(command))
    assert proc.returncode == 2
    result = json.loads(proc.stdout)
    assert result['status'] == 'BLOCKED_CHANGED_INPUT'
    assert result['publication_qa_started'] is True
