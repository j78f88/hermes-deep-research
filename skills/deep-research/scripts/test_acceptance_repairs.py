"""Acceptance bypass reproductions; no paid calls or original campaign edits."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest
import research_qa_repair as qa


def put(root, rel, value):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value)
    return path


def seal_test_legacy_pack_acceptance(root):
    """Add current-pack bindings to an existing synthetic legacy QA receipt."""
    manifest = json.loads((root / 'manifest.json').read_text())
    path = root / 'qa/evidence-privacy-review.json'
    receipt = json.loads(path.read_text())
    receipt.update(full_pack_sha256=sha(root / 'internal/full-research-pack.md'),
                   semantic_manifest=qa.semantic_manifest_snapshot(manifest))
    put(root, 'qa/evidence-privacy-review.json', receipt)


def seal_test_acceptance(root):
    """Bind the existing synthetic suite fixture, without inventing review findings."""
    review = json.loads((root / 'qa/plain-brief-review.json').read_text())
    manifest = json.loads((root / 'manifest.json').read_text())
    if 'full_pack_path' not in review:
        review['full_pack_path'] = ('internal/reference-pack.md'
            if manifest.get('output_layout') == 'brief-and-reference-v2' else 'internal/full-research-pack.md')
        put(root, 'qa/plain-brief-review.json', review)
    pending = qa.build_pending_editor_decisions(root, review=review, previous_decisions={})
    pending['status'] = 'reviewed' if not pending['retained_findings'] else 'pending'
    put(root, 'qa/editor-note-decisions.json', pending)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_for(root, notes=None):
    put(root, 'internal/plain-brief.md', 'The result is qualified.\n')
    put(root, 'internal/reference-pack.md', '# Reference\n\n## Sources\nEvidence.\n')
    return {'status': 'PASS', 'brief_path': 'internal/plain-brief.md',
            'full_pack_path': 'internal/reference-pack.md',
            'brief_sha256': sha(root / 'internal/plain-brief.md'),
            'full_pack_sha256': sha(root / 'internal/reference-pack.md'),
            'writer_model': 'anthropic/claude-opus-5.5', 'editor_model': 'openai/gpt-6.1-sol-pro',
            'editor_minor': notes or [], 'editor_blocking': []}


@pytest.mark.parametrize('classification', sorted(qa.DECISION_CLASSES))
@pytest.mark.parametrize('verdict', [None, '', 'partial', 'unsupported', 'unknown', True])
def test_every_disposition_requires_explicit_supported_verdict(classification, verdict):
    decision = {'note': 'A qualification is missing.', 'classification': classification,
                'evidence_refs': ['source.md']}
    if verdict is not None:
        decision['evidence_verdict'] = verdict
    result = qa.evaluate_editor_review({'status': 'PASS', 'editor_minor': [decision['note']]},
                                      evidence_decisions=[decision])
    assert not result['publication_qa_allowed']


def test_approved_unapplied_correction_is_open():
    result = qa.evaluate_editor_review({'status': 'PASS', 'editor_minor': ['Keep the period.']},
        evidence_decisions=[{'note': 'Keep the period.', 'classification': 'apply_correction',
                             'evidence_refs': ['source.md'], 'evidence_verdict': 'supported'}])
    assert not result['publication_qa_allowed']
    assert result['requires_rewrite']


def test_style_relabelling_needs_independent_classification():
    result = qa.evaluate_editor_review({'status': 'PASS', 'editor_minor': ['Keep the planned qualifier.']},
        evidence_decisions=[{'note': 'Keep the planned qualifier.', 'classification': 'optional_style',
                             'evidence_refs': ['source.md'], 'evidence_verdict': 'supported'}])
    assert not result['publication_qa_allowed']


def accepted(root, note=None, classification='optional_style'):
    manifest = {'output_layout': 'brief-and-reference-v2', 'report_purpose': 'factual',
                'plain_brief': {'required': True}, 'deliverable_exports': {'enabled': True}}
    put(root, 'manifest.json', manifest)
    review = review_for(root, [note] if note else [])
    put(root, 'qa/plain-brief-review.json', review)
    decisions = {'brief_sha256': review['brief_sha256'], 'full_pack_sha256': review['full_pack_sha256'],
                 'semantic_manifest': qa.semantic_manifest_snapshot(manifest), 'decisions': []}
    if note:
        put(root, 'source.md', 'This is supporting source evidence.')
        disposition = {'note': note, 'classification': classification, 'evidence_verdict': 'supported',
                       'classification_verdict': 'supported',
                       'correction_applied': classification == 'apply_correction',
                       'factual_impact': {'optional_style': 'none', 'mistaken_reviewer': 'reviewer_error'}.get(classification, 'resolved'),
                       'reviewer': review['editor_model'],
                       'producers': [review['writer_model'], review['editor_model']],
                       'brief_sha256': review['brief_sha256'], 'full_pack_sha256': review['full_pack_sha256'],
                       'evidence_bindings': [{'path': 'source.md', 'sha256': sha(root / 'source.md')}],
                       'rationale': 'The source and both readers preserve the same qualification.'}
        put(root, 'qa/disposition.json', disposition)
        decisions['decisions'] = [{'note': note, 'classification': classification,
                                  'evidence_refs': ['source.md', 'qa/disposition.json'],
                                  'evidence_verdict': 'supported', 'classification_review': 'qa/disposition.json',
                                  'evidence_bindings': [{'path': p, 'sha256': sha(root / p)}
                                                        for p in ['source.md', 'qa/disposition.json']]}]
    from publication_test_support import add_generated_provenance
    from reader_test_support import editor_pass
    import plain_brief_rewrite as plain
    if note:
        binding = {'reader_body_sha256': hashlib.sha256(plain.brief_body(
                       (root / review['brief_path']).read_text()).encode()).hexdigest(),
                   'full_pack_sha256': review['full_pack_sha256'],
                   'evidence_bindings': disposition['evidence_bindings']}
        row = {key: disposition[key] for key in qa.JUDGEMENT_FIELDS}
        response = dict(editor_pass(), minor=[note], note_dispositions=[row], review_binding=binding)
        add_generated_provenance(root, review, editor_response=response, review_binding=binding)
        disposition.update(generation_id=review['provenance']['final_editor_generation_id'],
            raw_response_path=review['provenance']['raw_response_paths'][-1], review_binding=binding)
        put(root, 'qa/disposition.json', disposition)
        decisions['decisions'][0]['evidence_bindings'][1]['sha256'] = sha(root / 'qa/disposition.json')
    else:
        add_generated_provenance(root, review)
    put(root, 'qa/plain-brief-review.json', review)
    pending = qa.build_pending_editor_decisions(root, review=review, previous_decisions=decisions)
    pending.update(status='reviewed', decisions=decisions['decisions'])
    put(root, 'qa/editor-note-decisions.json', pending)
    return manifest, review, pending


def test_healthy_campaign_and_independent_style_control(tmp_path):
    accepted(tmp_path, 'Use a shorter heading.')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert qa.campaign_readiness(tmp_path)['status'] == 'PASS'
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('field', ['report_purpose', 'unknown', 'deliverable_exports.destination'])
def test_semantic_manifest_changes_invalidate_acceptance(tmp_path, field):
    manifest, _, _ = accepted(tmp_path)
    if '.' in field:
        manifest['deliverable_exports']['destination'] = '/a/different/export'
    else:
        manifest[field] = 'changed'
    put(tmp_path, 'manifest.json', manifest)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


@pytest.mark.parametrize('field', ['status', 'exported_at', 'exports', 'prior_exports', 'overwrite_approved'])
def test_only_five_exporter_bookkeeping_fields_are_mutable(tmp_path, field):
    manifest, _, _ = accepted(tmp_path)
    manifest['deliverable_exports'][field] = ['bookkeeping']
    put(tmp_path, 'manifest.json', manifest)
    assert qa.campaign_readiness(tmp_path)['status'] == 'PASS'


def test_fresh_clean_review_cannot_erase_old_factual_finding(tmp_path):
    _, review, _ = accepted(tmp_path)
    historical = dict(review, editor_minor=['Keep the planned qualifier.'])
    put(tmp_path, 'receipts/qa-history/generation1/plain-brief-review.json', historical)
    result = qa.campaign_readiness(tmp_path)
    assert not result['publication_qa_allowed']
    assert 'incomplete' in json.dumps(result)
    pending = qa.build_pending_editor_decisions(tmp_path, review=review)
    assert 'Keep the planned qualifier.' in pending['retained_findings']


@pytest.mark.parametrize('path', ['internal/plain-brief.md', 'internal/reference-pack.md', 'source.md'])
def test_reader_and_evidence_drift_blocks(tmp_path, path):
    accepted(tmp_path, 'Use a shorter heading.')
    (tmp_path / path).write_text('changed')
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


def test_evidence_binding_cannot_be_optional(tmp_path):
    _, review, decisions = accepted(tmp_path, 'Use a shorter heading.')
    decisions['decisions'][0].pop('evidence_bindings')
    assert qa.validate_saved_review_bindings(tmp_path, review, decisions)['status'] != 'PASS'


@pytest.mark.parametrize('path', ['../outside.md', '/etc/passwd', 'internal/../source.md'])
def test_unsafe_admitted_paths_rejected(tmp_path, path):
    with pytest.raises(ValueError):
        qa.build_review_packet([path], root=tmp_path)


def test_parent_directory_symlink_not_admitted(tmp_path):
    put(tmp_path, 'real/source.md', 'source')
    (tmp_path / 'alias').symlink_to(tmp_path / 'real', target_is_directory=True)
    with pytest.raises(ValueError):
        qa.build_review_packet(['alias/source.md'], root=tmp_path)


def test_empty_packet_is_not_acceptance(tmp_path):
    assert qa.verify_review_packet({'files': []}, root=tmp_path)['status'] != 'PASS'


def test_pending_generation_binds_pair_evidence_and_retains_findings(tmp_path):
    _, review, decisions = accepted(tmp_path, 'Use a shorter heading.')
    pending = qa.build_pending_editor_decisions(tmp_path, review=review, previous_decisions=decisions)
    assert pending['brief_sha256'] == review['brief_sha256']
    assert pending['full_pack_sha256'] == review['full_pack_sha256']
    assert pending['retained_findings'] == ['Use a shorter heading.']
    assert pending['review_inputs']['files']
    assert qa.verify_review_packet(pending['review_inputs'], root=tmp_path)['status'] == 'PASS'


def test_pending_generation_cannot_silently_drop_existing_decisions(tmp_path):
    _, review, _ = accepted(tmp_path, 'Keep the planned qualifier.')
    review['editor_minor'] = []
    put(tmp_path, 'qa/plain-brief-review.json', review)
    pending = qa.build_pending_editor_decisions(tmp_path, review=review)
    assert pending['retained_findings'] == ['Keep the planned qualifier.']


def test_pending_generation_binds_the_actual_editor_review(tmp_path):
    _, review, _ = accepted(tmp_path)
    pending = qa.build_pending_editor_decisions(tmp_path, review=review)
    put(tmp_path, 'qa/editor-note-decisions.json', pending)
    review['editor_minor'] = ['A new factual finding.']
    put(tmp_path, 'qa/plain-brief-review.json', review)
    assert qa.verify_review_packet(pending['review_inputs'], root=tmp_path)['status'] != 'PASS'


def test_optional_legacy_brief_does_not_skip_pack_gate(tmp_path, monkeypatch):
    put(tmp_path, 'manifest.json', {'plain_brief': {'required': False}})
    from publication_test_support import admit_frozen_legacy
    admit_frozen_legacy(monkeypatch, tmp_path)
    result = qa.campaign_readiness(tmp_path)
    assert not result['publication_qa_allowed']
    import export_deliverables
    with pytest.raises(ValueError, match='readiness'):
        export_deliverables.validate_plain_brief(tmp_path, {'plain_brief': {'required': False}})


def test_v2_brief_cannot_be_made_optional(tmp_path):
    manifest, _, _ = accepted(tmp_path)
    manifest['plain_brief']['required'] = False
    put(tmp_path, 'manifest.json', manifest)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


def test_publication_runner_checks_semantic_manifest(tmp_path):
    manifest, _, _ = accepted(tmp_path)
    manifest['report_purpose'] = 'decision_assessment'
    put(tmp_path, 'manifest.json', manifest)
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('run_publication_reviews.py')),
                           '--campaign-root', str(tmp_path), '--preflight-only'], capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr


@pytest.mark.parametrize('classification', ['already_resolved', 'mistaken_reviewer', 'apply_correction'])
def test_checked_current_dispositions_are_healthy_controls(tmp_path, classification):
    accepted(tmp_path, 'Keep the qualification.', classification=classification)
    disposition = json.loads((tmp_path / 'qa/disposition.json').read_text())
    if classification == 'apply_correction':
        disposition['correction_applied'] = True
    if classification == 'mistaken_reviewer':
        disposition['factual_impact'] = 'reviewer_error'
    put(tmp_path, 'qa/disposition.json', disposition)
    decisions = json.loads((tmp_path / 'qa/editor-note-decisions.json').read_text())
    decisions['decisions'][0]['evidence_bindings'][1]['sha256'] = sha(tmp_path / 'qa/disposition.json')
    put(tmp_path, 'qa/editor-note-decisions.json', decisions)
    assert qa.campaign_readiness(tmp_path)['status'] == 'PASS'


def test_symlinked_history_cannot_hide_prior_findings(tmp_path):
    accepted(tmp_path)
    put(tmp_path, 'earlier/plain-brief-review.json', {'editor_minor': ['Keep the planned qualifier.']})
    (tmp_path / 'receipts/qa-history').mkdir(parents=True)
    (tmp_path / 'receipts/qa-history/generation1').symlink_to(tmp_path / 'earlier', target_is_directory=True)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


@pytest.mark.parametrize('path', ['../outside.json', '/outside.json', 'qa/../outside.json'])
def test_assurance_stub_cannot_write_unsafe_paths(tmp_path, path, monkeypatch):
    def deny_write(*args, **kwargs):
        raise AssertionError('unsafe write attempted')
    monkeypatch.setattr(Path, 'write_text', deny_write)
    with pytest.raises(ValueError):
        qa.prepare_preexport_assurance_stub(tmp_path, brief_sha256='a'*64,
                                            pack_sha256='b'*64, assurance_rel=path)


def test_publication_packet_cannot_overwrite_reader(tmp_path):
    accepted(tmp_path)
    original = (tmp_path / 'internal/plain-brief.md').read_bytes()
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('run_publication_reviews.py')),
        '--campaign-root', str(tmp_path), '--packet-out', 'internal/plain-brief.md', '--preflight-only'],
        capture_output=True, text=True)
    assert proc.returncode == 2
    assert (tmp_path / 'internal/plain-brief.md').read_bytes() == original


def test_new_unresolved_history_during_publication_cannot_pass(tmp_path):
    accepted(tmp_path)
    script = "import json; from pathlib import Path; p=Path('receipts/qa-history/new/plain-brief-review.json'); p.parent.mkdir(parents=True); p.write_text(json.dumps({'editor_minor':['Keep the planned qualifier.']}))"
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('run_publication_reviews.py')),
        '--campaign-root', str(tmp_path), '--command', sys.executable, '-c', script], capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_export_override_cannot_redirect_frozen_destination(tmp_path):
    from test_two_outputs import _export_v2
    from test_deep_research_campaign_tools import prepare_test_export
    root, export = _export_v2(tmp_path)
    manifest = json.loads((root / 'manifest.json').read_text())
    for key in ['reference_pack_intended_export_path', 'plain_brief_intended_export_path']:
        manifest['deliverable_exports'].pop(key, None)
    put(root, 'manifest.json', manifest)
    seal_test_acceptance(root)
    other_export = tmp_path / 'other-export'
    prepare_test_export(other_export)
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('export_deliverables.py')),
        '--campaign-root', str(root), '--export-root', str(other_export)], capture_output=True, text=True)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert not (other_export).exists()


def test_legacy_optional_pack_snapshot_preserves_json_types(tmp_path):
    manifest = {'deliverable_exports': {'enabled': True}}
    put(tmp_path, 'manifest.json', manifest)
    put(tmp_path, 'internal/full-research-pack.md', '# Pack')
    put(tmp_path, 'qa/evidence-privacy-review.json', {'status': 'PASS',
        'full_pack_sha256': sha(tmp_path / 'internal/full-research-pack.md'),
        'semantic_manifest': qa.semantic_manifest_snapshot(manifest)})
    manifest['deliverable_exports']['enabled'] = 1
    put(tmp_path, 'manifest.json', manifest)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


def test_classification_evidence_must_name_real_support(tmp_path):
    accepted(tmp_path, 'Use a shorter heading.')
    disposition_path = tmp_path / 'qa/disposition.json'
    disposition = json.loads(disposition_path.read_text())
    disposition['evidence_bindings'] = [{}]
    put(tmp_path, 'qa/disposition.json', disposition)
    decisions = json.loads((tmp_path / 'qa/editor-note-decisions.json').read_text())
    decisions['decisions'][0]['evidence_bindings'][1]['sha256'] = sha(disposition_path)
    put(tmp_path, 'qa/editor-note-decisions.json', decisions)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


def test_manifest_snapshot_preserves_json_types(tmp_path):
    manifest, _, _ = accepted(tmp_path)
    manifest['deliverable_exports']['enabled'] = 1
    put(tmp_path, 'manifest.json', manifest)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


def test_publication_command_cannot_change_manifest_semantics(tmp_path):
    accepted(tmp_path)
    script = "import json; from pathlib import Path; p=Path('manifest.json'); m=json.loads(p.read_text()); m['unknown']='new'; p.write_text(json.dumps(m))"
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('run_publication_reviews.py')),
                           '--campaign-root', str(tmp_path), '--command', sys.executable, '-c', script],
                          capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_review_packet_freezes_semantics_not_export_bookkeeping(tmp_path):
    manifest, _, _ = accepted(tmp_path)
    packet = qa.build_review_packet(['manifest.json'], root=tmp_path)
    manifest['deliverable_exports']['status'] = 'copied_to_export'
    put(tmp_path, 'manifest.json', manifest)
    assert qa.verify_review_packet(packet, root=tmp_path)['status'] == 'PASS'
    manifest['deliverable_exports']['unknown'] = 'changed'
    put(tmp_path, 'manifest.json', manifest)
    assert qa.verify_review_packet(packet, root=tmp_path)['status'] != 'PASS'


def test_healthy_optional_legacy_pack_control(tmp_path, monkeypatch):
    manifest = {'plain_brief': {'required': False}}
    put(tmp_path, 'manifest.json', manifest)
    from publication_test_support import admit_frozen_legacy
    admit_frozen_legacy(monkeypatch, tmp_path)
    put(tmp_path, 'internal/full-research-pack.md', '# Pack\n\n## Sources\nEvidence.')
    put(tmp_path, 'qa/evidence-privacy-review.json', {'status': 'PASS',
        'full_pack_sha256': sha(tmp_path / 'internal/full-research-pack.md'),
        'semantic_manifest': qa.semantic_manifest_snapshot(manifest)})
    assert qa.campaign_readiness(tmp_path)['status'] == 'PASS'
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name('run_publication_reviews.py')),
                          '--campaign-root', str(tmp_path), '--preflight-only'],
                          capture_output=True, text=True)
    assert proc.returncode == 2  # the child has no test-local frozen inventory admission
    assert json.loads(proc.stdout)['publication_qa_allowed'] is False


def test_self_classification_not_independent(tmp_path):
    accepted(tmp_path, 'Use a shorter heading.')
    path = tmp_path / 'qa/disposition.json'
    disposition = json.loads(path.read_text())
    disposition['reviewer'] = 'anthropic/claude-opus-4'
    put(tmp_path, 'qa/disposition.json', disposition)
    decisions = json.loads((tmp_path / 'qa/editor-note-decisions.json').read_text())
    decisions['decisions'][0]['evidence_bindings'][1]['sha256'] = sha(path)
    put(tmp_path, 'qa/editor-note-decisions.json', decisions)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']
