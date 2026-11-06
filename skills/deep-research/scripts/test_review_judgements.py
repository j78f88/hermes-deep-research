"""F2/F3 anti-bypass and installed-workflow proof with real offline client transport."""
import copy
import json
from pathlib import Path

import pytest
import plain_brief_rewrite as plain
import research_qa_repair as qa
import receipt_integrity as ri
from reader_test_support import editor_pass, offline_plain_transport
from test_acceptance_repairs import accepted, put, sha
from test_core_integration import campaign, body


@pytest.fixture(autouse=True)
def deny_network(monkeypatch, tmp_path):
    import socket
    import urllib.request
    def deny(*args, **kwargs):
        raise AssertionError('network forbidden: synthetic judgement tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(urllib.request, 'urlopen', deny)
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', deny)
    monkeypatch.setattr(plain.model_client, '_api_key', lambda: 'synthetic-only')
    monkeypatch.setattr(plain, 'EXPORT_ROOT', tmp_path / 'sandbox-export')
    monkeypatch.setattr(plain, 'AUTO', tmp_path / 'sandbox-export')


def reply(user, notes=(), classification='optional_style', *, verdict='supported', applied=True):
    result = editor_pass()
    result['minor'] = list(notes)
    if 'REVIEW_BINDING: ' not in user:
        return json.dumps(result)  # RED: old workflow never requests typed judgements.
    binding = json.loads(user.split('REVIEW_BINDING: ', 1)[1].splitlines()[0])
    retained = json.loads(user.split('RETAINED_FINDINGS: ', 1)[1].splitlines()[0])
    result['review_binding'] = binding
    result['note_dispositions'] = [
        {'note': note, 'classification': classification, 'classification_verdict': verdict,
         'evidence_verdict': 'supported',
         'factual_impact': {'optional_style': 'none', 'already_resolved': 'resolved',
                            'mistaken_reviewer': 'reviewer_error', 'apply_correction': 'resolved'}[classification],
         'correction_applied': applied, 'rationale': 'Compared the exact current readers with the saved source qualification.',
         'evidence_bindings': binding['evidence_bindings']}
        for note in dict.fromkeys([*retained, *notes])]
    return json.dumps(result)


def generate(root, notes=('Use a shorter heading.',), classification='optional_style', **kwargs):
    if not (root / 'manifest.json').exists():
        campaign(root)
    calls = []
    def respond(role, system, user):
        calls.append(role)
        return body() if role == 'writer' else reply(user, notes, classification, **kwargs)
    with offline_plain_transport(plain, responder=respond):
        record = plain.campaign_brief(root)
    return record, calls


def read(root, rel):
    return json.loads((root / rel).read_text())


def rebind_decisions(root, record):
    """Honest packet rebuild prevents hash drift hiding the actual independence test."""
    put(root, plain.DEFAULT_REVIEW_REL, record)
    decisions = read(root, plain.DEFAULT_EDITOR_DECISIONS_REL)
    decisions['review_inputs'] = qa.build_review_packet(
        [row['path'] for row in decisions['review_inputs']['files']], root=root)
    put(root, plain.DEFAULT_EDITOR_DECISIONS_REL, decisions)


def test_f2_uncatalogued_self_asserted_classification_is_not_acceptance(tmp_path):
    accepted(tmp_path, 'Restore the forecast qualifier.')
    check = read(tmp_path, 'qa/disposition.json')
    check['reviewer'] = 'nobody/not-a-catalogued-or-served-model'
    put(tmp_path, 'qa/disposition.json', check)
    decisions = read(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL)
    decisions['decisions'][0]['evidence_bindings'][1]['sha256'] = sha(tmp_path / 'qa/disposition.json')
    put(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL, decisions)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


@pytest.mark.parametrize('classification', sorted(qa.DECISION_CLASSES))
def test_existing_editor_automatically_judges_notes_without_a_third_stage(tmp_path, classification):
    record, calls = generate(tmp_path, classification=classification)
    assert record['acceptance_status'] == 'PASS', record['readiness']
    assert calls == ['writer', 'brief_reviewer']
    decisions = read(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL)
    assert decisions['status'] == 'reviewed'
    assert decisions['decisions'][0]['classification'] == classification
    check = read(tmp_path, decisions['decisions'][0]['classification_review'])
    assert check['reviewer'] == record['editor_model']  # comments editor need not be a writer
    assert check['generation_id'] == record['provenance']['final_editor_generation_id']
    seen = []
    table = {c['generation_id']: {'model': c['served_model'], 'tokens_prompt': 100000}
             for c in record['provenance']['calls']}
    result = ri.verify_provenance(record, tmp_path, fetch=lambda gid: seen.append(gid) or table.get(gid))
    assert result['verdict'] == 'PASS', result
    assert check['generation_id'] in seen


def test_retained_factual_notes_are_sent_to_the_same_final_comments_editor(tmp_path):
    first, _ = generate(tmp_path, notes=('Restore the forecast qualifier.',), classification='apply_correction')
    old = (tmp_path / plain.DEFAULT_REVIEW_REL).read_bytes()
    second, calls = generate(tmp_path, notes=(), classification='already_resolved')
    assert second['acceptance_status'] == 'PASS', second['readiness']
    assert calls == ['writer', 'brief_reviewer']
    decisions = read(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL)
    assert [d['note'] for d in decisions['decisions']] == ['Restore the forecast qualifier.']
    assert next((tmp_path / 'receipts/qa-history').glob('*/plain-brief-review.json')).read_bytes() == old


@pytest.mark.parametrize('field,value', [('classification_verdict', 'partial'), ('classification_verdict', None),
    ('evidence_verdict', 'partial'), ('correction_applied', False)])
def test_unresolved_or_unsupported_typed_judgements_hold(tmp_path, field, value):
    campaign(tmp_path)
    def respond(role, system, user):
        if role == 'writer':
            return body()
        result = json.loads(reply(user, ['Restore the forecast qualifier.'], 'apply_correction'))
        if result.get('note_dispositions'):
            result['note_dispositions'][0][field] = value
        return json.dumps(result)
    with offline_plain_transport(plain, responder=respond):
        record = plain.campaign_brief(tmp_path)
    assert record['acceptance_status'] == 'HOLD'


@pytest.mark.parametrize('mutation', ['raw', 'label', 'reviewer', 'generation', 'evidence', 'raw_choice', 'raw_verdict'])
def test_judgement_must_match_the_exact_served_response(tmp_path, mutation):
    record, _ = generate(tmp_path)
    assert record['acceptance_status'] == 'PASS'
    decisions = read(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL)
    d = decisions['decisions'][0]
    check = read(tmp_path, d['classification_review'])
    if mutation in ('raw', 'raw_choice', 'raw_verdict'):
        raw = read(tmp_path, check['raw_response_path'])
        choice = raw['raw_responses'][-1]['choices'][0]
        if mutation == 'raw_choice':
            raw['raw_responses'][-1]['choices'][0] = None
        elif mutation == 'raw_verdict':
            parsed = json.loads(choice['message']['content'])
            parsed['verdict'] = 'FAIL'
            choice['message']['content'] = json.dumps(parsed)
        else:
            choice['message']['content'] = json.dumps(editor_pass())
        put(tmp_path, check['raw_response_path'], raw)
    elif mutation == 'label':
        check['classification'] = d['classification'] = 'mistaken_reviewer'
        check['factual_impact'] = 'reviewer_error'
    elif mutation == 'reviewer':
        check['reviewer'] = 'nobody/fiction'
    elif mutation == 'generation':
        check['generation_id'] = record['artifact_writers'][0]['generation_id']
    else:
        check['evidence_bindings'][0]['sha256'] = '0' * 64
    put(tmp_path, d['classification_review'], check)
    d['evidence_bindings'] = [{'path': p, 'sha256': sha(tmp_path / p)} for p in d['evidence_refs']]
    decisions['review_inputs'] = qa.build_review_packet(
        [row['path'] for row in decisions['review_inputs']['files']], root=tmp_path)
    put(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL, decisions)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


@pytest.mark.parametrize('role', ['writer', 'rewriting_editor'])
def test_f3_actual_earlier_author_cannot_be_the_classification_reviewer(tmp_path, role):
    record, _ = generate(tmp_path)
    assert record['acceptance_status'] == 'PASS'
    author = copy.deepcopy(record['artifact_writers'][0])
    author.update(role=role, served_model=record['editor_model'], generation_id='gen-SYNTHETIC-earlier')
    record['artifact_writers'].insert(0, copy.deepcopy(author))
    record['provenance']['attempts'].insert(0, copy.deepcopy(author))
    record['provenance']['calls'].insert(0, author)
    rebind_decisions(tmp_path, record)
    result = qa.campaign_readiness(tmp_path)
    assert not result['publication_qa_allowed'], result


@pytest.mark.parametrize('field', ['review_inputs', 'status'])
def test_pending_packet_cannot_omit_required_acceptance_fields(tmp_path, field):
    accepted(tmp_path)
    pending = read(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL)
    pending.pop(field, None)
    put(tmp_path, plain.DEFAULT_EDITOR_DECISIONS_REL, pending)
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


@pytest.mark.parametrize('rel', ['internal/plain-brief.md', 'internal/reference-pack.md', 'qa/evidence-privacy-review.json'])
def test_post_review_bytes_and_evidence_races_hold(tmp_path, rel):
    record, _ = generate(tmp_path)
    assert record['acceptance_status'] == 'PASS'
    (tmp_path / rel).write_text((tmp_path / rel).read_text() + '\nchanged')
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']


def test_manifest_custom_review_path_is_used_by_default_completion_gate(tmp_path):
    campaign(tmp_path)
    manifest = read(tmp_path, 'manifest.json')
    manifest['plain_brief']['review'] = 'qa/custom-review.json'
    put(tmp_path, 'manifest.json', manifest)
    record, _ = generate(tmp_path)
    assert record['acceptance_status'] == 'PASS'
    assert qa.campaign_readiness(tmp_path)['publication_qa_allowed'] is True
    import complete_campaign
    assert complete_campaign.check_readiness(tmp_path)['verdict'] == 'PASS'


@pytest.mark.parametrize('field,value', [('note', []), ('evidence_bindings', [{}]),
    ('evidence_bindings', ['source.md']), ('evidence_bindings', [{'path': '../outside', 'sha256': '0'*64}]),
    ('classification', []), ('rationale', False), ('correction_applied', 1)])
def test_malformed_judgement_stops_without_paid_schema_repair(tmp_path, field, value):
    campaign(tmp_path)
    roles = []
    def respond(role, system, user):
        roles.append(role)
        if role == 'writer':
            return body()
        result = json.loads(reply(user, ['Restore the forecast qualifier.']))
        result['note_dispositions'][0][field] = value
        return json.dumps(result)
    with offline_plain_transport(plain, responder=respond):
        record = plain.campaign_brief(tmp_path)
    assert roles == ['writer', 'brief_reviewer']
    assert record['acceptance_status'] == 'HOLD'
    assert record['schema_valid'] is False
    assert (tmp_path / record['provenance']['raw_response_paths'][-1]).is_file()


def test_editor_reads_exact_admitted_source_evidence_not_only_receipt(tmp_path):
    campaign(tmp_path)
    put(tmp_path, 'sources/forecast.md', 'Synthetic source: 2025 capacity is a forecast, not observed capacity.')
    receipt = read(tmp_path, 'qa/evidence-privacy-review.json')
    receipt['evidence_inputs'] = ['sources/forecast.md']
    put(tmp_path, 'qa/evidence-privacy-review.json', receipt)
    seen = []
    def respond(role, system, user):
        if role == 'writer':
            return body()
        seen.append(user)
        return reply(user, ['Restore the forecast qualifier.'], 'already_resolved')
    with offline_plain_transport(plain, responder=respond):
        record = plain.campaign_brief(tmp_path)
    assert record['acceptance_status'] == 'PASS', record['readiness']
    assert '2025 capacity is a forecast, not observed capacity.' in seen[-1]
    check = read(tmp_path, record['classification_reviews'][0])
    assert {'path': 'sources/forecast.md', 'sha256': sha(tmp_path / 'sources/forecast.md')} in check['evidence_bindings']
    (tmp_path / 'sources/forecast.md').write_text('changed after review')
    assert not qa.campaign_readiness(tmp_path)['publication_qa_allowed']
