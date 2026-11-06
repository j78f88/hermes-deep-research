"""Exact served identities or fresh official id/canonical_slug pairs only."""
import copy
import json
import urllib.request

import pytest
import diagram_review_native as diagram
import receipt_integrity as ri
from research_settings import load_settings
from test_receipt_served_contract import receipt

ALIAS = 'anthropic/claude-opus-5.5'
CANONICAL = ALIAS + '-20260921'
CATALOGUE = [{'id': ALIAS, 'canonical_slug': CANONICAL}]


def catalogue(monkeypatch, rows=CATALOGUE):
    seen = []
    def fetch():
        seen.append('official-catalogue')
        if isinstance(rows, Exception):
            raise rows
        return copy.deepcopy(rows)
    monkeypatch.setattr(ri, 'fetch_model_catalogue', fetch, raising=False)
    return seen


def boundary(kind, root, monkeypatch, served=CANONICAL):
    if kind == 'diagram':
        writers = [{'role': 'writer', 'status': 'success', 'identity_verified': True,
                    'served_model': ALIAS, 'generation_id': 'gen-SYNTHETIC-writer'}]
        return diagram.verify_writer_calls(load_settings(environ={}), writers,
                                           fetch=lambda gid: {'model': served})
    record, table = receipt(root)
    if kind == 'judgement':
        call = {'role': 'reviewer', 'status': 'success', 'identity_verified': True,
                'served_model': 'openai/gpt-6.1-sol-pro', 'generation_id': 'gen-SYNTHETIC-judgement'}
        record['provenance']['calls'].append(copy.deepcopy(call))
        record['provenance']['attempts'].append(copy.deepcopy(call))
        table[call['generation_id']] = {'model': served, 'tokens_prompt': 5000}
    else:
        for call in record['provenance']['calls']:
            if call['served_model'] == ALIAS:
                table[call['generation_id']]['model'] = served
    before = copy.deepcopy(record)
    result = ri.verify_provenance(record, root, fetch=table.get)
    assert record == before, 'verification must not rewrite saved identities'
    return result


@pytest.mark.parametrize('kind', ['brief', 'diagram'])
def test_fresh_official_alias_pair_accepts_real_generation_identity(tmp_path, monkeypatch, kind):
    seen = catalogue(monkeypatch)
    result = boundary(kind, tmp_path, monkeypatch)
    assert result['verdict'] == 'PASS', result
    assert seen == ['official-catalogue']
    assert any(row['served_model'] == CANONICAL for row in result['calls'])


def test_classification_judgements_share_the_same_identity_semantics(tmp_path, monkeypatch):
    alias = 'openai/gpt-6.1-sol-pro'
    canonical = alias + '-20260929'
    seen = catalogue(monkeypatch, [{'id': alias, 'canonical_slug': canonical}])
    assert boundary('judgement', tmp_path, monkeypatch, canonical)['verdict'] == 'PASS'
    assert len(seen) == 1


@pytest.mark.parametrize('reverse', [False, True])
def test_equivalence_primitive_is_symmetric_and_exact(monkeypatch, reverse):
    seen = catalogue(monkeypatch)
    verifier = ri.ModelIdentityVerifier()
    left, right = (CANONICAL, ALIAS) if reverse else (ALIAS, CANONICAL)
    assert verifier.matches(left, right) is True
    assert verifier.matches(left, right) is True
    assert len(seen) == 1


@pytest.mark.parametrize('kind', ['brief', 'diagram'])
@pytest.mark.parametrize('rows,served', [
    (CATALOGUE, ALIAS + '-20260922'),
    (CATALOGUE, ALIAS + ':extended'),
    (CATALOGUE, ALIAS + '-20260921-extra'),
    ([], CANONICAL),
    ([{'id': ALIAS}], CANONICAL),
    (CATALOGUE + [{'id': ALIAS, 'canonical_slug': [CANONICAL]}], CANONICAL),
    (CATALOGUE + [{'id': [ALIAS], 'canonical_slug': CANONICAL}], CANONICAL),
    (CATALOGUE + [{'id': ALIAS, 'canonical_slug': ALIAS + '-20260922'}], CANONICAL),
    ([{'id': ALIAS, 'canonical_slug': CANONICAL}, {'id': CANONICAL, 'canonical_slug': ALIAS + '-other'}], CANONICAL),
    (CATALOGUE, ''),
])
def test_unproven_or_ambiguous_pair_fails_closed(tmp_path, monkeypatch, kind, rows, served):
    catalogue(monkeypatch, rows)
    assert boundary(kind, tmp_path, monkeypatch, served)['verdict'] == 'FAIL'


