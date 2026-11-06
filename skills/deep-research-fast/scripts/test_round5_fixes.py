"""round 4 review: grade display setting, code masking and resume (ported reviewer probes)."""
import json
from pathlib import Path

import pytest

import fact_grades as fg
from deepest_research import DeepestResearch
from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config
from test_deepest_research import QUESTIONS, SEARCH as DEEP_SEARCH
from test_fast_sentence_check import SEARCH, prepared, sentence_reply

GRADE_TOKENS = ('Source grade:', '(grade ', 'Fact grades (NATO Admiralty Code):',
                'usually reliable source', 'fairly reliable source', 'source reliability unknown',
                'confirmed by another independent source', 'supported only in part')


class KindFake(FakeHermes):
    def __init__(self, search, kind):
        super().__init__(search)
        self.kind = kind

    def oneshot(self, model, prompt, **kwargs):
        result = super().oneshot(model, prompt, **kwargs)
        if prompt.startswith('You check findings'):
            data = json.loads(result.stdout)
            for row in data['checks']:
                row['source_kind'] = self.kind
            result.stdout = json.dumps(data)
        return result


def config(visibility):
    cfg = small_config()
    if visibility == 'absent':
        cfg.pop('grades')
    else:
        cfg['grades'] = {'show_in_answer': visibility}
    return cfg


def no_grades(answer):
    assert all(token not in answer for token in GRADE_TOKENS), answer


@pytest.mark.parametrize('kind', ['self', 'discovery', 'unknown'])
@pytest.mark.parametrize('visibility', [False, 'absent', True])
def test_visibility_preserves_all_grade_artifacts(tmp_path, kind, visibility):
    fake = KindFake(SEARCH, kind)
    initial, _ = prepared(tmp_path, fake)
    fr = FastResearch(tmp_path, initial.question, run_id=initial.run_id, skip_preflight=True,
                      config=config(visibility), **fake.hooks())
    fr.oneshot = sentence_reply(fake, {'Broad fact.': 'partly', 'Wrong fact.': 'unsupported',
                                      'Unknown fact.': 'garbage'})
    assert fr.check() == {'checked': 3, 'narrowed': 1, 'removed': 1, 'unchecked': 1,
                          'inference_checked': 0, 'inference_narrowed': 0,
                          'inference_removed': 0, 'inference_unchecked': 0}
    fr.finish()
    answer = fr.path('answer.md').read_text()
    data = {name: fr._read_json(name) for name in
            ('grades.json', 'citations.json', 'sentence_check.json', 'not_verified.json')}
    grades = data['grades.json']
    letter = {'self': 'B', 'discovery': 'D', 'unknown': 'F'}[kind]
    assert grades['counts'] == {letter + '2': 2, letter + '3': 1}
    assert grades['citation_recall'] == 0.25
    assert grades['citation_precision'] is None and grades['precision_note'] == 'not measured'
    assert all(c['grade']['code'] == letter + '2' for c in data['citations.json'])
    assert 'Narrow fact.[1]' in answer
    assert 'Unknown fact.[1] (Not checked)' in answer
    assert 'Set OPENROUTER_API_KEY. (Not verified)' in answer
    assert '- Wrong fact.' in fr.path('evidence.md').read_text()
    assert 'Wrong fact.' not in answer
    assert 'Citation recall: 0.25.' in answer
    if visibility is True:
        assert all(token in answer for token in ('(grade ', 'Fact grades (NATO Admiralty Code):'))
        assert 'Source grade:' in fr.path('evidence.md').read_text()
    else:
        no_grades(answer)
    rec = fr.record()
    assert rec['grade_counts'] == grades['counts']
    assert rec['citation_recall'] == grades['citation_recall']
    assert rec['sentence_check_counts'] == grades['sentence_check_counts']
    assert fr._read_json('quote_check.json')['verdict'] == 'pass'
    fr.finish()
    assert fr.path('answer.md').read_text() == answer

    # Exercise the other display setting on the same immutable inputs and cached verdicts.
    calls = len(fake.sessions)
    fr.show_grades = visibility is not True
    fr.check()
    fr.finish()
    assert len(fake.sessions) == calls
    strip = lambda d: {k: ({kk: vv for kk, vv in v.items() if kk != 'shown_in_answer'} if isinstance(v, dict) else v) for k, v in d.items()}
    assert strip(data) == strip({name: fr._read_json(name) for name in data})
    other = fr.path('answer.md').read_text()
    if fr.show_grades:
        assert all(token in other for token in ('(grade ', 'Fact grades (NATO Admiralty Code):'))
        assert 'Source grade:' in fr.path('evidence.md').read_text()
    else:
        no_grades(other)


