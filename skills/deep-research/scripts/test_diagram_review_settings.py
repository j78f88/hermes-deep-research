"""Diagram integration contracts; synthetic pixels and fake HTTP only."""
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

import diagram_review_native as diagram
import model_client
import receipt_integrity
from research_settings import load_settings
from test_model_client import response, transport_for


def fixtures(tmp_path, writers=None):
    (tmp_path / 'test.architecture.json').write_text(json.dumps({'components': [], 'connections': []}))
    (tmp_path / 'brief.json').write_text(json.dumps({'question': 'Synthetic unit test', 'image_answerable_questions': []}))
    for suffix in ('', '-top', '-bottom'):
        (tmp_path / f'test{suffix}.png').write_bytes(b'synthetic-test-pixels')
    writer_file = tmp_path / 'writers.json'
    writer_file.write_text(json.dumps(writers if writers is not None else [{
        'role': 'writer', 'status': 'success', 'identity_verified': True,
        'served_model': 'anthropic/claude-opus-5.5', 'generation_id': 'gen-synthetic-writer'}]))
    return writer_file


def invoke(monkeypatch, tmp_path, writer_file, text=None, served='openai/gpt-6.1-sol-pro', *,
           provider=None, reviewer_model=None):
    monkeypatch.setattr(sys, 'argv', ['diagram_review_native.py', '--dir', str(tmp_path),
        '--name', 'test', '--writer-calls', str(writer_file)])
    overrides: dict = {'execution': {'concurrency': 1}}
    if reviewer_model:
        overrides['roles'] = {'visual_reviewer': {'model': reviewer_model, 'fallback_models': []}}
    conf = load_settings(environ={}, overrides=overrides)
    monkeypatch.setattr(diagram, 'load_settings', lambda **kw: conf, raising=False)
    declared = json.loads(writer_file.read_text())
    if isinstance(declared, dict):
        declared = declared.get('calls', [])
    if provider is None:
        records = {r['generation_id']: {'model': r.get('served_model')}
                   for r in declared if isinstance(r, dict) and r.get('generation_id')}
        provider = records.get
    monkeypatch.setattr(receipt_integrity, 'fetch_generation', provider)
    verdict = text or json.dumps({'verdict': 'approve', 'confidence': 'high', 'issues': [], 'required_fixes': []})
    seen = []
    def fake_chat(*args, **kwargs):
        # The real shared client runs; only the HTTP transport is replaced.
        seen.append(kwargs)
        return model_client.chat_completion(*args, **kwargs, api_key='unit-test-key',
            transport=transport_for(response(served, verdict)))
    monkeypatch.setattr(diagram, 'chat_completion', fake_chat, raising=False)
    result = diagram.main()
    return result, seen


def test_native_diagram_uses_shared_settings_and_verified_served_independence(tmp_path, monkeypatch):
    writers = fixtures(tmp_path)
    code, seen = invoke(monkeypatch, tmp_path, writers)
    assert code == 0
    assert len(seen) == 3
    assert all(k['role'] == 'visual_reviewer' for k in seen)
    review = json.loads((tmp_path / 'review/test-visual-review.json').read_text())
    assert review['verdict'] == 'approve'
    assert review['reviewer_model'] == 'openai/gpt-6.1-sol-pro'
    assert review['acceptance_eligible'] is True
    assert (tmp_path / 'review/effective-settings.json').exists()
    assert (tmp_path / 'review/test-full-raw.json').exists()


def test_diagram_raw_paid_response_saved_before_application_json_parse(tmp_path, monkeypatch):
    writers = fixtures(tmp_path)
    code, seen = invoke(monkeypatch, tmp_path, writers, text='not your expected JSON schema')
    assert code == 1
    assert len(seen) == 3  # one per requested view, no repeat for a local parser failure
    raw = json.loads((tmp_path / 'review/test-full-raw.json').read_text())
    assert raw['raw_responses'][0]['choices'][0]['message']['content'] == 'not your expected JSON schema'
    assert json.loads((tmp_path / 'review/test-visual-review.json').read_text())['verdict'] == 'reject'


def test_diagram_unknown_served_model_cannot_approve(tmp_path, monkeypatch):
    code, _ = invoke(monkeypatch, tmp_path, fixtures(tmp_path), served='unknown/provider-model')
    assert code == 1
    assert json.loads((tmp_path / 'review/test-visual-review.json').read_text())['acceptance_eligible'] is False


def test_builder_requested_pin_alone_is_not_writer_provenance(tmp_path, monkeypatch):
    fixtures(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['diagram_review_native.py', '--dir', str(tmp_path),
        '--name', 'test', '--builder-model', 'anthropic/claude-opus-5.5'])
    monkeypatch.setattr(diagram, 'call', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('must not pay for unaccepting review')))
    assert diagram.main() == 2


