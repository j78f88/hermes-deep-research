"""Offline fixture bindings exercise the shared gate without changing its rules."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import reader_test_support as support
import research_qa_repair as qa
import test_deep_research_campaign_tools as base
import test_plain_brief_campaign_gate as gate


def read(root: Path, rel: str) -> dict:
    return json.loads((root / rel).read_text(encoding="utf-8"))


def write(root: Path, rel: str, payload: dict) -> None:
    (root / rel).write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("include_preseed", [False, True])
def test_depth_fixture_binds_optional_legacy_pack_acceptance(tmp_path, monkeypatch, include_preseed):
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root, deliverable_export_enabled=False, include_preseed=include_preseed)
    from publication_test_support import freeze_legacy_fixture
    freeze_legacy_fixture(monkeypatch, root)
    manifest = read(root, "manifest.json")
    receipt = read(root, "qa/evidence-privacy-review.json")
    pack = root / "internal/full-research-pack.md"
    assert receipt.get("full_pack_sha256") == hashlib.sha256(pack.read_bytes()).hexdigest()
    assert receipt.get("semantic_manifest") == qa.semantic_manifest_snapshot(manifest)
    readiness = qa.campaign_readiness(root)
    assert readiness["status"] == "PASS", readiness
    assert readiness["publication_qa_allowed"] is True
    assert not (root / gate.REVIEW_REL).exists()
    assert not (root / "qa/editor-note-decisions.json").exists()


def test_fixture_binding_refresh_preserves_existing_negative_judgements(tmp_path):
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root, deliverable_export_enabled=False)
    manifest = read(root, "manifest.json")
    manifest["plain_brief"] = {"required": False}
    manifest["report_purpose"] = "understanding"
    write(root, "manifest.json", manifest)
    receipt = read(root, "qa/evidence-privacy-review.json")
    receipt.update(status="HOLD", blocking=["Synthetic unresolved finding"], fixture_marker={"keep": True})
    write(root, "qa/evidence-privacy-review.json", receipt)

    support.refresh_fixture_acceptance_bindings(root)

    refreshed = read(root, "qa/evidence-privacy-review.json")
    expected = dict(receipt, semantic_manifest=qa.semantic_manifest_snapshot(manifest))
    assert refreshed == expected
    assert not qa.campaign_readiness(root)["publication_qa_allowed"]
    assert not (root / gate.REVIEW_REL).exists()
    assert not (root / "qa/editor-note-decisions.json").exists()


def test_fixture_binding_refresh_preserves_pair_findings_and_reader_hashes(tmp_path):
    root = gate.campaign(tmp_path)
    manifest = read(root, "manifest.json")
    manifest["output_layout"] = "something-else"
    write(root, "manifest.json", manifest)
    assert not qa.campaign_readiness(root)["publication_qa_allowed"]
    decisions_rel = "qa/editor-note-decisions.json"
    decisions = read(root, decisions_rel)
    decisions.update(retained_findings=["Retain this synthetic note"], decisions=[{"note": "Unresolved"}])
    write(root, decisions_rel, decisions)
    review_before = (root / gate.REVIEW_REL).read_bytes()
    pack = root / "internal/full-research-pack.md"
    pack.write_text(pack.read_text() + "\nChanged fixture reader.\n", encoding="utf-8")

    support.refresh_fixture_acceptance_bindings(root)

    assert (root / gate.REVIEW_REL).read_bytes() == review_before
    assert read(root, decisions_rel) == dict(decisions, semantic_manifest=qa.semantic_manifest_snapshot(manifest))
    assert not qa.campaign_readiness(root)["publication_qa_allowed"]


def test_fixture_binding_refresh_does_not_create_missing_evidence(tmp_path):
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root, deliverable_export_enabled=False)
    receipt = root / "qa/evidence-privacy-review.json"
    receipt.unlink()
    support.refresh_fixture_acceptance_bindings(root)
    assert not receipt.exists()
    assert not qa.campaign_readiness(root)["publication_qa_allowed"]


def test_synthetic_plain_client_exercises_wrapper_and_actual_writer_identity(tmp_path):
    from research_settings import load_settings

    plain = gate.load_plain()
    settings = load_settings(environ={})
    seen = []

    def respond(role, system, user):
        seen.append((role, user))
        return "Synthetic brief." if role == "writer" else json.dumps(support.editor_pass())

    with support.offline_plain_client(plain, respond), plain.run_context(settings, tmp_path) as state:
        assert plain.call(plain.WRITER, "s", "draft", role="writer") == "Synthetic brief."
        result = plain.call(plain.EDITOR, "s", "review", role="editor")
        assert result.acceptance_eligible is True
        assert [call["role"] for call in state.calls] == ["writer", "brief_reviewer"]
        assert state.writers == state.calls[:1]
        assert all(call["identity_verified"] is True for call in state.calls)
        assert {call["served_model"].split("/")[0] for call in state.calls} == {"anthropic", "openai"}
    assert seen == [("writer", "draft"), ("brief_reviewer", "review")]
    raw = [json.loads(path.read_text()) for path in sorted(tmp_path.glob("*.raw.json"))]
    assert len(raw) == 2
    assert all(item["raw_responses"][0]["synthetic"] is True for item in raw)


def test_synthetic_editor_does_not_invent_corrected_draft_writer(tmp_path):
    from research_settings import load_settings

    plain = gate.load_plain()
    settings = load_settings(environ={})
    unknown = {"role": "corrected_draft", "status": "unknown_author"}
    with support.offline_plain_client(plain, lambda *args: json.dumps(support.editor_pass())), \
            plain.run_context(settings, tmp_path, writers=[unknown]) as state:
        result = plain.call(plain.EDITOR, "s", "handwritten draft", role="editor")
        assert result.acceptance_eligible is False
        assert state.writers == [unknown]
        assert len(state.calls) == 1


def test_offline_transport_blocks_legacy_network_and_real_key_lookup(tmp_path, monkeypatch):
    import io
    from research_settings import load_settings

    plain = gate.load_plain()
    monkeypatch.setattr(plain.model_client, "_api_key", lambda: pytest.fail("real key lookup"))
    monkeypatch.setattr(plain.urllib.request, "urlopen", lambda *args, **kwargs: pytest.fail("real network"))
    settings = load_settings(environ={})

    def transport(request, timeout):
        assert request.get_header("Authorization") == "Bearer synthetic-offline-only"
        return io.BytesIO(json.dumps({
            "id": "gen-SYNTHETIC-transport", "model": settings.resolve("writer")["model"],
            "choices": [{"finish_reason": "stop", "message": {"content": "offline"}}],
        }).encode())

    with support.offline_plain_transport(plain, transport), plain.run_context(settings, tmp_path):
        assert plain.model_client._api_key() == "synthetic-offline-only"
        with pytest.raises(AssertionError, match="legacy network forbidden"):
            plain.urllib.request.urlopen("https://example.invalid")
        assert plain.call(plain.WRITER, "s", "u", role="writer") == "offline"