@pytest.mark.parametrize('visibility', [False, 'absent'])
@pytest.mark.parametrize('questions', [[], QUESTIONS])
def test_fresh_deepest_hides_low_grades(tmp_path, visibility, questions):
    fake = KindFake(DEEP_SEARCH, 'discovery')
    fake.followups = questions
    fr = DeepestResearch(tmp_path, 'Question?', run_id='deep-hidden', config=config(visibility), **fake.hooks())
    rec = fr.run()
    assert rec['status'] == 'completed', rec['error']
    no_grades(fr.path('answer.md').read_text())
    assert rec['grade_counts'] and set(rec['grade_counts']) == {'D2'}
    assert rec['citation_recall'] == 1.0
    assert rec['quote_check'] == 'pass'
    assert len(fr.store.child_runs(fr.run_id)) == len(questions) + 1
    sessions = len(fake.sessions)
    answer = fr.path('answer.md').read_text()
    assert fr.run()['status'] == 'completed'
    assert fr.path('answer.md').read_text() == answer
    assert len(fake.sessions) == sessions


@pytest.mark.parametrize('code', ['`print("Done. ", argv[2])`', '`a | b[2]. next`', '``x`[2]``'])
@pytest.mark.parametrize('as_table', [False, True])
def test_masking_keeps_code_atomic_in_table_and_prose(tmp_path, code, as_table):
    fr, fake = prepared(tmp_path)
    fr.show_grades = False
    a, b = fr._admitted()[:2]
    left = f'Use {code} here.[C{a["id"]}]'
    right = f'Fact B.[C{b["id"]}]'
    body = f'| {left} | {right} |' if as_table else f'{left} {right}'
    answer, cites, dropped = fr.render(body, {c['id']: c for c in fr._admitted()})
    assert not dropped
    fr.path('answer.md').write_text(answer)
    fr.write_json('citations.json', cites)
    seen = []
    original = sentence_reply(fake, {'Fact B.': 'unsupported'})

    def spy(model, prompt, **kwargs):
        if prompt.startswith('You check sentences'):
            seen.append(prompt)
        return original(model, prompt, **kwargs)

    fr.oneshot = spy
    result = fr.check()
    assert result['checked'] == 2 and result['removed'] == 1
    assert f'Claim: Use {code} here.' in seen[0]
    assert f'Use {code} here.[1]' in fr.path('answer.md').read_text()
    assert len(fr._read_json('citations.json')) == 1
    assert fr._read_json('quote_check.json')['verdict'] == 'pass'



@pytest.mark.parametrize('controller', [FastResearch, DeepestResearch])
@pytest.mark.parametrize('stop_after', ['check', 'finish'])
def test_resume_honours_new_hidden_setting(tmp_path, controller, stop_after):
    fake = KindFake(SEARCH, 'discovery')
    first = controller(tmp_path, 'Question?', run_id='visibility-resume', config=config(True), **fake.hooks())
    assert first.run(stop_after=stop_after)['status'] == 'completed'
    prior = first.path('answer.md').read_text()
    assert '(grade D2:' in prior
    assert 'Source grade:' in first.path('evidence.md').read_text()
    # Model an interrupted legacy run that had completed these persisted phases.
    first.store.set_run(first.run_id, status='failed')
    resumed = controller(tmp_path, first.question, run_id=first.run_id, config=config(False), **fake.hooks())
    assert resumed.show_grades is False
    rec = resumed.run()
    assert rec['status'] == 'completed', rec['error']
    assert rec['quote_check'] == 'pass'
    no_grades(resumed.path('answer.md').read_text())
