#!/usr/bin/env python3
"""forged review records fail by name, and model calls are checked at the provider.

The forged record below is the one the 2026-10-17 incident session wrote by hand (evidence bundle
``deep-research-fabrication-incident-20261017``, session log line 39), copied verbatim.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import test_deep_research_campaign_tools as base
import test_plain_brief_campaign_gate as gate
from reader_test_support import offline_plain_transport
from publication_test_support import admit_frozen_legacy
from research_settings import load_settings

SCRIPT_DIR = Path(__file__).resolve().parent

INCIDENT_FORGED_REVIEW = {
    "status": "PASS", "word_count": 576, "cap_status": "under_cap", "lint_status": "clean",
    "editor_model": "claude-opus-5.5-mock", "hashes": {"plain_brief": "mock-hash", "full_pack": "mock-hash"},
}


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ri = load("receipt_integrity")


def write_forged(root: Path) -> None:
    (root / gate.REVIEW_REL).write_text(json.dumps(INCIDENT_FORGED_REVIEW), encoding="utf-8")


# --- static check ---------------------------------------------------------------------------------

def test_incident_forged_record_is_fabricated() -> None:
    result = ri.static_check(INCIDENT_FORGED_REVIEW)
    assert result["verdict"] == "FAIL"
    assert result["class"] == "receipt_fabricated"
    joined = " ".join(result["reasons"])
    assert "claude-opus-5.5-mock" in joined and "mock-hash" in joined


def test_script_shaped_record_is_not_fabricated(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    record = json.loads((root / gate.REVIEW_REL).read_text())
    assert ri.static_check(record, require_model_ids=True)["verdict"] == "PASS"


def test_pass_record_without_any_hash_is_fabricated() -> None:
    record = {"status": "PASS", "editor_model": "openai/gpt-5.6-sol", "writer_model": "anthropic/claude-fable-5.1"}
    assert ri.static_check(record)["class"] == "receipt_fabricated"


def test_placeholder_generation_id_is_fabricated() -> None:
    record = {"status": "PASS", "brief_sha256": "a" * 64, "provenance": {"calls": [{"role": "editor", "generation_id": "mock"}]}}
    assert ri.static_check(record)["class"] == "receipt_fabricated"


# --- verifier and exporter replay (ISSUE-763 acceptance) --------------------------------------------

def test_verifier_names_forged_review_receipt_fabricated(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    write_forged(root)
    rc, payload = gate.verify(root)
    assert rc == 3
    assert payload["status"] == "FAIL"
    assert payload["classification"] == "receipt_fabricated"
    assert any(f.startswith("receipt_fabricated") and "mock-hash" in f for f in payload["failures"]), payload["failures"]


def test_verifier_names_forged_qa_receipt(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    receipt = root / "qa" / "evidence-privacy-review.json"
    record = json.loads(receipt.read_text())
    record["reviewer"] = "mock-reviewer"
    receipt.write_text(json.dumps(record), encoding="utf-8")
    rc, payload = gate.verify(root)
    assert rc == 3
    assert payload["classification"] == "receipt_fabricated"
    assert any("evidence-privacy-review.json" in f for f in payload["failures"]), payload["failures"]


def test_exporter_refuses_forged_review_by_name(tmp_path: Path) -> None:
    root, export = gate._export_campaign(tmp_path)
    write_forged(root)
    proc = base.run([sys.executable, str(base.EXPORT), "--campaign-root", str(root), "--export-root", str(export)])
    assert proc.returncode == 3
    assert "receipt_fabricated" in proc.stdout
    assert not (export / "full-research-pack.md").exists()


def test_genuine_campaign_still_passes_verifier(tmp_path: Path) -> None:
    rc, payload = gate.verify(gate.campaign(tmp_path))
    assert rc == 0, payload


# --- provider provenance --------------------------------------------------------------------------

def _record(root: Path, calls: list[dict]) -> dict:
    record = json.loads((root / gate.REVIEW_REL).read_text())
    record['writer_model'] = 'anthropic/claude-fable-5.1'
    record["provenance"] = {"run_id": "r1", "calls": calls}
    return record


def _legacy_campaign(tmp_path: Path, monkeypatch) -> Path:
    """Exercise old receipt semantics only for an explicitly frozen inventory unit."""
    root = gate.campaign(tmp_path)
    admit_frozen_legacy(monkeypatch, root)
    return root


def _calls():
    return [{"role": "writer", "generation_id": "gen-1111111111-writerAAAA", "requested_model": "anthropic/claude-fable-5.1"},
            {"role": "editor", "generation_id": "gen-2222222222-editorBBBB", "requested_model": "openai/gpt-6.1-sol-pro"}]


def _fetch(table: dict):
    return lambda gid: table.get(gid)


def test_provenance_passes_when_provider_has_the_calls(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ri, 'fetch_model_catalogue', lambda: [{
        'id': 'anthropic/claude-fable-5.1', 'canonical_slug': 'anthropic/claude-fable-5.1-20260901'}])
    root = _legacy_campaign(tmp_path, monkeypatch)
    table = {"gen-1111111111-writerAAAA": {"model": "anthropic/claude-fable-5.1-20260901", "tokens_prompt": 50000},
             "gen-2222222222-editorBBBB": {"model": "openai/gpt-6.1-sol-pro", "tokens_prompt": 50000}}
    result = ri.verify_provenance(_record(root, _calls()), root, fetch=_fetch(table))
    assert result["verdict"] == "PASS", result


def _corrected_record(root: Path, draft_rel: str) -> dict:
    record = _record(root, _calls()[1:])  # editor call only: the writer is skipped for a corrected draft
    record["corrected_draft_path"] = draft_rel
    record["body_parity"] = True
    record["writer_skipped"] = "corrected_draft"
    return record


def test_corrected_draft_without_writer_passes_when_brief_matches_draft(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)
    (root / "workers").mkdir(exist_ok=True)
    (root / "workers/draft.md").write_text((root / gate.PLAIN_REL).read_text(), encoding="utf-8")
    table = {"gen-2222222222-editorBBBB": {"model": "openai/gpt-6.1-sol-pro", "tokens_prompt": 50000}}
    result = ri.verify_provenance(_corrected_record(root, "workers/draft.md"), root, fetch=_fetch(table))
    assert result["verdict"] == "PASS", result


def test_corrected_draft_without_writer_fails_when_brief_differs(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)
    (root / "workers").mkdir(exist_ok=True)
    brief_text = (root / gate.PLAIN_REL).read_text()
    assert "project docs" in brief_text
    (root / "workers/draft.md").write_text(brief_text.replace("project docs", "project notes"), encoding="utf-8")
    table = {"gen-2222222222-editorBBBB": {"model": "openai/gpt-6.1-sol-pro", "tokens_prompt": 50000}}
    result = ri.verify_provenance(_corrected_record(root, "workers/draft.md"), root, fetch=_fetch(table))
    assert result["class"] == "receipt_fabricated", result
    assert any("writer" in r for r in result["reasons"])


def test_corrected_draft_outside_campaign_root_needs_a_writer_call(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)
    outside = tmp_path / "draft.md"
    outside.write_text((root / gate.PLAIN_REL).read_text(), encoding="utf-8")
    table = {"gen-2222222222-editorBBBB": {"model": "openai/gpt-6.1-sol-pro", "tokens_prompt": 50000}}
    result = ri.verify_provenance(_corrected_record(root, str(outside)), root, fetch=_fetch(table))
    assert result["class"] == "receipt_fabricated", result


def test_record_without_corrected_draft_still_needs_a_writer_call(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)
    table = {"gen-2222222222-editorBBBB": {"model": "openai/gpt-6.1-sol-pro", "tokens_prompt": 50000}}
    result = ri.verify_provenance(_record(root, _calls()[1:]), root, fetch=_fetch(table))
    assert result["class"] == "receipt_fabricated", result


def test_provenance_unknown_generation_is_fabricated(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)
    table = {"gen-1111111111-writerAAAA": {"model": "anthropic/claude-fable-5.1", "tokens_prompt": 50000}}
    result = ri.verify_provenance(_record(root, _calls()), root, fetch=_fetch(table))
    assert result["class"] == "receipt_fabricated"
    assert any("does not exist" in r for r in result["reasons"])


def test_provenance_wrong_served_model_is_fabricated(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)
    table = {"gen-1111111111-writerAAAA": {"model": "anthropic/claude-fable-5.1", "tokens_prompt": 50000},
             "gen-2222222222-editorBBBB": {"model": "google/gemini-3.1-pro-preview", "tokens_prompt": 50000}}
    result = ri.verify_provenance(_record(root, _calls()), root, fetch=_fetch(table))
    assert result["class"] == "receipt_fabricated"
    assert any("gemini" in r for r in result["reasons"])


def test_provenance_editor_that_never_read_the_pack_is_fabricated(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)
    table = {"gen-1111111111-writerAAAA": {"model": "anthropic/claude-fable-5.1", "tokens_prompt": 50000},
             "gen-2222222222-editorBBBB": {"model": "openai/gpt-6.1-sol-pro", "tokens_prompt": 12}}
    result = ri.verify_provenance(_record(root, _calls()), root, fetch=_fetch(table))
    assert result["class"] == "receipt_fabricated"


def test_provenance_missing_is_unverified_hold(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    record = json.loads((root / gate.REVIEW_REL).read_text())
    record.pop('provenance')
    result = ri.verify_provenance(record, root, fetch=_fetch({}))
    assert (result["verdict"], result["class"]) == ("HOLD", "receipt_unverified")


def test_provenance_provider_down_is_hold_not_pass(tmp_path: Path, monkeypatch) -> None:
    root = _legacy_campaign(tmp_path, monkeypatch)

    seen = []
    def down(_gid):
        seen.append(_gid)
        raise RuntimeError("provider unreachable")
    result = ri.verify_provenance(_record(root, _calls()), root, fetch=down)
    assert result["verdict"] == "HOLD"
    assert seen == [_calls()[0]['generation_id']]
    assert 'provider unreachable' in result['reasons'][0]


# --- the review script records provenance ---------------------------------------------------------

def test_plain_brief_call_records_generation_and_served_model(tmp_path: Path) -> None:
    plain = gate.load_plain()
    settings = load_settings(environ={})
    model = settings.resolve('brief_reviewer')['model']
    response = {"id": "gen-3333333333-recordCCCC", "model": model,
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": {"cost": 0.01}}

    plain.CALLS.clear()
    writer = {"role": "writer", "status": "success", "identity_verified": True,
              "served_model": settings.resolve('writer')['model'], "generation_id": "gen-SYNTHETIC-writer"}
    with offline_plain_transport(plain, lambda req, timeout: io.BytesIO(json.dumps(response).encode())), \
            plain.run_context(settings, tmp_path / 'raw', writers=[writer]) as state:
        result = plain.call(model, "sys", "user", role="editor")
    assert result.acceptance_eligible is True
    assert result == "ok"
    call = plain.CALLS[-1]
    assert call['role'] == 'brief_reviewer'
    assert call['requested_model'] == model and call['served_model'] == model
    assert call['generation_id'] == 'gen-3333333333-recordCCCC'
    assert call['identity_verified'] is True and call['status'] == 'success'
    assert call['cost_usd'] == 0.01
    assert state.calls == [call] and state.writers == [writer]
    saved = json.loads(Path(state.raw_files[0]).read_text())
    assert saved['calls'] == state.calls and saved['raw_responses'] == [response]


# --- provider publication lag ------------------------------------------------------------------------------

NOW = 1_792_772_922.0


def _lag_calls(age_writer: int, age_editor: int) -> list[dict]:
    return [{"role": "writer", "generation_id": f"gen-{int(NOW) - age_writer}-writerAAAA", "requested_model": "anthropic/claude-fable-5.1"},
            {"role": "editor", "generation_id": f"gen-{int(NOW) - age_editor}-editorBBBB", "requested_model": "openai/gpt-6.1-sol-pro"}]


def _lag_record(tmp_path: Path, calls: list[dict], monkeypatch) -> tuple[dict, Path]:
    root = _legacy_campaign(tmp_path, monkeypatch)
    return _record(root, calls), root


def test_recent_unpublished_generation_holds_not_fabricated(tmp_path: Path, monkeypatch) -> None:
    """Real run dr-oss-research-engines: records appeared 6 to 10 minutes after the calls."""
    record, root = _lag_record(tmp_path, _lag_calls(360, 300), monkeypatch)
    result = ri.verify_provenance(record, root, fetch=lambda gid: None, now=NOW)
    assert result["verdict"] == "HOLD" and result["class"] == "receipt_unverified"
    assert all("not yet published" in r for r in result["reasons"])


def test_old_missing_generation_is_still_fabricated(tmp_path: Path, monkeypatch) -> None:
    record, root = _lag_record(tmp_path, _lag_calls(3600, 3600), monkeypatch)
    result = ri.verify_provenance(record, root, fetch=lambda gid: None, now=NOW)
    assert result["verdict"] == "FAIL" and result["class"] == "receipt_fabricated"


def test_future_dated_missing_generation_is_fabricated(tmp_path: Path, monkeypatch) -> None:
    record, root = _lag_record(tmp_path, _lag_calls(-600, -600), monkeypatch)
    result = ri.verify_provenance(record, root, fetch=lambda gid: None, now=NOW)
    assert result["verdict"] == "FAIL" and result["class"] == "receipt_fabricated"


def test_recent_unpublished_never_hides_a_wrong_served_model(tmp_path: Path, monkeypatch) -> None:
    calls = _lag_calls(300, 300)
    table = {calls[0]["generation_id"]: {"model": "google/gemini-3.1-pro-preview", "tokens_prompt": 50000}}
    record, root = _lag_record(tmp_path, calls, monkeypatch)
    result = ri.verify_provenance(record, root, fetch=table.get, now=NOW)
    assert result["verdict"] == "FAIL" and result["class"] == "receipt_fabricated"