@pytest.mark.parametrize('kind', ['brief', 'diagram'])
def test_unavailable_catalogue_holds_instead_of_accusing_fabrication(tmp_path, monkeypatch, kind):
    seen = catalogue(monkeypatch, OSError('synthetic-secret lookup unavailable'))
    result = boundary(kind, tmp_path, monkeypatch)
    assert result['verdict'] == 'HOLD', result
    if kind == 'brief':
        assert result['class'] == 'receipt_unverified'
    assert len(seen) == 1
    assert 'synthetic-secret' not in json.dumps(result)


@pytest.mark.parametrize('kind', ['brief', 'diagram'])
def test_cross_developer_mismatch_is_rejected_without_catalogue(tmp_path, monkeypatch, kind):
    seen = catalogue(monkeypatch, RuntimeError('must not fetch'))
    assert boundary(kind, tmp_path, monkeypatch, 'openai/gpt-6.1-sol-pro')['verdict'] == 'FAIL'
    assert seen == []


@pytest.mark.parametrize('kind', ['brief', 'diagram'])
def test_exact_identity_does_not_depend_on_catalogue(tmp_path, monkeypatch, kind):
    seen = catalogue(monkeypatch, RuntimeError('must not fetch'))
    assert boundary(kind, tmp_path, monkeypatch, ALIAS)['verdict'] == 'PASS'
    assert seen == []


def test_catalogue_cache_is_only_per_verification(tmp_path, monkeypatch):
    seen = catalogue(monkeypatch)
    assert boundary('brief', tmp_path, monkeypatch)['verdict'] == 'PASS'
    assert boundary('brief', tmp_path, monkeypatch)['verdict'] == 'PASS'
    assert len(seen) == 2


def test_caller_supplied_canonical_and_verification_boolean_cannot_authenticate(tmp_path, monkeypatch):
    record, table = receipt(tmp_path)
    for call in record['provenance']['calls']:
        if call['served_model'] == ALIAS:
            table[call['generation_id']]['model'] = CANONICAL
    record.update(canonical_model=CANONICAL, catalogue_verified=True,
                  catalogue={'models': CATALOGUE, 'source_url': 'https://openrouter.ai/api/v1/models'})
    catalogue(monkeypatch, [])
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['verdict'] == 'FAIL'


def test_unknown_generation_stays_fabricated_with_alias_catalogue(tmp_path, monkeypatch):
    record, table = receipt(tmp_path)
    table.pop(record['provenance']['calls'][0]['generation_id'])
    catalogue(monkeypatch)
    assert ri.verify_provenance(record, tmp_path, fetch=table.get)['class'] == 'receipt_fabricated'


def test_catalogue_authentication_uses_fixed_public_https_url_not_saved_settings(monkeypatch):
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return 'https://openrouter.ai/api/v1/models'
        def read(self): return json.dumps({'data': CATALOGUE}).encode()
    def open_url(request, **kwargs):
        requests.append(request)
        return Response()
    monkeypatch.setattr(urllib.request, 'urlopen', open_url)
    assert ri.fetch_model_catalogue() == CATALOGUE
    assert requests[0].full_url == 'https://openrouter.ai/api/v1/models'
    assert not requests[0].has_header('Authorization')


def test_redirected_catalogue_is_not_authenticated(monkeypatch):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def geturl(self): return 'https://attacker.invalid/models'
        def read(self): return json.dumps({'data': CATALOGUE}).encode()
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kw: Response())
    with pytest.raises(ValueError):
        ri.fetch_model_catalogue()
