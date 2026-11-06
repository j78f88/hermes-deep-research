"""Independent round 3 probes. Assertions describe required correct behaviour."""
import json

import pytest

import fact_grades as fg
from deepest_research import DeepestResearch
from fast_research import FastResearch
from fast_test_support import small_config
from test_fast_sentence_check import prepared, sentence_reply
from test_page_kinds import _seed


def _answer(fr, body):
    claims = fr._admitted()
    text, cites, dropped = fr.render(body, {c['id']: c for c in claims})
    assert not dropped
    fr.path('answer.md').write_text(text)
    fr.write_json('citations.json', cites)


def test_removed_code_keeps_its_index(tmp_path):
    fr, fake = prepared(tmp_path)
    a, b = fr._admitted()[:2]
    _answer(fr, f"Use `argv[2]` here.[C{a['id']}] Fact B.[C{b['id']}]")
    fr.oneshot = sentence_reply(fake, {'Use `argv[2]` here.': 'unsupported'})
    assert fr.check()['removed'] == 1
    removed = fr.path('evidence.md').read_text().split('## Removed because the sources did not support them', 1)[1]
    assert '- Use `argv[2]` here.' in removed, removed
    assert fr._read_json('quote_check.json')['verdict'] == 'pass'


@pytest.mark.parametrize('code', ['`argv[2]. next`', '`print("Done. ", argv[2])`', '``x`[2]``'])
def test_code_is_atomic_to_sentence_check(tmp_path, code):
    fr, fake = prepared(tmp_path)
    a, b = fr._admitted()[:2]
    _answer(fr, f"Use {code} here.[C{a['id']}] Fact B.[C{b['id']}]")
    seen = []
    reply = sentence_reply(fake, {})
    def spy(model, prompt, **kwargs):
        if prompt.startswith('You check sentences'):
            seen.append(prompt)
        return reply(model, prompt, **kwargs)
    fr.oneshot = spy
    result = fr.check()
    assert result['checked'] == 2, result
    assert f'Claim: Use {code} here.' in seen[0], seen[0]
    assert f'Use {code} here.[1] Fact B.[2]' in fr.path('answer.md').read_text()
    assert fr._read_json('quote_check.json')['verdict'] == 'pass'


@pytest.mark.parametrize('code', ['`argv[2]. next`', '`print("Done. ", argv[2])`'])
def test_split_inline_code_with_removed_source_does_not_crash(tmp_path, code):
    fr, fake = prepared(tmp_path)
    a, b = fr._admitted()[:2]
    _answer(fr, f"Use {code} here.[C{a['id']}] Fact B.[C{b['id']}]")
    fr.oneshot = sentence_reply(fake, {'Fact B.': 'unsupported'})
    fr.check()
    assert f'Use {code} here.[1]' in fr.path('answer.md').read_text()
    assert len(fr._read_json('citations.json')) == 1
    assert fr._read_json('quote_check.json')['verdict'] == 'pass'


def test_standard_fence_preserves_indices_and_has_no_votes(tmp_path):
    fr, fake = prepared(tmp_path)
    a = fr._admitted()[0]
    _answer(fr, f"```python\nx = argv[1]\ny = argv[17]\n```\n\nFact A.[C{a['id']}]")
    fr.oneshot = sentence_reply(fake, {})
    result = fr.check()
    assert result['checked'] == 1
    assert 'x = argv[1]\ny = argv[17]' in fr.path('answer.md').read_text()
    assert fr._read_json('quote_check.json')['verdict'] == 'pass'


def test_unknown_votes_are_ignored_within_same_run(tmp_path):
    fr = FastResearch(tmp_path, 'q', run_id='same', skip_preflight=True)
    url = 'https://same.invalid/page'
    _seed(fr, url, ['self', 'unknown', 'garbage', None, 'unknown'])
    assert fr._page_kinds([url]) == {url: 'self'}


def test_other_run_real_votes_are_excluded(tmp_path):
    fr = FastResearch(tmp_path, 'q', run_id='own', skip_preflight=True)
    other = FastResearch(tmp_path, 'other q', run_id='other', skip_preflight=True)
    url = 'https://same.invalid/page'
    _seed(fr, url, ['self'])
    _seed(other, url, ['discovery'] * 5)
    assert fr._page_kinds([url]) == {url: 'self'}


@pytest.mark.parametrize('kinds,expected', [
    (['self', 'independent'], 'independent'),
    (['primary', 'discovery'], 'discovery'),
    (['independent', 'discovery'], 'discovery'),
    (['discovery', 'self', 'self'], 'self'),
    (['unknown', 'garbage', None], 'unknown'),
])
def test_page_majority_and_tie_order(tmp_path, kinds, expected):
    fr = FastResearch(tmp_path, 'q', run_id='own', skip_preflight=True)
    url = 'https://same.invalid/page'
    _seed(fr, url, kinds)
    assert fr._page_kinds([url]) == {url: expected}


def test_deepest_votes_from_own_children_only(tmp_path):
    deep = DeepestResearch(tmp_path, 'q', run_id='deepest-own', skip_preflight=True, config=small_config())
    other = DeepestResearch(tmp_path, 'other', run_id='deepest-other', skip_preflight=True, config=small_config())
    own_base = deep.child('q', base=True)
    own_branch = deep.child('branch')
    other_base = other.child('other', base=True)
    url = 'https://same.invalid/page'
    _seed(own_base, url, ['self', 'self'])
    _seed(own_branch, url, ['discovery', 'unknown', 'garbage'])
    _seed(other_base, url, ['discovery'] * 5)
    _seed(deep, url, ['discovery'] * 5)  # parent findings are not source runs
    assert deep._page_kinds([url]) == {url: 'self'}
    assert own_base._page_kinds([url]) == {url: 'self'}
    assert own_branch._page_kinds([url]) == {url: 'discovery'}


def test_deepest_with_no_source_runs(tmp_path):
    deep = DeepestResearch(tmp_path, 'q', run_id='deepest-empty', skip_preflight=True, config=small_config())
    assert deep._page_kinds(['https://same.invalid/page']) == {}


@pytest.mark.parametrize('narrowed', [
    'The page describes a tree of sub-questions',
    'The page describes a tree of sub-questions.',
    'The page says "tree of sub-questions."',
    'The page says (tree of sub-questions)',
])
def test_narrowing_does_not_swallow_next_setting(tmp_path, narrowed):
    fr, fake = prepared(tmp_path)
    a = fr._admitted()[0]
    _answer(fr, f"Broad fact.[C{a['id']}] Set OPENROUTER_API_KEY.")
    original = fake.oneshot
    def reply(model, prompt, **kwargs):
        result = original(model, prompt, **kwargs)
        if prompt.startswith('You check sentences'):
            result.stdout = json.dumps({'checks': [{'n': 1, 'verdict': 'partly', 'supported_claim': narrowed}]})
        return result
    fr.oneshot = reply
    fr.check()
    assert 'Set OPENROUTER_API_KEY. (Not verified)' in fr.path('answer.md').read_text()
    assert fr._read_json('not_verified.json') == ['Set OPENROUTER_API_KEY.']


def test_uncited_code_index_is_not_a_citation():
    text, marked = fg.mark_not_verified('Use `argv[1]` here.')
    assert text == 'Use `argv[1]` here. (Not verified)'
    assert marked == ['Use `argv[1]` here.']
