"""F4/F5 regressions: synthetic transport only, never live campaigns or providers."""
import copy
import hashlib
import io
import json
import socket
import sys
import urllib.request
from pathlib import Path

import pytest

import complete_campaign
import export_deliverables as exporter
import plain_brief_rewrite as plain
import receipt_integrity as ri
import report_purpose
from reader_test_support import editor_pass, offline_plain_transport, refresh_fixture_acceptance_bindings
from test_two_outputs import _export_v2, REVIEW_REL


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError('live network forbidden in publication-gate tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    monkeypatch.setattr(socket, 'create_connection', deny)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(urllib.request, 'urlopen', deny)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', deny)
    monkeypatch.setenv('HOME', str(tmp_path / 'isolated-home'))
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-publication-test-only')
    monkeypatch.setenv('PLAIN_WORDS_PATH', '/dev/null')
    monkeypatch.setenv('PYTHONDONTWRITEBYTECODE', '1')


def put(root, rel, data):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return path


def read(root, rel='manifest.json'):
    return json.loads((root / rel).read_text())


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def generated_campaign(tmp_path, monkeypatch):
    """Generate real modern records through the shared client, with fake HTTP only."""
    root, export = _export_v2(tmp_path, include_preseed=False)
    refresh_fixture_acceptance_bindings(root)
    body = '# Memory units\n\n' + '\n\n'.join(
        heading + '\nA gigabyte is a decimal unit. A gibibyte uses powers of two.'
        for heading in plain.SECTIONS)
    with offline_plain_transport(plain, responder=lambda role, *_: body if role == 'writer' else json.dumps(editor_pass())):
        record = plain.campaign_brief(root)
    assert record['status'] == 'PASS', record
    assert record['provenance']['attempts']
    assert all((root / rel).is_file() for rel in record['provenance']['raw_response_paths'])
    table = {c['generation_id']: {'model': c['served_model'], 'tokens_prompt': 100000}
             for c in record['provenance']['calls']}
    seen = []

    def provider(request, timeout):
        assert request.full_url.startswith(ri.GENERATION_URL)
        assert request.get_header('Authorization') == 'Bearer synthetic-publication-test-only'
        gid = request.full_url.split('id=', 1)[1]
        seen.append(gid)
        return io.BytesIO(json.dumps({'data': table[gid]}).encode())

    monkeypatch.setattr(urllib.request, 'urlopen', provider)
    return root, export, record, table, seen


def run_export(monkeypatch, root, export, *, dry_run=False, overwrite=False):
    argv = ['export_deliverables.py', '--campaign-root', str(root), '--export-root', str(export)]
    if dry_run:
        argv.append('--dry-run')
    if overwrite:
        argv.append('--overwrite-approved')
    monkeypatch.setattr(sys, 'argv', argv)
    return exporter.main()


@pytest.mark.parametrize('dry_run', [True, False])
def test_missing_provider_calls_blocks_delivery_before_any_copy(tmp_path, monkeypatch, capsys, dry_run):
    root, export = _export_v2(tmp_path, include_preseed=False)
    record = read(root, REVIEW_REL)
    record.pop('provenance', None)
    record.pop('artifact_writers', None)
    put(root, REVIEW_REL, record)
    plain.reset_editor_note_decisions(root, brief_sha256=record['brief_sha256'])
    before = snapshot(root), snapshot(export)
    assert run_export(monkeypatch, root, export, dry_run=dry_run) == 3
    output = capsys.readouterr().out
    assert 'receipt_unverified' in output or 'served' in output or 'provider' in output
    assert (snapshot(root), snapshot(export)) == before


