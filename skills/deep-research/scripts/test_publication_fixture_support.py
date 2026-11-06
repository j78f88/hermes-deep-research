"""Non-vacuity checks for synthetic publication fixture migration helpers."""
import copy
import importlib.machinery
import json
from pathlib import Path

import pytest

import receipt_integrity as ri
import report_purpose
from publication_test_support import (
    admit_frozen_legacy, freeze_legacy_fixture, run_frozen_legacy_export, run_offline_cli, run_synthetic_export,
    synthetic_generation_table,
)
from test_two_outputs import _export_v2, REVIEW_REL
import test_deep_research_campaign_tools as base


def test_generated_modern_fixture_checks_actual_saved_calls(tmp_path):
    root, _ = _export_v2(tmp_path, include_preseed=False)
    record = json.loads((root / REVIEW_REL).read_text())
    table = synthetic_generation_table(root)
    seen = []

    def fetch(gid):
        seen.append(gid)
        return table.get(gid)

    assert record['provenance']['schema_version'] == ri.SERVED_CALLS_SCHEMA
    assert record['artifact_writers']
    assert all((root / rel).is_file() for rel in record['provenance']['raw_response_paths'])
    assert ri.verify_provenance(record, root, fetch=fetch)['verdict'] == 'PASS'
    assert seen == [call['generation_id'] for call in record['provenance']['calls']]
    saved = copy.deepcopy(table)
    record['provenance']['calls'][0]['served_model'] = 'unknown/forged'
    assert synthetic_generation_table(root) == saved
    assert ri.verify_provenance(record, root, fetch=fetch)['verdict'] == 'FAIL'


def test_export_transport_rejects_unknown_generation_without_copy(tmp_path):
    root, export = _export_v2(tmp_path, include_preseed=False)
    record = json.loads((root / REVIEW_REL).read_text())
    gid = 'gen-SYNTHETIC-absent-from-raw'
    for rows in (record['artifact_writers'], record['provenance']['calls'], record['provenance']['attempts']):
        rows[0]['generation_id'] = gid
    (root / REVIEW_REL).write_text(json.dumps(record))
    proc = run_synthetic_export(root, export)
    assert proc.returncode == 3
    assert 'unknown synthetic generation' in proc.stdout
    assert not export.exists()


def test_explicit_legacy_inventory_does_not_admit_other_or_changed_units(tmp_path, monkeypatch):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    original_inventory = copy.deepcopy(report_purpose.LEGACY_CAMPAIGNS)
    original_loader = importlib.machinery.SourceFileLoader.exec_module
    with monkeypatch.context() as local:
        manifest = admit_frozen_legacy(local, first)
        import test_plain_brief_campaign_gate as gate
        file_loaded_resolver = gate.load_plain()._purpose_module()
        assert file_loaded_resolver.LEGACY_CAMPAIGNS == report_purpose.LEGACY_CAMPAIGNS
        assert file_loaded_resolver.resolve_report_purpose(manifest, first) == 'legacy'
        (second / 'manifest.json').write_text(json.dumps(dict(manifest, target='Another unit')))
        with pytest.raises(ValueError, match='frozen historical inventory'):
            report_purpose.resolve_report_purpose(json.loads((second / 'manifest.json').read_text()), second)
        with pytest.raises(ValueError, match='frozen historical inventory'):
            file_loaded_resolver.resolve_report_purpose(json.loads((second / 'manifest.json').read_text()), second)
        manifest['unknown_semantic_change'] = True
        (first / 'manifest.json').write_text(json.dumps(manifest))
        with pytest.raises(ValueError, match='frozen historical inventory'):
            report_purpose.resolve_report_purpose(manifest, first)
        with pytest.raises(ValueError, match='frozen historical inventory'):
            file_loaded_resolver.resolve_report_purpose(manifest, first)
    assert report_purpose.LEGACY_CAMPAIGNS == original_inventory
    assert importlib.machinery.SourceFileLoader.exec_module is original_loader


def test_frozen_legacy_cli_uses_real_inventory_and_preserves_files(tmp_path, monkeypatch):
    import research_qa_repair as qa

    root = tmp_path / 'campaign'
    base.write_depth_pass_fixture(root, deliverable_export_enabled=False)
    freeze_legacy_fixture(monkeypatch, root)
    assert qa.campaign_readiness(root)['status'] == 'PASS'
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}
    proc = run_offline_cli(base.VERIFY, '--campaign-root', str(root), '--json')
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout)['status'] == 'PASS'
    assert {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()} == before
    manifest = json.loads((root / 'manifest.json').read_text())
    manifest['unknown_semantic_change'] = 'must not inherit historical admission'
    (root / 'manifest.json').write_text(json.dumps(manifest))
    proc = run_offline_cli(base.VERIFY, '--campaign-root', str(root), '--json')
    assert proc.returncode == 3
    assert 'frozen historical inventory' in ' '.join(json.loads(proc.stdout)['failures'])
    assert qa.campaign_readiness(root)['publication_qa_allowed'] is False


def test_legacy_export_helper_preserves_intentional_stale_pack_binding(tmp_path):
    root, export = tmp_path / 'campaign', tmp_path / 'export'
    base.prepare_test_export(export)
    base.write_depth_pass_fixture(root)
    path = root / 'qa/evidence-privacy-review.json'
    receipt = json.loads(path.read_text())
    receipt['full_pack_sha256'] = '0' * 64
    path.write_text(json.dumps(receipt))
    proc = run_frozen_legacy_export(root, export, '--dry-run')
    assert proc.returncode == 3
    assert json.loads(path.read_text())['full_pack_sha256'] == '0' * 64
    assert not export.exists()
