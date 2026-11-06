"""Regression for the real public catalogue's many-aliases-to-one-model rows."""
import pytest

import receipt_integrity as ri
from test_model_alias_provenance import ALIAS, CANONICAL, boundary, catalogue

# Exact id/canonical_slug rows observed at the official endpoint in the live proof.
# Batch transport is another alias of the same canonical model, not ambiguity.
OFFICIAL_ROWS = [
    {'id': 'openai/gpt-6.1-sol-pro', 'canonical_slug': 'openai/gpt-6.1-sol-pro-20260929'},
    {'id': 'openai/gpt-6.1-sol-pro:batch', 'canonical_slug': 'openai/gpt-6.1-sol-pro-20260929'},
    {'id': 'anthropic/claude-opus-5.5', 'canonical_slug': 'anthropic/claude-opus-5.5-20260921'},
    {'id': 'anthropic/claude-opus-5.5:batch', 'canonical_slug': 'anthropic/claude-opus-5.5-20260921'},
]


@pytest.mark.parametrize('kind', ['brief', 'diagram', 'judgement'])
def test_actual_official_batch_aliases_do_not_reject_genuine_calls(tmp_path, monkeypatch, kind):
    seen = catalogue(monkeypatch, OFFICIAL_ROWS)
    served = 'openai/gpt-6.1-sol-pro-20260929' if kind == 'judgement' else CANONICAL
    result = boundary(kind, tmp_path, monkeypatch, served)
    assert result['verdict'] == 'PASS', result
    assert seen == ['official-catalogue']


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('include_canonical_self_row', [False, True])
def test_exact_mapping_allows_other_aliases_and_consistent_canonical_self_row(
    monkeypatch, reverse, include_canonical_self_row,
):
    rows = list(OFFICIAL_ROWS)
    if include_canonical_self_row:
        rows.append({'id': CANONICAL, 'canonical_slug': CANONICAL})
    catalogue(monkeypatch, rows)
    pair = (CANONICAL, ALIAS) if reverse else (ALIAS, CANONICAL)
    assert ri.ModelIdentityVerifier().matches(*pair) is True


@pytest.mark.parametrize('bad_row', [
    {'id': ALIAS, 'canonical_slug': ALIAS + '-20260922'},
    {'id': CANONICAL, 'canonical_slug': ALIAS + '-other'},
    {'id': CANONICAL, 'canonical_slug': ALIAS},
    {'id': [ALIAS], 'canonical_slug': CANONICAL},
])
@pytest.mark.parametrize('kind', ['brief', 'diagram'])
def test_batch_aliases_cannot_hide_conflicting_or_malformed_identity(
    tmp_path, monkeypatch, kind, bad_row,
):
    catalogue(monkeypatch, OFFICIAL_ROWS + [bad_row])
    assert boundary(kind, tmp_path, monkeypatch)['verdict'] == 'FAIL'