@pytest.mark.parametrize('dry_run', [True, False])
def test_fresh_provider_lookup_not_writable_saved_pass_controls_delivery(generated_campaign, monkeypatch, capsys, dry_run):
    root, export, record, _, seen = generated_campaign
    record['provider_verification'] = {'verdict': 'PASS', 'calls': record['provenance']['calls']}
    record['provenance']['verification'] = {'verdict': 'PASS'}
    put(root, REVIEW_REL, record)
    plain.reset_editor_note_decisions(root, brief_sha256=record['brief_sha256'])
    before = snapshot(root), snapshot(export)
    def unavailable(*args, **kwargs):
        seen.append('unavailable')
        raise RuntimeError('synthetic provider unavailable')
    monkeypatch.setattr(urllib.request, 'urlopen', unavailable)
    monkeypatch.setattr(ri.time, 'sleep', lambda _: None)
    assert run_export(monkeypatch, root, export, dry_run=dry_run) == 3
    output = capsys.readouterr().out
    assert 'HOLD' in output and 'receipt_unverified' in output
    assert seen
    assert (snapshot(root), snapshot(export)) == before


@pytest.mark.parametrize('dry_run', [True, False])
def test_healthy_generated_provenance_is_rechecked_before_delivery(generated_campaign, monkeypatch, capsys, dry_run):
    root, export, record, _, seen = generated_campaign
    assert run_export(monkeypatch, root, export, dry_run=dry_run) == 0, capsys.readouterr().out
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] is True
    assert seen == [c['generation_id'] for c in record['provenance']['calls']]
    if dry_run:
        assert not export.exists()
    else:
        for item in result['exports']:
            assert Path(item['export_path']).read_bytes() == (root / item['staged_path']).read_bytes()
            assert item['sha256'] == item['readback_sha256']
        assert read(root)['deliverable_exports']['status'] == 'copied_to_export'


def test_custom_review_path_propagates_to_all_export_gates(generated_campaign, monkeypatch, capsys):
    root, export, _, _, seen = generated_campaign
    manifest = read(root)
    manifest['plain_brief']['review'] = 'qa/custom-brief-review.json'
    (root / REVIEW_REL).rename(root / manifest['plain_brief']['review'])
    put(root, 'manifest.json', manifest)
    refresh_fixture_acceptance_bindings(root)
    plain.reset_editor_note_decisions(root, brief_sha256=read(root, manifest['plain_brief']['review'])['brief_sha256'],
                                     review_rel=manifest['plain_brief']['review'])
    decisions = read(root, 'qa/editor-note-decisions.json')
    assert decisions['retained_findings'] == []
    assert decisions['decisions'] == []
    decisions['status'] = 'reviewed'
    put(root, 'qa/editor-note-decisions.json', decisions)
    assert run_export(monkeypatch, root, export, dry_run=True) == 0, capsys.readouterr().out
    assert seen


def test_hold_cannot_archive_or_replace_an_existing_note(generated_campaign, monkeypatch, capsys):
    root, export, record, _, _ = generated_campaign
    dest = Path(read(root)['deliverable_exports']['plain_brief_intended_export_path'])
    dest.parent.mkdir(parents=True)
    dest.write_text('Prior approved note stays untouched.')
    record.pop('provenance')
    record.pop('artifact_writers')
    put(root, REVIEW_REL, record)
    before = snapshot(root), snapshot(export)
    assert run_export(monkeypatch, root, export, overwrite=True) == 3
    assert (snapshot(root), snapshot(export)) == before


def optional_campaign(root, *, purpose=None):
    manifest = {'target': 'Synthetic optional campaign', 'generated_at': '2000-01-01',
                'plain_brief': {'required': False, 'not_required_reason': 'Historical optional output'}}
    if purpose is not None:
        manifest['report_purpose'] = purpose
    put(root, 'manifest.json', manifest)
    pack = put(root, 'internal/full-research-pack.md', '# Reference\n\n## Sources\nEvidence.')
    import research_qa_repair as qa
    put(root, 'qa/evidence-privacy-review.json', {'status': 'PASS',
        'full_pack_sha256': hashlib.sha256(pack.read_bytes()).hexdigest(),
        'semantic_manifest': qa.semantic_manifest_snapshot(manifest)})
    return manifest