def test_diagram_same_developer_writing_editor_is_rejected_before_transport(tmp_path, monkeypatch):
    writers = fixtures(tmp_path, [{'role': 'rewriting_editor', 'status': 'success',
        'served_model': 'openai/gpt-6.1-terra-pro', 'generation_id': 'gen-synthetic-editor', 'identity_verified': True}])
    code, seen = invoke(monkeypatch, tmp_path, writers)
    assert code == 2
    assert seen == []


@pytest.mark.parametrize('provider_record', [None, {}, {'model': 'unknown/provider-model'},
    {'model': 'openai/gpt-6.1-terra-pro'}, ['not-a-provider-record']])
def test_caller_boolean_cannot_authenticate_nonexistent_unknown_or_mismatched_writer(
        tmp_path, monkeypatch, provider_record):
    checked = []
    def fetch(gid):
        checked.append(gid)
        return provider_record
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path), provider=fetch)
    assert code == 2
    assert seen == []
    assert checked == ['gen-synthetic-writer']
    review = json.loads((tmp_path / 'review/test-visual-review.json').read_text())
    assert review['verdict'] == 'reject'
    assert review['acceptance_eligible'] is False
    assert review['writer_verification']['verdict'] != 'PASS'
    assert review['inspected_views'] == []


@pytest.mark.parametrize('error', [
    urllib.error.HTTPError('https://unit.invalid', 401, 'synthetic-secret', Message(), None),
    urllib.error.HTTPError('https://unit.invalid', 403, 'synthetic-secret', Message(), None),
    RuntimeError('synthetic-secret provider unavailable')])
def test_provider_auth_or_lookup_failure_stops_before_shared_review_without_saving_error_text(
        tmp_path, monkeypatch, capsys, error):
    checked = []
    def fetch(gid):
        checked.append(gid)
        raise error
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path), provider=fetch)
    assert code == 2
    assert seen == []
    assert checked == ['gen-synthetic-writer']
    saved = (tmp_path / 'review/test-visual-review.json').read_text()
    review = json.loads(saved)
    assert review['acceptance_eligible'] is False
    assert review['writer_verification']['verdict'] == 'HOLD'
    assert 'synthetic-secret' not in saved + capsys.readouterr().err


@pytest.mark.parametrize('change', [
    {'generation_id': 'not-a-generation'}, {'generation_id': ''},
    {'generation_id': None}, {'identity_verified': False},
    {'served_model': 'unknown/provider-model'}, {'status': 'not-a-call-status'}])
def test_invalid_writer_inventory_is_rejected_without_provider_or_reviewer_calls(tmp_path, monkeypatch, change):
    writer_file = fixtures(tmp_path)
    writers = json.loads(writer_file.read_text())
    writers[0].update(change)
    writer_file.write_text(json.dumps(writers))
    checked = []
    code, seen = invoke(monkeypatch, tmp_path, writer_file, provider=lambda gid: checked.append(gid))
    assert code == 2
    assert seen == []
    assert checked == []


@pytest.mark.parametrize('inventory', [[], {'calls': []}, [None]])
def test_absent_writer_inventory_is_nonaccepting(tmp_path, monkeypatch, inventory):
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path, inventory), provider=lambda gid: None)
    assert code == 2
    assert seen == []
    review = json.loads((tmp_path / 'review/test-visual-review.json').read_text())
    assert review['writer_verification']['verdict'] != 'PASS'
    assert review['acceptance_eligible'] is False


def test_faked_developer_field_cannot_hide_provider_confirmed_same_developer(tmp_path, monkeypatch):
    writers = [{'role': 'rewriting_editor', 'status': 'success', 'identity_verified': True,
        'served_model': 'openai/gpt-6.1-terra-pro', 'developer': 'anthropic',
        'generation_id': 'gen-synthetic-editor'}]
    checked = []
    def fetch(gid):
        checked.append(gid)
        return {'model': 'openai/gpt-6.1-terra-pro', 'developer': 'anthropic', 'credential': 'synthetic-secret'}
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path, writers), provider=fetch)
    assert code == 2
    assert seen == []
    assert checked == ['gen-synthetic-editor']
    saved = (tmp_path / 'review/test-visual-review.json').read_text()
    review = json.loads(saved)
    assert review['writer_verification']['calls'][0]['developer'] == 'openai'
    assert review['acceptance_eligible'] is False
    assert 'synthetic-secret' not in saved


