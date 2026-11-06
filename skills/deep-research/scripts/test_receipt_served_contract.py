"""Offline synthetic receipts exercise modern served-call and legacy contracts."""
import copy
import hashlib
import json
import urllib.request

import pytest
import receipt_integrity as ri
from publication_test_support import admit_frozen_legacy

CONTRACT = 'deep-research-served-calls-v1'


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('live network forbidden in synthetic receipt tests')
    monkeypatch.setattr(urllib.request, 'urlopen', deny)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', deny)
    monkeypatch.setattr(ri, '_api_key', lambda: 'synthetic-offline-only')
    monkeypatch.setattr(ri, 'fetch_model_catalogue', lambda: [])


def receipt(root):
    (root / 'brief.md').write_text('Brief evidence. ' * 200)
    (root / 'pack.md').write_text('Full evidence. ' * 200)
    def call(number, role, model, status='success'):
        return {'generation_id': f'gen-SYNTHETIC-{number:04d}', 'role': role,
                'status': status, 'served_model': model, 'identity_verified': True}
    calls = [call(1, 'writer', 'anthropic/claude-opus-5.5', 'truncated'),
             call(2, 'writer', 'google/gemini-3.8-flash'),
             call(3, 'brief_reviewer', 'openai/gpt-6.1-sol-pro'),
             call(4, 'writer', 'anthropic/claude-opus-5.5'),
             call(5, 'brief_reviewer', 'openai/gpt-6.1-terra-pro')]
    record = {'status': 'PASS', 'editor_verdict': 'PASS', 'acceptance_eligible': True,
              'brief_path': 'brief.md', 'full_pack_path': 'pack.md',
              'brief_sha256': hashlib.sha256((root / 'brief.md').read_bytes()).hexdigest(),
              'writer_model': calls[3]['served_model'], 'editor_model': calls[-1]['served_model'],
              'artifact_writers': copy.deepcopy([c for c in calls if c['role'] == 'writer']),
              'provenance': {'schema_version': CONTRACT, 'calls': calls,
                             'attempts': copy.deepcopy(calls),
                             'final_editor_generation_id': calls[-1]['generation_id']}}
    table = {c['generation_id']: {'model': c['served_model'], 'tokens_prompt': 5000} for c in calls}
    return record, table


def test_mixed_served_models_each_verify_against_their_own_call(tmp_path):
    record, table = receipt(tmp_path)
    seen = []
    def fetch(gid):
        seen.append(gid)
        return table.get(gid)
    result = ri.verify_provenance(record, tmp_path, fetch=fetch)
    assert result['verdict'] == 'PASS', result
    assert seen == [c['generation_id'] for c in record['provenance']['calls']]
    assert [c['role'] for c in result['calls']] == [c['role'] for c in record['provenance']['calls']]


def test_an_earlier_full_review_cannot_cover_final_editor_token_shortfall(tmp_path):
    record, table = receipt(tmp_path)
    table[record['provenance']['final_editor_generation_id']]['tokens_prompt'] = 1
    result = ri.verify_provenance(record, tmp_path, fetch=table.get)
    assert result['class'] == 'receipt_fabricated'
    assert any('final editor' in reason for reason in result['reasons'])


@pytest.mark.parametrize('index', range(5))
def test_modern_served_identity_is_exact_not_a_legacy_model_alias(tmp_path, index):
    record, table = receipt(tmp_path)
    table[record['provenance']['calls'][index]['generation_id']]['model'] += '-different'
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


def test_call_flags_cannot_use_numeric_equality_to_launder_verification(tmp_path):
    record, table = receipt(tmp_path)
    record['provenance']['calls'][-1]['identity_verified'] = 1
    assert record['provenance']['attempts'][-1]['identity_verified'] is True
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


@pytest.mark.parametrize('index', range(5))
def test_every_served_call_including_truncated_is_provider_checked(tmp_path, index):
    record, table = receipt(tmp_path)
    table[record['provenance']['calls'][index]['generation_id']]['model'] = 'mistralai/mistral-large'
    result = ri.verify_provenance(record, tmp_path, fetch=table.get)
    assert result['class'] == 'receipt_fabricated'
    assert any('mistral' in reason for reason in result['reasons'])


@pytest.mark.parametrize('change', ['missing_final', 'earlier_final', 'missing_attempt', 'role_laundering',
                                   'declared_final_model', 'declared_writer_model', 'duplicate_id'])
def test_modern_contract_rejects_inconsistent_saved_evidence(tmp_path, change):
    record, table = receipt(tmp_path)
    provenance = record['provenance']
    if change == 'missing_final':
        provenance.pop('final_editor_generation_id')
    elif change == 'earlier_final':
        provenance['final_editor_generation_id'] = provenance['calls'][2]['generation_id']
    elif change == 'missing_attempt':
        provenance['calls'].pop(0)
    elif change == 'role_laundering':
        provenance['calls'][0]['role'] = 'unrelated'
    elif change == 'declared_final_model':
        record['editor_model'] = provenance['calls'][2]['served_model']
    elif change == 'declared_writer_model':
        record['writer_model'] = provenance['calls'][1]['served_model']
    else:
        provenance['calls'][-1]['generation_id'] = provenance['calls'][2]['generation_id']
        provenance['attempts'][-1]['generation_id'] = provenance['calls'][2]['generation_id']
        provenance['final_editor_generation_id'] = provenance['calls'][2]['generation_id']
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