def admit_synthetic_manifest(monkeypatch, root, manifest):
    """Test-local byte-frozen inventory, never a live historical read or write."""
    entry = {'root': str(root), 'manifest_sha256': hashlib.sha256((root / 'manifest.json').read_bytes()).hexdigest(),
             'semantic_sha256': report_purpose.semantic_manifest_sha256(manifest),
             'identity': {'target': manifest['target'], 'generated_at': manifest['generated_at']}}
    monkeypatch.setattr(report_purpose, 'LEGACY_CAMPAIGNS', [entry])


@pytest.mark.parametrize('purpose', [None, 'factual', 'decision_assessment'])
def test_unknown_optional_campaign_cannot_take_legacy_export_shortcut(tmp_path, purpose):
    manifest = optional_campaign(tmp_path, purpose=purpose)
    with pytest.raises(ValueError, match='purpose|legacy|brief'):
        exporter.validate_plain_brief(tmp_path, manifest)


@pytest.mark.parametrize('purpose', [None, 'factual'])
def test_completion_receipts_do_not_accept_unknown_optional_legacy(tmp_path, purpose):
    manifest = optional_campaign(tmp_path, purpose=purpose)
    result = complete_campaign.check_receipts(tmp_path, manifest, online=False)
    assert result['verdict'] == 'HOLD', result


@pytest.mark.parametrize('declared', [True, False])
def test_frozen_optional_legacy_inventory_still_admitted(tmp_path, monkeypatch, declared):
    manifest = optional_campaign(tmp_path)
    if not declared:
        manifest.pop('plain_brief')
        put(tmp_path, 'manifest.json', manifest)
        refresh_fixture_acceptance_bindings(tmp_path)
    admit_synthetic_manifest(monkeypatch, tmp_path, manifest)
    exporter.validate_plain_brief(tmp_path, manifest)
    result = complete_campaign.check_receipts(tmp_path, manifest, online=False)
    assert result['verdict'] == 'PASS', result


def corrected_record(root):
    put(root, 'brief.md', 'A qualification remains.\n')
    put(root, 'draft.md', 'A qualification remains.\n')
    put(root, 'pack.md', 'Sources and context.\n')
    return {'status': 'PASS', 'brief_path': 'brief.md', 'full_pack_path': 'pack.md',
            'brief_sha256': hashlib.sha256((root / 'brief.md').read_bytes()).hexdigest(),
            'writer_model': 'anthropic/claude-opus-5.5', 'editor_model': 'openai/gpt-6.1-sol-pro',
            'writer_skipped': 'corrected_draft', 'corrected_draft_path': 'draft.md',
            'provenance': {'calls': [{'role': 'editor', 'generation_id': 'gen-SYNTHETIC-legacy01'}]}}


@pytest.mark.parametrize('purpose', [None, 'factual', 'decision_assessment'])
def test_corrected_draft_legacy_identity_exemption_is_not_available_to_new_campaigns(tmp_path, purpose):
    optional_campaign(tmp_path, purpose=purpose)
    result = ri.verify_provenance(corrected_record(tmp_path), tmp_path,
        fetch=lambda _: {'model': 'openai/gpt-6.1-sol-pro', 'tokens_prompt': 1000})
    assert result['verdict'] != 'PASS', result


def test_corrected_draft_legacy_exemption_only_for_byte_frozen_inventory(tmp_path, monkeypatch):
    manifest = optional_campaign(tmp_path)
    admit_synthetic_manifest(monkeypatch, tmp_path, manifest)
    record = corrected_record(tmp_path)
    result = ri.verify_provenance(record, tmp_path,
        fetch=lambda _: {'model': 'openai/gpt-6.1-sol-pro', 'tokens_prompt': 1000})
    assert result['verdict'] == 'PASS', result
    manifest['target'] += ' altered'
    put(tmp_path, 'manifest.json', manifest)
    assert ri.verify_provenance(record, tmp_path,
        fetch=lambda _: {'model': 'openai/gpt-6.1-sol-pro', 'tokens_prompt': 1000})['verdict'] != 'PASS'


