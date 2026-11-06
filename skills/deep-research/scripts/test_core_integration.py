"""Offline wiring regressions; synthetic transports are not live provider receipts."""
import io
import json
import hashlib
import urllib.error
import subprocess
import sys
from email.message import Message
from pathlib import Path
from unittest.mock import patch

import pytest
import plain_brief_rewrite as plain
import model_client
from research_settings import load_settings
from research_qa_repair import campaign_readiness, semantic_manifest_snapshot
from reader_test_support import editor_pass, offline_plain_transport
import receipt_integrity
import itertools

SYNTHETIC_CALL_IDS = itertools.count(1)


@pytest.fixture(autouse=True)
def deny_unmocked_legacy_network(monkeypatch, tmp_path):
    # Red tests must be safe even while the old production bypass still exists.
    export = tmp_path / 'offline-export'
    auto = export
    auto.mkdir(parents=True)
    monkeypatch.setattr(plain, 'EXPORT_ROOT', export)
    monkeypatch.setattr(plain, 'AUTO', auto)
    def deny(*args, **kwargs):
        raise AssertionError('unmocked legacy network forbidden in offline tests')
    monkeypatch.setattr(plain.urllib.request, 'urlopen', deny)
    monkeypatch.setattr(plain.urllib.request.OpenerDirector, 'open', deny)
    monkeypatch.setattr(model_client, '_api_key', lambda: 'synthetic-only')
    monkeypatch.setattr(plain.time, 'sleep', lambda _: None)


def test_offline_harness_confines_export_writes(tmp_path):
    assert plain.EXPORT_ROOT == tmp_path / 'offline-export'
    assert plain.AUTO == plain.EXPORT_ROOT


def body():
    return '# Memory units\n\n' + '\n\n'.join(h + '\nA gigabyte is a decimal unit. A gibibyte uses powers of two.' for h in plain.SECTIONS)


def campaign(root):
    (root / 'internal').mkdir()
    (root / 'qa').mkdir()
    manifest = {'report_purpose': 'factual', 'output_layout': plain.OUTPUT_LAYOUT_V2,
                'target': 'Memory units', 'internal_outputs': [{'path': plain.REFERENCE_PACK_REL}],
                'plain_brief': {'required': True}}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    pack = root / plain.REFERENCE_PACK_REL
    pack.write_text('# Memory units\nOne gigabyte is 1,000,000,000 bytes. One gibibyte is 1,073,741,824 bytes.\n')
    (root / 'qa/evidence-privacy-review.json').write_text(json.dumps({
        'status': 'PASS', 'blocking': [], 'reference_pack_sha256': plain.sha256_file(pack), **editor_pass()}))
    return manifest


def result_for(settings, role, text, *, eligible=True):
    model = settings.resolve(role)['model']
    call = {'role': role, 'status': 'success', 'served_model': model, 'requested_model': model,
            'identity_verified': True, 'generation_id': f'gen-SYNTHETIC-{next(SYNTHETIC_CALL_IDS):04d}', 'cost_usd': None}
    return model_client.CompletionResult(text, {'synthetic': True}, [call], [{'synthetic': True, 'text': text}], eligible)


def test_call_uses_shared_client_settings_and_preserves_raw_before_parse(tmp_path, monkeypatch):
    settings = load_settings(environ={})
    seen = []
    def completion(model, system, user, **kwargs):
        seen.append((model, kwargs))
        return result_for(settings, kwargs['role'], 'not json')
    monkeypatch.setattr(model_client, 'chat_completion', completion)
    with plain.run_context(settings, tmp_path):
        assert plain.call(plain.WRITER, 's', 'u', role='writer') == 'not json'
    assert seen[0][0] is None
    assert seen[0][1]['max_tokens'] is None
    assert list(tmp_path.glob('*.raw.json'))
    assert json.loads(next(tmp_path.glob('*.raw.json')).read_text())['raw_responses']


