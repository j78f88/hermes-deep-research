"""Offline depth-gate regressions; synthetic QA records are not live provenance."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import research_qa_repair as qa
import verify_campaign_depth as depth
from reader_test_support import semantic_review_fields
from publication_test_support import freeze_legacy_fixture, run_offline_cli


SCRIPT_DIR = Path(__file__).resolve().parent


def put(root, rel, value):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    return path


def optional_pack(root, *, monkeypatch=None, declared=True, accepted=False, v2=False):
    manifest: dict = {"report_purpose": "factual"}
    if declared:
        manifest["plain_brief"] = {"required": False, "not_required_reason": "Synthetic legacy-only fixture."}
    if v2:
        manifest["output_layout"] = "brief-and-reference-v2"
    put(root, "manifest.json", manifest)
    if monkeypatch is not None:
        manifest = freeze_legacy_fixture(monkeypatch, root)
    rel = "internal/reference-pack.md" if v2 else "internal/full-research-pack.md"
    pack = put(root, rel, "# Fixture reference\n\n## Sources\n" + "\n".join(
        f"https://example.com/evidence-{index}" for index in range(5)))
    digest = hashlib.sha256(pack.read_bytes()).hexdigest()
    # Existing pack-review checks pass; shared semantic acceptance is separately bound.
    receipt = {"status": "PASS", "blocking": [], **semantic_review_fields(),
               "reference_pack_sha256" if v2 else "internal_full_pack_sha256": digest}
    if accepted:
        receipt.update(full_pack_sha256=digest, semantic_manifest=qa.semantic_manifest_snapshot(manifest))
    put(root, "qa/evidence-privacy-review.json", receipt)
    return manifest


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("declared", [False, True])
def test_optional_brief_cannot_skip_pack_acceptance(tmp_path, monkeypatch, declared):
    manifest = optional_pack(tmp_path, monkeypatch=monkeypatch, declared=declared)
    before = snapshot(tmp_path)
    result = depth.plain_brief_gate(tmp_path, manifest)
    assert any("campaign_readiness" in reason and "absent or stale" in reason for reason in result["holds"])
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("declared", [False, True])
def test_accepted_optional_legacy_pack_remains_allowed(tmp_path, monkeypatch, declared):
    manifest = optional_pack(tmp_path, monkeypatch=monkeypatch, declared=declared, accepted=True)
    readiness = qa.campaign_readiness(tmp_path, require_brief=True)
    assert readiness["status"] == "PASS" and readiness["publication_qa_allowed"] is True
    before = snapshot(tmp_path)
    result = depth.plain_brief_gate(tmp_path, manifest)
    assert not result["failures"] and not result["holds"]
    assert result["required"] is False
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("status,allowed", [("PASS", False), ("PASS", None), ("PASS", 1), ("BLOCKED_FACTUAL_FINDINGS", True)])
def test_full_gate_requires_pass_and_literal_publication_permission(tmp_path, monkeypatch, status, allowed):
    calls = []

    def readiness(root, **kwargs):
        calls.append((root, kwargs))
        return {"status": status, "publication_qa_allowed": allowed, "findings": ["An original qualification remains open."]}

    def existing_gate(root, manifest):
        # The shared gate must run before even the delegated optional return.
        assert calls
        return {"declared": True, "required": False, "metrics": {}, "failures": [], "holds": []}

    monkeypatch.setattr(sys.modules["research_qa_repair"], "campaign_readiness", readiness)
    monkeypatch.setattr(depth, "load_plain_brief_module", lambda: SimpleNamespace(verify_campaign_plain_brief=existing_gate))
    result = depth.plain_brief_gate(tmp_path, {"plain_brief": {"required": False, "review": "qa/custom-review.json"}})
    assert calls[0][0] == tmp_path
    assert calls[0][1]["require_brief"] is True
    assert calls[0][1]["review_rel"] == "qa/custom-review.json"
    assert any("campaign_readiness" in reason and "An original qualification remains open." in reason for reason in result["holds"])


def test_shared_gate_does_not_replace_existing_lint_or_receipt_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules["research_qa_repair"], "campaign_readiness", lambda *a, **k: {
        "status": "BLOCKED_FACTUAL_FINDINGS", "publication_qa_allowed": False, "findings": ["Retained note."]})
    monkeypatch.setattr(depth, "load_plain_brief_module", lambda: SimpleNamespace(verify_campaign_plain_brief=lambda *a: {
        "declared": True, "required": True, "metrics": {"plain_brief_lint_ok": False},
        "failures": ["plain_brief_lint_failed original finding"], "holds": ["plain_brief_review_stale original finding"]}))
    result = depth.plain_brief_gate(tmp_path, {})
    assert result["failures"] == ["plain_brief_lint_failed original finding"]
    assert "plain_brief_review_stale original finding" in result["holds"]
    assert any("Retained note." in reason for reason in result["holds"])
    assert result["metrics"]["plain_brief_lint_ok"] is False


def test_shared_gate_error_holds_instead_of_escaping_or_skipping_lint(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise ValueError("Synthetic unreadable readiness input")

    monkeypatch.setattr(sys.modules["research_qa_repair"], "campaign_readiness", unavailable)
    monkeypatch.setattr(depth, "load_plain_brief_module", lambda: SimpleNamespace(verify_campaign_plain_brief=lambda *a: {
        "declared": False, "required": False, "metrics": {}, "failures": [], "holds": ["existing hold"]}))
    result = depth.plain_brief_gate(tmp_path, {})
    assert "existing hold" in result["holds"]
    assert any("campaign_readiness_unavailable ValueError" in reason for reason in result["holds"])


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("accepted", [False, True])
def test_depth_cli_checks_optional_pack_even_in_non_deep_mode(tmp_path, monkeypatch, declared, accepted):
    optional_pack(tmp_path, monkeypatch=monkeypatch, declared=declared, accepted=accepted)
    before = snapshot(tmp_path)
    proc = run_offline_cli(SCRIPT_DIR / "verify_campaign_depth.py",
                           "--campaign-root", str(tmp_path), "--mode", "board_brief_only", "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == (0 if accepted else 2), payload
    assert payload["status"] == ("PASS" if accepted else "HOLD")
    if not accepted:
        assert any("campaign_readiness" in reason for reason in payload["holds"])
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("drift", [None, 'pack', 'manifest'])
def test_complete_synthetic_legacy_depth_fixture_uses_current_pack_acceptance(tmp_path, monkeypatch, drift):
    from test_acceptance_repairs import seal_test_legacy_pack_acceptance
    from test_deep_research_campaign_tools import write_depth_pass_fixture

    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    freeze_legacy_fixture(monkeypatch, root)
    seal_test_legacy_pack_acceptance(root)
    if drift == 'pack':
        pack = root / 'internal/full-research-pack.md'
        pack.write_text(pack.read_text() + '\nChanged after evidence review.\n')
    elif drift == 'manifest':
        manifest = json.loads((root / 'manifest.json').read_text())
        manifest['new_semantic_setting'] = 'changed after evidence review'
        put(root, 'manifest.json', manifest)
    before = snapshot(root)
    proc = run_offline_cli(SCRIPT_DIR / "verify_campaign_depth.py",
                           "--campaign-root", str(root), "--json")
    payload = json.loads(proc.stdout)
    assert proc.returncode == {None: 0, 'pack': 2, 'manifest': 3}[drift], payload
    assert payload["status"] == {None: 'PASS', 'pack': 'HOLD', 'manifest': 'FAIL'}[drift]
    if drift == 'pack':
        assert any("campaign_readiness" in reason and 'absent or stale' in reason for reason in payload["holds"])
    elif drift == 'manifest':
        assert any('report_purpose missing' in reason and 'frozen historical inventory' in reason for reason in payload['failures'])
    assert snapshot(root) == before


def test_two_output_campaign_cannot_declare_brief_optional(tmp_path):
    manifest = optional_pack(tmp_path, accepted=True, v2=True)
    result = depth.plain_brief_gate(tmp_path, manifest)
    assert any("only frozen historical campaigns may omit the current plain brief" in reason for reason in result["holds"])


@pytest.mark.parametrize('declared', [False, True])
def test_nonlegacy_optional_brief_is_blocked_even_with_accepted_pack(tmp_path, declared):
    manifest = optional_pack(tmp_path, declared=declared, accepted=True)
    before = snapshot(tmp_path)
    readiness = qa.campaign_readiness(tmp_path)
    assert readiness['status'] == 'BLOCKED_CAMPAIGN_READINESS'
    assert readiness['publication_qa_allowed'] is False
    assert readiness['findings'] == ['only frozen historical campaigns may omit the current plain brief']
    result = depth.plain_brief_gate(tmp_path, manifest)
    assert any('only frozen historical campaigns may omit the current plain brief' in hold for hold in result['holds'])
    assert snapshot(tmp_path) == before


def test_unknown_purposeless_optional_pack_fails_closed_without_inventory(tmp_path):
    manifest = optional_pack(tmp_path, accepted=True)
    manifest.pop('report_purpose')
    put(tmp_path, 'manifest.json', manifest)
    before = snapshot(tmp_path)
    readiness = qa.campaign_readiness(tmp_path)
    assert readiness['publication_qa_allowed'] is False
    assert 'frozen historical inventory' in ' '.join(readiness['findings'])
    proc = run_offline_cli(SCRIPT_DIR / 'verify_campaign_depth.py',
                           '--campaign-root', str(tmp_path), '--mode', 'board_brief_only', '--json')
    assert proc.returncode == 3
    assert 'frozen historical inventory' in ' '.join(json.loads(proc.stdout)['failures'])
    assert snapshot(tmp_path) == before