def test_classification_reviewer_generation_in_attempts_is_provider_checked(generated_campaign):
    """Classification may share the comments editor, never a contributing writer."""
    root, _, record, table, _ = generated_campaign
    from research_settings import load_settings
    def transport(request, timeout):
        return io.BytesIO(json.dumps({'id': 'gen-SYNTHETIC-classifier01',
            'model': json.loads(request.data)['model'], 'usage': {'cost': 0},
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{"classification":"optional_style"}'}}]}).encode())
    with offline_plain_transport(plain, transport), plain.run_context(
            load_settings(environ={}), root / 'qa/classification-provider', writers=record['artifact_writers']) as run:
        answer = plain.call(record['editor_model'], 'Classify retained notes without rewriting.',
                            'Synthetic evidence and readers.', role='reviewer')
    assert answer.acceptance_eligible is True
    record = copy.deepcopy(record)
    record['provenance']['attempts'].extend(run.calls)
    record['provenance']['calls'].extend(copy.deepcopy(run.calls))
    record['provenance']['raw_response_paths'].extend(str(Path(p).relative_to(root)) for p in run.raw_files)
    classifier = run.calls[-1]
    assert classifier['served_model'] == record['editor_model']
    table[classifier['generation_id']] = {'model': classifier['served_model'], 'tokens_prompt': 5000}
    seen = []
    def fetch(gid):
        seen.append(gid)
        return table.get(gid)
    result = ri.verify_provenance(record, root, fetch=fetch)
    assert result['verdict'] == 'PASS', result
    assert seen == [c['generation_id'] for c in record['provenance']['calls']]
    assert classifier['generation_id'] in seen
    table[classifier['generation_id']]['model'] = 'mistralai/mistral-large'
    result = ri.verify_provenance(record, root, fetch=fetch)
    assert result['verdict'] == 'FAIL'
    assert any(classifier['generation_id'] in reason and 'mistral' in reason for reason in result['reasons'])


@pytest.mark.parametrize('change', ['unverified', 'uncatalogued', 'writer_developer', 'missing_generation'])
def test_classification_reviewer_must_have_known_independent_served_identity(generated_campaign, change):
    root, _, record, table, _ = generated_campaign
    record = copy.deepcopy(record)
    judgement: dict = dict(record['provenance']['attempts'][-1], role='reviewer', generation_id='gen-SYNTHETIC-judgement01')
    if change == 'unverified':
        judgement['identity_verified'] = False
    elif change == 'uncatalogued':
        judgement['served_model'] = 'nobody/not-a-catalogued-or-served-model'
    elif change == 'writer_developer':
        judgement['served_model'] = record['writer_model']
    else:
        judgement['generation_id'] = None
    record['provenance']['attempts'].append(copy.deepcopy(judgement))
    if judgement['generation_id']:
        record['provenance']['calls'].append(judgement)
        table[judgement['generation_id']] = {'model': judgement['served_model'], 'tokens_prompt': 5000}
    assert ri.verify_provenance(record, root, fetch=table.get)['verdict'] != 'PASS'


@pytest.mark.parametrize('unknown', ['unverified_author', 'uncatalogued_author', 'same_developer'])
def test_modern_receipt_rejects_unknown_or_nonindependent_author_identity(generated_campaign, unknown):
    root, _, record, table, _ = generated_campaign
    record = copy.deepcopy(record)
    author = record['artifact_writers'][0]
    if unknown == 'unverified_author':
        author['identity_verified'] = False
    elif unknown == 'uncatalogued_author':
        author['served_model'] = 'nobody/not-a-catalogued-or-served-model'
        record['writer_model'] = author['served_model']
    else:
        author['served_model'] = record['editor_model']
        record['writer_model'] = author['served_model']
    for rows in (record['provenance']['calls'], record['provenance']['attempts']):
        rows[0] = copy.deepcopy(author)
    table[author['generation_id']]['model'] = author['served_model']
    result = ri.verify_provenance(record, root, fetch=table.get)
    assert result['verdict'] != 'PASS', result