def test_auth_failure_stops_and_preserves_attempts(tmp_path, monkeypatch):
    settings = load_settings(environ={})
    attempts = []
    def transport(req, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError(req.full_url, 401, 'Denied', {}, io.BytesIO(b'{"error":"Denied"}'))
    original = model_client.chat_completion
    def completion(*args, **kwargs):
        return original(*args, **kwargs, api_key='synthetic-secret', transport=transport, sleep=lambda _: None)
    monkeypatch.setattr(model_client, 'chat_completion', completion)
    with plain.run_context(settings, tmp_path):
        with pytest.raises(model_client.ModelCallError, match='authentication'):
            plain.call(plain.WRITER, 's', 'u', role='writer')
    assert attempts == [1]
    saved = json.loads(next(tmp_path.glob('*.raw.json')).read_text())
    assert saved['calls'][0]['status'] == 'authentication'


def test_comments_reviewer_receives_actual_all_writer_calls(tmp_path, monkeypatch):
    settings = load_settings(environ={})
    seen = []
    def completion(model, system, user, **kwargs):
        seen.append(kwargs)
        return result_for(settings, kwargs['role'], body() if kwargs['role'] == 'writer' else json.dumps(editor_pass()))
    monkeypatch.setattr(model_client, 'chat_completion', completion)
    with plain.run_context(settings, tmp_path / 'raw'):
        plain.rewrite('units', {'slug': 'units', 'title': 'Units', 'sources': []}, tmp_path)
    assert [x['role'] for x in seen] == ['writer', 'brief_reviewer']
    assert seen[-1]['writers'][0]['served_model'] == settings.resolve('writer')['model']


def test_unknown_corrected_draft_author_cannot_be_independently_accepted(tmp_path, monkeypatch):
    settings = load_settings(environ={})
    seen = []
    def completion(model, system, user, **kwargs):
        seen.append(kwargs)
        return result_for(settings, kwargs['role'], json.dumps(editor_pass()), eligible=False)
    monkeypatch.setattr(model_client, 'chat_completion', completion)
    draft = tmp_path / 'draft.md'; draft.write_text(body())
    with plain.run_context(settings, tmp_path / 'raw'):
        _, final = plain.rewrite('units', {'slug': 'units', 'title': 'Units', 'sources': []}, tmp_path, corrected_draft=draft)
    assert len(seen) == 1
    assert seen[0]['writers'] and seen[0]['writers'][0].get('status') == 'unknown_author'
    assert final['editor'] != 'PASS'


def install_completion(monkeypatch, *, notes=None):
    settings = load_settings(environ={})
    def completion(model, system, user, **kwargs):
        review = editor_pass(); review['minor'] = notes or []
        return result_for(settings, kwargs['role'], body() if kwargs['role'] == 'writer' else json.dumps(review))
    monkeypatch.setattr(model_client, 'chat_completion', completion)


def test_generation_freezes_semantics_before_pending_and_keeps_history(tmp_path, monkeypatch):
    campaign(tmp_path)
    install_completion(monkeypatch)
    first = plain.campaign_brief(tmp_path)
    assert first['status'] == 'PASS'
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is True
    decisions = json.loads((tmp_path / 'qa/editor-note-decisions.json').read_text())
    assert decisions['semantic_manifest'] == semantic_manifest_snapshot(json.loads((tmp_path / 'manifest.json').read_text()))
    old_review = (tmp_path / 'qa/plain-brief-review.json').read_bytes()
    plain.campaign_brief(tmp_path)
    histories = list((tmp_path / 'receipts/qa-history').glob('*/plain-brief-review.json'))
    assert len(histories) == 1 and histories[0].read_bytes() == old_review
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is True


def test_current_evidence_is_bound_by_generated_pending(tmp_path, monkeypatch):
    campaign(tmp_path); install_completion(monkeypatch)
    plain.campaign_brief(tmp_path)
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is True
    evidence = tmp_path / 'qa/evidence-privacy-review.json'
    evidence.write_text(evidence.read_text() + '\n')
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is False


def test_refresh_retains_old_notes_and_cannot_claim_acceptance(tmp_path, monkeypatch):
    campaign(tmp_path); install_completion(monkeypatch, notes=['Keep the unit qualification.'])
    first = plain.campaign_brief(tmp_path)
    assert first['acceptance_status'] == 'HOLD'
    install_completion(monkeypatch)
    second = plain.campaign_brief(tmp_path)
    assert second['acceptance_status'] == 'HOLD'
    pending = json.loads((tmp_path / 'qa/editor-note-decisions.json').read_text())
    assert pending['retained_findings'] == ['Keep the unit qualification.']
    history = next((tmp_path / 'receipts/qa-history').iterdir())
    assert (history / 'internal/plain-brief.md').exists()
    assert (history / 'qa/evidence-privacy-review.json').exists()


def test_plain_optional_exit_checks_shared_readiness(tmp_path, monkeypatch):
    manifest = {'report_purpose': 'factual', 'plain_brief': {'required': False, 'not_required_reason': 'legacy'}}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(plain, 'reference_pack_review_holds', lambda *a: [])
    result = plain.verify_campaign_plain_brief(tmp_path, manifest)
    assert any('campaign_readiness' in h for h in result['holds'])


def test_old_publish_refuses_unbound_pack_before_any_write(tmp_path, monkeypatch):
    out = tmp_path / 'out'; out.mkdir()
    (out / 'units.md').write_text(body())
    operations = []
    monkeypatch.setattr(plain.subprocess, 'run', lambda *a, **k: operations.append(a))
    with pytest.raises(ValueError, match='campaign_root'):
        plain.publish({'units': {'slug': 'units', 'sources': [], 'date': '2026-11-01'}}, ['units'], out)
    assert operations == []


def test_old_cli_publish_refuses_before_output_directory_creation(tmp_path):
    manifest = tmp_path / 'packs.json'
    manifest.write_text(json.dumps({'units': {'slug': 'units', 'sources': []}}))
    out = tmp_path / 'not-created'
    proc = subprocess.run([sys.executable, '-B', str(Path(plain.__file__)), '--manifest', str(manifest),
                           '--publish', '--out', str(out)], capture_output=True, text=True)
    assert proc.returncode != 0
    assert 'campaign_root' in proc.stderr
    assert not out.exists()


@pytest.mark.parametrize('writer_model,editor_model', [('anthropic/claude-opus-5.5', 'anthropic/claude-opus-5.5'), ('unknown/unknown', 'openai/gpt-6.1-sol-pro')])
def test_generated_review_gate_rechecks_actual_developers(tmp_path, monkeypatch, writer_model, editor_model):
    campaign(tmp_path); install_completion(monkeypatch)
    plain.campaign_brief(tmp_path)
    review_path = tmp_path / plain.DEFAULT_REVIEW_REL
    record = json.loads(review_path.read_text())
    record['artifact_writers'] = [{**record['artifact_writers'][0], 'served_model': writer_model}]
    record['editor_model'] = editor_model
    latest = [call for call in record['provenance']['attempts']
              if call['role'] == 'brief_reviewer' and call['status'] == 'success'][-1]
    latest['served_model'] = editor_model
    # No incidental latest-attempt identity mismatch may explain this rejection.
    assert latest['served_model'] == record['editor_model']
    review_path.write_text(json.dumps(record))
    # Rebuild the input packet honestly; independence is a separate, non-disableable gate.
    plain.reset_editor_note_decisions(tmp_path, brief_sha256=record['brief_sha256'], apply_served_judgements=True)
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    result = plain.verify_campaign_plain_brief(tmp_path, manifest)
    assert 'plain_brief_review_not_independent' in result['holds']


@pytest.mark.parametrize('role', ['writer', 'rewriting_editor'])
@pytest.mark.parametrize('remove_all', [False, True])
def test_saved_successful_author_omission_cannot_hide_reviewer_developer(tmp_path, role, remove_all):
    campaign(tmp_path)
    reviews = iter([dict(editor_pass(), verdict='FAIL'), editor_pass()])
    def respond(role, system, user):
        return body() if role == 'writer' else json.dumps(next(reviews))
    with offline_plain_transport(plain, responder=respond):
        record = plain.campaign_brief(tmp_path)
    assert record['acceptance_status'] == 'PASS'
    # Adversarial saved synthetic evidence: an earlier successful author shares
    # the final reviewer's developer. Keep attempts and provider calls intact.
    for rows in (record['artifact_writers'], record['provenance']['attempts'],
                 record['provenance']['calls']):
        rows[0].update(role=role, served_model=record['editor_model'])
    review_path = tmp_path / plain.DEFAULT_REVIEW_REL
    review_path.write_text(json.dumps(record))
    plain.reset_editor_note_decisions(tmp_path, brief_sha256=record['brief_sha256'], apply_served_judgements=True)
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is False
    assert 'plain_brief_review_not_independent' in plain.verify_campaign_plain_brief(tmp_path, manifest)['holds']
    if remove_all:
        record.pop('artifact_writers')
    else:
        record['artifact_writers'].pop(0)
    review_path.write_text(json.dumps(record))
    plain.reset_editor_note_decisions(tmp_path, brief_sha256=record['brief_sha256'], apply_served_judgements=True)
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is False
    result = plain.verify_campaign_plain_brief(tmp_path, manifest)
    assert 'plain_brief_review_not_independent' in result['holds'], result
    table = {c['generation_id']: {'model': c['served_model'], 'tokens_prompt': 5000}
             for c in record['provenance']['calls']}
    assert receipt_integrity.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


@pytest.mark.parametrize('role', ['writer', 'rewriting_editor'])
@pytest.mark.parametrize('failure', ['truncated', 'rate_limit'])
def test_saved_failed_author_attempt_omission_holds_without_breaking_fallback(tmp_path, role, failure):
    campaign(tmp_path)
    writer_attempts = 0
    def transport(request, timeout):
        nonlocal writer_attempts
        payload = json.loads(request.data)
        is_writer = payload['messages'][0]['content'] != plain.EDITOR_SYS
        if is_writer:
            writer_attempts += 1
            if writer_attempts == 1 and failure == 'rate_limit':
                raise urllib.error.HTTPError(request.full_url, 429, 'synthetic rate limit', Message(),
                                             io.BytesIO(b'{"error":"synthetic limit"}'))
        text = body() if is_writer else json.dumps(editor_pass())
        finish = 'length' if is_writer and writer_attempts == 1 and failure == 'truncated' else 'stop'
        return io.BytesIO(json.dumps({'synthetic': True,
            'id': f'gen-SYNTHETIC-{next(SYNTHETIC_CALL_IDS):04d}', 'model': payload['model'],
            'choices': [{'finish_reason': finish, 'message': {'content': text}}],
            'usage': {'cost': 0}}).encode())
    with offline_plain_transport(plain, transport):
        record = plain.campaign_brief(tmp_path)
    assert record['acceptance_status'] == 'PASS'
    assert record['artifact_writers'][0]['status'] == failure
    for rows in (record['artifact_writers'], record['provenance']['attempts']):
        rows[0]['role'] = role
    if failure == 'truncated':
        record['provenance']['calls'][0]['role'] = role
    review_path = tmp_path / plain.DEFAULT_REVIEW_REL
    review_path.write_text(json.dumps(record))
    plain.reset_editor_note_decisions(tmp_path, brief_sha256=record['brief_sha256'], apply_served_judgements=True)
    manifest = json.loads((tmp_path / 'manifest.json').read_text())
    assert plain.verify_campaign_plain_brief(tmp_path, manifest)['holds'] == []
    table = {c['generation_id']: {'model': c['served_model'], 'tokens_prompt': 5000}
             for c in record['provenance']['calls']}
    assert receipt_integrity.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'PASS'
    record['artifact_writers'].pop(0)
    review_path.write_text(json.dumps(record))
    plain.reset_editor_note_decisions(tmp_path, brief_sha256=record['brief_sha256'], apply_served_judgements=True)
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is False
    result = plain.verify_campaign_plain_brief(tmp_path, manifest)
    assert 'plain_brief_review_not_independent' in result['holds'], result
    assert receipt_integrity.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


def test_generation_rejects_evidence_changed_during_review(tmp_path, monkeypatch):
    campaign(tmp_path)
    citations = tmp_path / 'citations.json'; citations.write_text('{"source": "before"}')
    settings = load_settings(environ={})
    def completion(model, system, user, **kwargs):
        if kwargs['role'] == 'brief_reviewer':
            citations.write_text('{"source": "after"}')
        return result_for(settings, kwargs['role'], body() if kwargs['role'] == 'writer' else json.dumps(editor_pass()))
    monkeypatch.setattr(model_client, 'chat_completion', completion)
    with pytest.raises(ValueError, match='changed during'):
        plain.campaign_brief(tmp_path)
    assert not (tmp_path / plain.DEFAULT_REVIEW_REL).exists()


def test_rewriting_editor_is_preserved_as_an_actual_writer(tmp_path, monkeypatch):
    settings = load_settings(environ={})
    seen = []
    def completion(model, system, user, **kwargs):
        seen.append(kwargs)
        return result_for(settings, kwargs['role'], 'written')
    monkeypatch.setattr(model_client, 'chat_completion', completion)
    with plain.run_context(settings, tmp_path) as state:
        plain.call(None, 's', 'u', role='rewriting_editor')
        plain.call(None, 's', 'u', role='editor')
    assert state.writers[0]['role'] == 'rewriting_editor'
    assert seen[-1]['writers'][0]['role'] == 'rewriting_editor'


@pytest.mark.parametrize('target', ['provider_raw', 'rounds'])
def test_generated_pending_binds_original_response_evidence(tmp_path, monkeypatch, target):
    campaign(tmp_path); install_completion(monkeypatch)
    record = plain.campaign_brief(tmp_path)
    path = (record['provenance']['raw_response_paths'][0] if target == 'provider_raw' else record['rounds_path'])
    source = tmp_path / path
    source.write_text(source.read_text() + '\n')
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is False


def test_corrected_draft_exact_author_packet_is_preserved(tmp_path, monkeypatch):
    campaign(tmp_path); install_completion(monkeypatch)
    draft = tmp_path / 'draft.md'; draft.write_text(body())
    settings = load_settings(environ={})
    authors = tmp_path / 'draft-authors.json'
    calls = result_for(settings, 'rewriting_editor', body()).calls
    authors.write_text(json.dumps({'draft_sha256': plain.sha256_file(draft), 'writer_calls': calls}))
    record = plain.campaign_brief(tmp_path, corrected_draft=draft, corrected_draft_authors=authors)
    assert record['writer_model'] == calls[0]['served_model']
    assert record['artifact_writers'] == calls
    assert record['acceptance_status'] == 'PASS'
    assert record['writer_skipped'] == 'corrected_draft'
    assert 'draft-authors.json' in record['evidence_inputs']
    table = {call['generation_id']: {'model': call['served_model'], 'tokens_prompt': 5000}
             for call in record['provenance']['calls']}
    assert [call['role'] for call in record['provenance']['calls']] == ['rewriting_editor', 'brief_reviewer']
    assert receipt_integrity.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'PASS'
    draft.write_text(body() + '\nAn unreviewed addition.\n')
    with pytest.raises(ValueError, match='author packet'):
        plain.campaign_brief(tmp_path, corrected_draft=draft, corrected_draft_authors=authors)


def test_local_parser_recovers_valid_object_without_model_call():
    payload = editor_pass()
    raw = 'Preface with {not JSON}.\n```json\n' + json.dumps(payload) + '\n```\nEnd.'
    assert plain.parse_editor_response(raw) == payload


@pytest.mark.parametrize('field,value', [
    ('verdict', 'MAYBE'), ('verdict', True), ('verdict', None),
    ('blocking', None), ('blocking', 'none'), ('blocking', [42]),
    ('minor', None), ('minor', 'none'), ('minor', [False]),
    ('checks.factual.basis', 42), ('checks.reasoning.basis', True),
    ('checks.editorial.basis', ['evidence']), ('checks.presentation.basis', '  '),
    ('checks.factual.verdict', 'pass'),
    ('important_quantity_qualification_review.basis', 42),
    ('important_quantity_qualification_review.verdict', True),
])
def test_schema_invalid_review_stops_before_any_paid_repair(tmp_path, field, value):
    review = editor_pass()
    node = review
    *parents, key = field.split('.')
    for parent in parents:
        node = node[parent]
    node[key] = value
    raw = json.dumps(review)
    seen = []
    def respond(role, system, user):
        seen.append(role)
        return body() if role == 'writer' else raw
    with offline_plain_transport(plain, responder=respond):
        _, final = plain.rewrite('units', {'slug': 'units', 'title': 'Units', 'sources': []}, tmp_path)
    assert seen == ['writer', 'brief_reviewer']
    assert final['editor'] == 'FAIL'
    assert (tmp_path / 'units.editor-r0.raw.txt').read_text() == raw
    saved = json.loads((tmp_path / 'units.review.json').read_text())
    assert saved['rounds'][0]['editor'] == review  # no synthetic factual judgement
    assert saved['rounds'][0]['schema_valid'] is False
    assert saved['rounds'][0]['schema_errors']
    assert (tmp_path / 'units.md').read_text() == body()


@pytest.mark.parametrize('field', ['blocking', 'minor'])
def test_schema_invalid_arrays_preserve_raw_campaign_review_without_crashing(tmp_path, field):
    campaign(tmp_path)
    review = editor_pass()
    review[field] = None
    seen = []
    def respond(role, system, user):
        seen.append(role)
        return body() if role == 'writer' else json.dumps(review)
    with offline_plain_transport(plain, responder=respond):
        record = plain.campaign_brief(tmp_path)
    assert seen == ['writer', 'brief_reviewer']
    assert record['status'] == 'FAIL'
    assert record['acceptance_status'] == 'HOLD'
    assert record['schema_valid'] is False
    assert record['schema_errors']
    rounds = json.loads((tmp_path / record['rounds_path']).read_text())
    assert rounds['rounds'][0]['editor'] == review
    assert record['retained_findings'] == []


@pytest.mark.parametrize('flag', ['true', 'false', 1, None, False])
def test_reviewer_acceptance_flag_must_be_literal_true(tmp_path, monkeypatch, flag):
    settings = load_settings(environ={})
    seen = []
    def completion(model, system, user, **kwargs):
        seen.append(kwargs['role'])
        return result_for(settings, kwargs['role'], body() if kwargs['role'] == 'writer'
                          else json.dumps(editor_pass()), eligible=flag)
    monkeypatch.setattr(model_client, 'chat_completion', completion)
    _, final = plain.rewrite('units', {'slug': 'units', 'title': 'Units', 'sources': []}, tmp_path)
    assert final['editor'] == 'FAIL'
    assert seen == ['writer', 'brief_reviewer']


def test_within_generation_all_round_findings_require_saved_disposition(tmp_path):
    campaign(tmp_path)
    first = editor_pass()
    first.update(verdict='FAIL', blocking=['Keep the byte unit.'], minor=['Shorten the heading.'])
    reviews = iter([first, editor_pass()])
    roles = []
    def respond(role, system, user):
        roles.append(role)
        if role == 'writer':
            return body()
        review = next(reviews)
        if review['blocking']:
            # A genuine typed unresolved first round permits the ordinary writer
            # correction loop; an untyped second round cannot erase its findings.
            from test_review_judgements import reply
            typed = json.loads(reply(user, review['blocking'] + review['minor'], 'apply_correction', applied=False))
            review.update(note_dispositions=typed['note_dispositions'], review_binding=typed['review_binding'])
        return json.dumps(review)
    with offline_plain_transport(plain, responder=respond):
        record = plain.campaign_brief(tmp_path)
    assert roles == ['writer', 'brief_reviewer', 'writer', 'brief_reviewer']
    assert record['status'] == 'FAIL'
    assert any('note_dispositions' in error for error in record['schema_errors'])
    assert record['acceptance_status'] == 'HOLD'
    expected = ['Keep the byte unit.', 'Shorten the heading.']
    assert record['retained_findings'] == expected
    pending = json.loads((tmp_path / 'qa/editor-note-decisions.json').read_text())
    assert pending['retained_findings'] == expected
    assert pending['status'] == 'pending'
    assert campaign_readiness(tmp_path)['publication_qa_allowed'] is False


def test_generated_mixed_reviewer_rounds_verify_through_real_shared_transport(tmp_path):
    campaign(tmp_path)
    settings = load_settings(environ={})
    editor_calls = 0
    table = {}
    def transport(request, timeout):
        nonlocal editor_calls
        payload = json.loads(request.data)
        is_editor = payload['messages'][0]['content'] == plain.EDITOR_SYS
        if is_editor:
            editor_calls += 1
            if editor_calls == 2:
                raise urllib.error.HTTPError(request.full_url, 429, 'synthetic rate limit', {},
                                             io.BytesIO(b'{"error":"synthetic limit"}'))
        review = editor_pass()
        if is_editor and editor_calls == 1:
            review.update(verdict='FAIL', blocking=['Keep the byte unit.'])
        if is_editor:
            from test_review_judgements import reply
            user = payload['messages'][1]['content']
            typed = json.loads(reply(user, review['blocking'], 'already_resolved'))
            review.update(note_dispositions=typed['note_dispositions'], review_binding=typed['review_binding'])
        text = json.dumps(review) if is_editor else body()
        gid = f'gen-SYNTHETIC-{next(SYNTHETIC_CALL_IDS):04d}'
        table[gid] = {'model': payload['model'], 'tokens_prompt': 5000}
        return io.BytesIO(json.dumps({'synthetic': True, 'id': gid, 'model': payload['model'],
            'choices': [{'finish_reason': 'stop', 'message': {'content': text}}], 'usage': {'cost': 0}}).encode())
    with offline_plain_transport(plain, transport):
        record = plain.campaign_brief(tmp_path)
    assert record['status'] == 'PASS'
    provenance = record['provenance']
    assert provenance['schema_version'] == 'deep-research-served-calls-v1'
    assert [c['role'] for c in provenance['calls']] == ['writer', 'brief_reviewer', 'writer', 'brief_reviewer']
    assert len(provenance['attempts']) == 5
    assert provenance['attempts'][-2]['status'] == 'rate_limit'
    assert provenance['calls'][1]['served_model'] != record['editor_model']
    assert provenance['final_editor_generation_id'] == provenance['calls'][-1]['generation_id']
    result = receipt_integrity.verify_provenance(record, tmp_path, fetch=table.get)
    assert result['verdict'] == 'PASS', result
