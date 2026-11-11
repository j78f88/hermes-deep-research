#!/usr/bin/env python3
"""Tests for plain_brief_rewrite.py, research_qa_repair.py and report_purpose.py (DR-897)."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import plain_brief_rewrite as pbr
import report_purpose as rp
import research_qa_repair as rqr
from research_settings import load_settings


VALID_CALLS = """## What the owner needs to decide
1. **Should we run the bounded pilot this week?**
   - Why it matters: It tests the main assumption before more work begins.
   - If you do nothing: The recommendation remains untested and work pauses.
   - Suggested: Yes – Run the two-week pilot with the stated stop condition.
   - Urgency: week
   - Sources: [[pilot-note#Evidence]], `runs/pilot/report.md`
"""


def test_report_purpose_validation_and_resolution(tmp_path):
    assert rp.validate_report_purpose("factual") == "factual"
    assert rp.validate_report_purpose("decision_assessment") == "decision_assessment"
    with pytest.raises(ValueError):
        rp.validate_report_purpose("unknown")
    assert rp.resolve_report_purpose({"report_purpose": "factual"}, tmp_path) == "factual"


def test_c1_call_block_validation():
    assert pbr.validate_c1_call_block(VALID_CALLS, require_section=True) == []
    assert pbr.validate_c1_call_block(VALID_CALLS.replace("Yes – Run", "Yes - Run"), require_section=True) == []
    assert pbr.validate_c1_call_block("## What the owner needs to decide\n\nNo call needed.\n", require_section=True) == []
    assert pbr.validate_c1_call_block(VALID_CALLS.replace("Yes – Run", "Yes — Run"), require_section=True)
    assert pbr.validate_c1_call_block("## What the owner needs to decide\n1. Plain question?\n", require_section=True)


def test_process_vocabulary_and_lint_rules():
    assert set(pbr.process_hits("Seen in [S12] and [L03-S2 @00:12:34].")) == {"[S12]", "[L03-S2 @00:12:34]"}
    assert pbr.process_hits("The [H100](https://example.com/S12) is fast.") == []
    good = "# Short brief\n\n" + "\n\n".join(s + "\nOne plain sentence here." for s in pbr.SECTIONS)
    res = pbr.lint(good, prose=True, purpose="factual")
    assert res["ok"], res
    with_table = good + "\n| Col A | Col B |\n|---|---|\n| 1 | 2 |\n"
    assert pbr.lint(with_table, prose=True, purpose="factual")["table_in_brief"] is True
    assert pbr.lint(with_table, prose=True, purpose="factual")["ok"] is False
    with_local_link = good + "\nSee [pack](reference-pack.md).\n"
    assert pbr.lint(with_local_link, prose=True, purpose="factual")["local_md_link"] is True


def test_round_zero_pass_with_minor_notes_is_accepted_without_dispositions():
    review = {
        "verdict": "PASS",
        "blocking": [],
        "minor": ["Optional wording tweak"],
        "checks": {
            "factual": {"verdict": "PASS", "basis": "Supported."},
            "reasoning": {"verdict": "PASS", "basis": "Sound."},
            "editorial": {"verdict": "PASS", "basis": "Clear."},
            "presentation": {"verdict": "PASS", "basis": "No tables."},
        },
        "important_quantity_qualification_review": {"verdict": "PASS", "basis": "Exact."},
    }
    assert pbr.editor_review_schema_errors(review, purpose="factual", retained_findings=["prior minor note"]) == []


def test_campaign_frontmatter_and_source_extraction():
    ref = "---\ntitle: Pack\nsource: https://scx.ai/about\nauthor: Research contributors\n---\n\n# Pack\n"
    fm = pbr.render_campaign_frontmatter(
        title="Australian sovereign AI",
        date="2026-10-27",
        related='  - "[[Research Hub]]"',
        reference_text=ref,
    )
    assert "author: Research contributors\n" in fm
    assert "source: https://scx.ai/about\n" in fm
    assert pbr.extract_frontmatter_source_url("see https://a.example/x now") == "https://a.example/x"


def test_validate_reference_sources_section_and_assurance_stub(tmp_path):
    assert rqr.validate_reference_sources_section("# Pack\n\n## Sources\n- https://example.com/1\n")["status"] == "PASS"
    dup = "# Pack\n\n## Sources\n- https://example.com/1\n\n## Sources\n- https://example.com/2\n"
    assert rqr.validate_reference_sources_section(dup)["status"] == "BLOCKED_DUPLICATE_SOURCES_HEADING"
    (tmp_path / "internal").mkdir()
    pack = tmp_path / "internal" / "reference-pack.md"
    brief = tmp_path / "internal" / "plain-brief.md"
    pack.write_text("# Pack\n\n## Sources\n- https://example.com/1\n")
    brief.write_text("# Brief\n\nBody.\n")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "output_layout": "brief-and-reference-v2",
        "report_purpose": "factual",
        "internal_outputs": [{"path": "internal/reference-pack.md"}],
        "plain_brief": {"path": "internal/plain-brief.md", "review": "qa/plain-brief-review.json"},
    }))
    pack_sha = hashlib.sha256(pack.read_bytes()).hexdigest()
    brief_sha = hashlib.sha256(brief.read_bytes()).hexdigest()
    stub = rqr.prepare_preexport_assurance_stub(tmp_path, brief_sha256=brief_sha, pack_sha256=pack_sha)
    assert stub["status"] == "RESET"
    saved_stub = json.loads((tmp_path / "qa" / "final-assurance.json").read_text())
    assert saved_stub["status"] == "PENDING"
    assert saved_stub["reference_pack_sha256"] == pack_sha
    assert saved_stub["brief_sha256"] == brief_sha


def test_build_and_verify_review_packet(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello")
    pkt = rqr.build_review_packet(["a.txt"], root=tmp_path)
    assert rqr.verify_review_packet(pkt, root=tmp_path)["status"] == "PASS"
    f.write_text("changed")
    assert rqr.verify_review_packet(pkt, root=tmp_path)["status"] == "BLOCKED_CHANGED_INPUT"


def test_call_retries_when_output_is_truncated(tmp_path):
    def reply(content, finish):
        return io.BytesIO(json.dumps({
            "id": "gen-SYNTHETIC-trunc",
            "model": "anthropic/claude-opus-5.5",
            "usage": {"cost": 0},
            "choices": [{"finish_reason": finish, "message": {"content": content}}],
        }).encode())

    sent = []

    def transport(req, timeout):
        sent.append(json.loads(req.data)["max_tokens"])
        return reply("half", "length") if len(sent) == 1 else reply("whole reply.", "stop")

    settings = load_settings(environ={})
    orig_chat = pbr.model_client.chat_completion
    with patch.object(
        pbr.model_client, "chat_completion",
        side_effect=lambda *a, **k: orig_chat(*a, transport=transport, api_key="test-key", sleep=lambda _: None, **k),
    ), pbr.run_context(settings, tmp_path / "raw") as state:
        out = pbr.call("anthropic/claude-opus-5.5", "s", "u", max_tokens=1000, role="writer")
        saved = json.loads(Path(state.raw_files[0]).read_text())
    assert [c["status"] for c in saved["calls"]] == ["truncated", "success"]
    assert out == "whole reply."
    assert sent == [1000, 2000]