def test_all_declared_writers_rewriting_editors_and_fallbacks_are_authenticated(tmp_path, monkeypatch):
    writers = [
        {'role': 'writer', 'status': 'timeout', 'served_model': None, 'generation_id': None,
         'identity_verified': False},
        {'role': 'writer', 'status': 'success', 'served_model': 'anthropic/claude-opus-5.5',
         'requested_model': 'openai/gpt-6.1-terra-pro', 'fallback_from': 'openai/gpt-6.1-terra-pro',
         'generation_id': 'gen-synthetic-fallback', 'identity_verified': True},
        {'role': 'rewriting_editor', 'status': 'success', 'served_model': 'deepseek/deepseek-v4-pro',
         'generation_id': 'gen-synthetic-rewriter', 'identity_verified': True},
        {'role': 'writer', 'status': 'success', 'served_model': 'google/gemini-3.8-flash',
         'generation_id': 'gen-synthetic-otherwriter', 'identity_verified': True}]
    records = {w['generation_id']: {'model': w['served_model'], 'credential': 'synthetic-secret'}
               for w in writers if w['generation_id']}
    checked = []
    def fetch(gid):
        checked.append(gid)
        return records[gid]
    original_chat = model_client.chat_completion
    def after_auth(*args, **kwargs):
        assert checked == list(records)
        return original_chat(*args, **kwargs)
    monkeypatch.setattr(model_client, 'chat_completion', after_auth)
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path, {'calls': writers}), provider=fetch)
    assert code == 0
    assert len(seen) == 3
    assert checked == list(records)  # one fresh lookup per contributing generation, before all views
    saved = (tmp_path / 'review/test-visual-review.json').read_text()
    verification = json.loads(saved)['writer_verification']
    assert verification['verdict'] == 'PASS'
    assert [r['generation_id'] for r in verification['calls']] == list(records)
    assert {r['developer'] for r in verification['calls']} == {'anthropic', 'deepseek', 'google'}
    assert 'synthetic-secret' not in saved


@pytest.mark.parametrize('role', ['writer', 'rewriting_editor'])
def test_second_contributor_provider_mismatch_blocks_review(tmp_path, monkeypatch, role):
    writer_file = fixtures(tmp_path)
    writers = json.loads(writer_file.read_text()) + [{'role': role, 'status': 'success',
        'served_model': 'deepseek/deepseek-v4-pro', 'identity_verified': True,
        'generation_id': 'gen-synthetic-second', 'fallback_from': 'anthropic/claude-opus-5.5'}]
    checked = []
    def fetch(gid):
        checked.append(gid)
        return {'model': 'anthropic/claude-opus-5.5'}
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path, writers), provider=fetch)
    assert code == 2
    assert seen == []
    assert checked == ['gen-synthetic-writer', 'gen-synthetic-second']


@pytest.mark.parametrize('status', ['success', 'truncated'])
def test_actual_reviewer_developer_must_differ_from_every_authenticated_writer(tmp_path, monkeypatch, status):
    writer_file = fixtures(tmp_path)
    writers = json.loads(writer_file.read_text())
    writers[0]['served_model'] = 'deepseek/deepseek-v4-pro'
    writers.append({'role': 'rewriting_editor', 'status': status,
        'served_model': 'openai/gpt-6.1-terra-pro', 'identity_verified': True,
        'generation_id': 'gen-synthetic-rewriter'})
    # The valid returned reviewer can match a truncated contribution that the
    # shared client's structural author set excludes. The wrapper must reject it.
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path, writers),
                        reviewer_model='anthropic/claude-opus-5.5', served='openai/gpt-6.1-terra-pro')
    assert code == 1
    assert len(seen) == 3
    raw = json.loads((tmp_path / 'review/test-full-raw.json').read_text())
    assert raw['calls'][-1]['capability_verified'] is True
    assert raw['calls'][-1]['identity_verified'] is True
    review = json.loads((tmp_path / 'review/test-visual-review.json').read_text())
    assert review['acceptance_eligible'] is False
    assert review['verdict'] == 'reject'
    assert review['writer_verification']['verdict'] == 'PASS'


def test_declared_generation_cannot_evade_authentication_by_claiming_failed_status(tmp_path, monkeypatch):
    writer_file = fixtures(tmp_path)
    writers = json.loads(writer_file.read_text()) + [{'role': 'rewriting_editor', 'status': 'truncated',
        'served_model': 'openai/gpt-6.1-terra-pro', 'identity_verified': True,
        'generation_id': 'gen-synthetic-truncated'}]
    checked = []
    def fetch(gid):
        checked.append(gid)
        return {'model': next(w['served_model'] for w in writers if w['generation_id'] == gid)}
    code, seen = invoke(monkeypatch, tmp_path, fixtures(tmp_path, writers), provider=fetch)
    assert code == 2
    assert seen == []
    assert checked == ['gen-synthetic-writer', 'gen-synthetic-truncated']
