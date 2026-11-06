"""Offline purpose and evidence-fidelity regressions. No model/network calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from reader_test_support import editor_pass, offline_plain_client

SCRIPTS = Path(__file__).parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FACTUAL = '''# Reactor output

**Bottom line.** The pilot generated 12 MW during May 2026. It was a trial, not commercial production.

## What was asked
What did the trial establish?

## What we found
The trial generated 12 MW during May 2026. Cooling limited the output.

## What is still missing
Commercial output is not confirmed.
'''


def test_full_reference_argument_is_not_silently_truncated(tmp_path):
    mod = load('plain_brief_rewrite')
    pack = tmp_path / 'pack.md'
    pack.write_text('x' * 200001 + '\nMaterial limitation at the end.')
    assert 'Material limitation at the end.' in mod.read_sources({'sources': [str(pack)]})


@pytest.mark.parametrize('raw', [
    'The editor response is not valid JSON.',
    '{"verdict":"PASS","blocking":[],"minor":[]}',
    json.dumps({'verdict': 'PASS', 'blocking': [], 'minor': [],
                'checks': {key: {'verdict': [], 'basis': 42}
                           for key in ('factual', 'reasoning', 'editorial', 'presentation')},
                'important_quantity_qualification_review': {'verdict': 'PASS', 'basis': 42}}),
])
def test_unparseable_editor_response_is_saved_without_another_model_call(tmp_path, raw):
    mod = load('plain_brief_rewrite')
    calls = []
    def respond(role, system, user):
        calls.append(role)
        return raw if role == mod.EDITOR else FACTUAL
    with offline_plain_client(mod, respond):
        _, final = mod.rewrite('factual', {'sources': [], 'title': 'Trial', 'slug': 'trial', 'report_purpose': 'factual'}, tmp_path)
    assert calls == ['writer', 'brief_reviewer']
    assert final['editor'] == 'FAIL'
    assert (tmp_path / 'trial.editor-r0.raw.txt').read_text() == raw
    assert (tmp_path / 'trial.md').read_text() == FACTUAL


def test_factual_brief_passes_without_commercial_or_owner_decision_sections():
    mod = load('plain_brief_rewrite')
    result = mod.lint(FACTUAL, prose=True, purpose='factual')
    assert result['ok'], result


@pytest.mark.parametrize('bad', ['business_assessment', 'FACTUAL', '', None])
def test_explicit_invalid_purpose_is_not_normalised(bad):
    with pytest.raises(ValueError, match='report_purpose'):
        load('report_purpose').validate_report_purpose(bad)


def test_unknown_new_campaign_cannot_delete_purpose(tmp_path):
    manifest = {'target': 'ordinary company', 'output_layout': 'brief-and-reference-v2'}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='report_purpose'):
        load('report_purpose').resolve_report_purpose(manifest, tmp_path)


def test_explicit_purpose_independent_of_topic():
    mod = load('report_purpose')
    assert mod.resolve_report_purpose({'target_type': 'company', 'report_purpose': 'factual'}) == 'factual'
    assert mod.resolve_report_purpose({'target_type': 'idea', 'report_purpose': 'decision_assessment'}) == 'decision_assessment'


def test_frozen_legacy_inventory_requires_matching_bytes_and_identity(tmp_path):
    mod = load('report_purpose')
    manifest = {'target': 'Old campaign', 'generated_at': '2026-10-20'}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    entry = {'root': '/admitted/old', 'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'identity': manifest}
    with patch.object(mod, 'LEGACY_CAMPAIGNS', [entry]):
        assert mod.resolve_report_purpose(manifest, tmp_path) == 'legacy'
        changed = dict(manifest, target='New campaign')
        with pytest.raises(ValueError):
            mod.resolve_report_purpose(changed, tmp_path)
        path.write_text(json.dumps(dict(manifest, unexpected='changed')))
        with pytest.raises(ValueError):
            mod.resolve_report_purpose(manifest, tmp_path)


def test_legacy_export_bookkeeping_keeps_history_but_semantic_edits_do_not(tmp_path):
    mod = load('report_purpose')
    manifest = {'target': 'Old campaign', 'generated_at': '2026-10-20',
                'deliverable_exports': {'enabled': True, 'status': 'staged'}}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    entry = {'root': '/admitted/old', 'manifest_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
             'semantic_sha256': mod.semantic_manifest_sha256(manifest),
             'identity': {'target': manifest['target'], 'generated_at': manifest['generated_at']}}
    with patch.object(mod, 'LEGACY_CAMPAIGNS', [entry]):
        changed = json.loads(json.dumps(manifest))
        changed['deliverable_exports'].update(status='copied_to_export', exported_at='2026-11-01',
                                       exports=[], prior_exports=[], overwrite_approved=True)
        path.write_text(json.dumps(changed, indent=2))
        assert mod.resolve_report_purpose(changed, tmp_path) == 'legacy'
        changed['deliverable_exports']['intended_directory'] = 'elsewhere'
        path.write_text(json.dumps(changed))
        with pytest.raises(ValueError, match='report_purpose'):
            mod.resolve_report_purpose(changed, tmp_path)


def test_legacy_generation_preserves_frozen_purposeless_manifest(tmp_path, monkeypatch):
    from test_deep_research_campaign_tools import write_depth_pass_fixture
    from test_plain_brief_campaign_gate import GOOD_BODY
    root = tmp_path / 'campaign'
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    from publication_test_support import freeze_legacy_fixture
    freeze_legacy_fixture(monkeypatch, root)
    path = root / 'manifest.json'
    before = path.read_bytes()
    import report_purpose as purpose
    mod = load('plain_brief_rewrite')
    def respond(role, system, user):
        return json.dumps(editor_pass()) if role == mod.EDITOR else GOOD_BODY
    with patch.object(mod, '_purpose_module', lambda: purpose), offline_plain_client(mod, respond):
        result = mod.campaign_brief(root)
    assert result['status'] == 'PASS'
    assert path.read_bytes() == before
    assert purpose.resolve_report_purpose(json.loads(path.read_text()), root) == 'legacy'


def test_shared_style_drives_writer_editor_and_retry_rules():
    mod = load('plain_brief_rewrite')
    reference = (SCRIPTS.parent / 'references/reader-pair-style.md').read_text()
    for phrase in ['Australian English', 'complete sentences', 'why and how', 'subject, unit and period',
                   'factual', 'reasoning', 'editorial', 'presentation']:
        assert phrase in reference
        assert phrase in mod.STYLE + mod.PROSE_RULES + mod.EDITOR_SYS
    assert 'Options compared' not in mod.style_for_purpose('factual')
    assert 'Technical judgement' in mod.style_for_purpose('decision_assessment')


@pytest.mark.parametrize('pack', [
    'The pilot generated 12 MW during May 2026.',  # lost caveat
    'The pilot generated 12 GW during May 2026. It was a trial, not commercial production.',
    'There were 12 employees, 10 sites and 20 customers in 2026.',
])
def test_quantity_count_cannot_pass_lost_caveat_wrong_unit_or_padding(pack):
    mod = load('verify_campaign_depth')
    findings = [{'claim': 'The pilot generated 12 MW during May 2026. It was a trial, not commercial production.',
                 'important': True}]
    metrics, holds = mod.quantitative_retention_holds(findings, pack)
    assert holds, metrics
    assert metrics['semantic_retention_verified'] is False


def test_retention_is_not_claimed_as_semantic_proof_even_with_matching_numbers():
    mod = load('verify_campaign_depth')
    findings = [{'claim': 'The pilot generated 12 MW during May 2026. It was a trial, not commercial production.',
                 'important': True}]
    metrics, holds = mod.quantitative_retention_holds(findings, FACTUAL)
    assert not holds
    assert metrics['semantic_retention_verified'] is False
    assert metrics['quantity_check_kind'] == 'screening_only'


def test_pack_review_required_before_any_brief_model_call(tmp_path):
    mod = load('plain_brief_rewrite')
    (tmp_path / 'internal').mkdir()
    (tmp_path / 'internal/reference-pack.md').write_text('# Pack\nEvidence.')
    manifest = {'report_purpose': 'factual', 'output_layout': 'brief-and-reference-v2',
                'internal_outputs': [{'path': 'internal/reference-pack.md'}]}
    (tmp_path / 'manifest.json').write_text(json.dumps(manifest))
    with patch.object(mod, 'call', side_effect=AssertionError('no model call before pack QA')):
        with pytest.raises(SystemExit, match='reference pack.*review'):
            mod.campaign_brief(tmp_path)


def test_purpose_cannot_bypass_depth_gate_when_brief_is_undeclared(tmp_path):
    mod = load('verify_campaign_depth')
    result = mod.plain_brief_gate(tmp_path, {'target': 'Unknown new campaign'})
    assert result['failures']
    assert 'report_purpose' in '; '.join(result['failures'])


def test_optional_brief_does_not_skip_reference_review(tmp_path):
    mod = load('plain_brief_rewrite')
    manifest = {'report_purpose': 'factual', 'plain_brief': {'required': False, 'not_required_reason': 'Legacy layout'},
                'internal_outputs': [{'path': 'internal/full-research-pack.md'}]}
    result = mod.verify_campaign_plain_brief(tmp_path, manifest)
    assert any('reference_pack_review' in item for item in result['holds'])


@pytest.mark.parametrize('payload', [None, [], 'PASS', {'status': 'PASS', 'checks': []}])
def test_malformed_pack_review_holds_instead_of_crashing(tmp_path, payload):
    mod = load('plain_brief_rewrite')
    (tmp_path / 'internal').mkdir()
    (tmp_path / 'qa').mkdir()
    (tmp_path / 'internal/reference-pack.md').write_text('# Full reference pack')
    (tmp_path / 'qa/evidence-privacy-review.json').write_text(json.dumps(payload))
    manifest = {'report_purpose': 'factual', 'output_layout': 'brief-and-reference-v2'}
    assert mod.reference_pack_review_holds(tmp_path, manifest)


def test_accepted_semantic_review_not_literal_keyword_matching_controls_retention(tmp_path):
    from test_two_outputs import write_v2_pass_fixture
    from test_plain_brief_campaign_gate import verify
    root = tmp_path / 'campaign'
    write_v2_pass_fixture(root, deliverable_export_enabled=False,
                          reference_extra='The pilot produced 12 MW in May 2026. Commercial output remains unconfirmed.')
    (root / 'lanes/lane-0.json').write_text(json.dumps({'findings': [
        {'claim': 'The pilot generated 12 MW during May 2026. It was a trial, not commercial production.', 'important': True}
    ]}))
    rc, result = verify(root)
    assert rc == 0, result
    assert result['metrics']['quantitative_screening_warnings']


def test_guidance_does_not_reintroduce_commercial_sections_or_fixed_review_prices():
    skill = (SCRIPTS.parent / 'SKILL.md').read_text()
    guide = (SCRIPTS.parent / 'references/plain-brief-output.md').read_text()
    assert 'report_purpose' in skill
    assert 'references/reader-pair-style.md' in skill
    assert 'plain brief REQUIRES an `## Options compared`' not in skill
    assert 'Cost is roughly US$0.50' not in guide
    assert 'report_purpose' in guide


def test_existing_pack_qa_packet_contains_the_shared_semantic_result_schema():
    from types import SimpleNamespace
    init = load('init_campaign')
    packet = init.qa_packet('evidence-privacy', 'Review the full pack.', SimpleNamespace(report_purpose='factual'))
    assert 'important_quantity_qualification_review' in packet
    assert 'reference_pack_sha256' in packet
    assert 'factual' in packet and 'presentation' in packet


def test_editor_requires_distinct_checks_and_evidence_qualified_quantities():
    mod = load('plain_brief_rewrite')
    assert all(name in mod.EDITOR_SYS for name in ('factual', 'reasoning', 'editorial', 'presentation',
                                                  'important quantities', 'qualifications', 'saved evidence'))
    assert not mod.editor_review_passes({'verdict': 'PASS', 'blocking': [], 'minor': []}, purpose='factual')
    good = {'verdict': 'PASS', 'blocking': [], 'minor': [],
            'checks': {key: {'verdict': 'PASS', 'basis': 'Checked actual current reader text and saved evidence.'}
                       for key in ('factual', 'reasoning', 'editorial', 'presentation')},
            'important_quantity_qualification_review': {'verdict': 'PASS', 'basis': 'No material quantity or qualification lost.'}}
    assert mod.editor_review_passes(good, purpose='factual')
    for key in good['checks']:
        bad = json.loads(json.dumps(good))
        bad['checks'][key]['verdict'] = 'FAIL'
        assert not mod.editor_review_passes(bad, purpose='factual')