@pytest.mark.parametrize('target,value', [('acceptance_eligible', 'true'), ('acceptance_eligible', 1),
                                         ('acceptance_eligible', False), ('status', 'FAIL'),
                                         ('editor_verdict', 'FAIL'), ('identity_verified', 'true'),
                                         ('identity_verified', False), ('call_status', 'truncated')])
def test_final_editor_must_be_successful_verified_and_accepted(tmp_path, target, value):
    record, table = receipt(tmp_path)
    if target in ('identity_verified', 'call_status'):
        key = 'status' if target == 'call_status' else target
        record['provenance']['calls'][-1][key] = value
        record['provenance']['attempts'][-1][key] = value
    else:
        record[target] = value
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] != 'PASS'


def test_corrected_draft_supplied_rewriting_author_is_checked_without_role_laundering(tmp_path):
    record, table = receipt(tmp_path)
    author = record['provenance']['calls'][3]
    author['role'] = 'rewriting_editor'
    editor = record['provenance']['calls'][-1]
    record['artifact_writers'] = [copy.deepcopy(author)]
    record['provenance']['calls'] = [author, editor]
    record['provenance']['attempts'] = [copy.deepcopy(editor)]
    record.update(writer_skipped='corrected_draft', corrected_draft_path='draft.md', body_parity=True)
    (tmp_path / 'draft.md').write_bytes((tmp_path / 'brief.md').read_bytes())
    result = ri.verify_provenance(record, tmp_path, fetch=table.get)
    assert result['verdict'] == 'PASS', result
    table[author['generation_id']]['model'] = 'mistralai/mistral-large'
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


@pytest.mark.parametrize('role', ['writer', 'rewriting_editor'])
@pytest.mark.parametrize('status', ['success', 'truncated', 'rate_limit'])
def test_every_inrun_author_attempt_must_be_in_artifact_writers(tmp_path, role, status):
    record, table = receipt(tmp_path)
    provenance = record['provenance']
    author = provenance['attempts'][0]
    author.update(role=role, status=status)
    provenance['calls'][0] = copy.deepcopy(author)
    record['artifact_writers'][0] = copy.deepcopy(author)
    if status == 'rate_limit':
        # Failed attempts without provider IDs are still tracked as writer attempts.
        author.update(generation_id=None, served_model=None, identity_verified=False)
        record['artifact_writers'][0] = copy.deepcopy(author)
        provenance['calls'].pop(0)
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'PASS'
    record['artifact_writers'].pop(0)
    seen = []
    def fetch(gid):
        seen.append(gid)
        return table.get(gid)
    result = ri.verify_provenance(record, tmp_path, fetch=fetch)
    assert result['verdict'] == 'FAIL', result
    assert result['class'] == 'receipt_fabricated'
    assert any('artifact_writers' in reason for reason in result['reasons'])
    assert seen == []  # saved author omission cannot be laundered by provider success


@pytest.mark.parametrize('role', ['writer', 'rewriting_editor'])
@pytest.mark.parametrize('actual,claimed', [(True, 1), (False, 0), (1, 1.0),
                                          ({'nested': [True]}, {'nested': [1]})])
def test_inrun_author_inclusion_requires_exact_json_identity(tmp_path, role, actual, claimed):
    record, table = receipt(tmp_path)
    for rows in (record['provenance']['calls'], record['provenance']['attempts'],
                 record['artifact_writers']):
        rows[0].update(role=role, synthetic_metadata=copy.deepcopy(actual))
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'PASS'
    record['artifact_writers'][0]['synthetic_metadata'] = claimed
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


def test_legacy_role_wide_model_and_any_editor_budget_semantics_unchanged(tmp_path, monkeypatch):
    record, table = receipt(tmp_path)
    admit_frozen_legacy(monkeypatch, tmp_path)
    calls = record['provenance']['calls']
    legacy_calls = [dict(calls[3]), dict(calls[2], role='editor'), dict(calls[4], role='editor')]
    record['provenance'] = {'calls': legacy_calls}
    record['editor_model'] = calls[2]['served_model']
    table[calls[4]['generation_id']]['model'] = record['editor_model']
    table[calls[4]['generation_id']]['tokens_prompt'] = 1
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'PASS'
    manifest_path = tmp_path / 'manifest.json'
    frozen = manifest_path.read_bytes()
    changed = json.loads(frozen)
    changed['target'] += ' changed'
    manifest_path.write_text(json.dumps(changed))
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'HOLD'
    manifest_path.write_bytes(frozen)
    table[calls[2]['generation_id']]['model'] = 'mistralai/mistral-large'
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'
