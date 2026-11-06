#!/usr/bin/env python3
"""Tests for the plain-brief C1 decision-call contract."""
from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reader_test_support import editor_pass, offline_plain_client, offline_plain_transport
from research_settings import load_settings

SCRIPT = Path(__file__).with_name("plain_brief_rewrite.py")


def load_module():
    spec = importlib.util.spec_from_file_location("plain_brief_rewrite", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    with patch("builtins.open", return_value=io.StringIO("OPENROUTER_API_KEY=test-key\n")):
        spec.loader.exec_module(module)
    return module


VALID_CALLS = """## What the owner needs to decide
1. **Should we run the bounded pilot this week?**
   - Why it matters: It tests the main assumption before more work begins.
   - If you do nothing: The recommendation remains untested and work pauses.
   - Suggested: Yes – Run the two-week pilot with the stated stop condition.
   - Urgency: week
   - Sources: [[pilot-note#Evidence]], `runs/pilot/report.md`
2. **Should we defer the integration until the pilot is reviewed?**
   - Why it matters: Integration would be harder to reverse than the pilot.
   - If you do nothing: The integration stays outside the approved scope.
   - Suggested: Later - Review this after the pilot results are available.
   - Sources: [[pilot-note#Recommendation]]
"""


class PlainBriefCallBlockTests(unittest.TestCase):
    def test_accepts_en_dash_delimiter(self):
        module = load_module()
        self.assertEqual(module.validate_c1_call_block(VALID_CALLS, require_section=True), [])

    def test_accepts_ascii_hyphen_delimiter(self):
        module = load_module()
        markdown = VALID_CALLS.replace("Yes – Run", "Yes - Run")
        self.assertEqual(module.validate_c1_call_block(markdown, require_section=True), [])

    def test_accepts_case_insensitive_labels(self):
        module = load_module()
        markdown = VALID_CALLS.replace("Why it matters:", "WHY IT MATTERS:").replace(
            "If you do nothing:", "If You Do Nothing:"
        ).replace("Suggested:", "sUgGeStEd:").replace("Sources:", "SOURCES:")
        self.assertEqual(module.validate_c1_call_block(markdown, require_section=True), [])

    def test_accepts_exact_no_call_sentinel(self):
        module = load_module()
        markdown = "## What the owner needs to decide\n\nNo call needed.\n"
        self.assertEqual(module.validate_c1_call_block(markdown, require_section=True), [])

    def test_rejects_em_dash_delimiter(self):
        module = load_module()
        markdown = VALID_CALLS.replace("Yes – Run", "Yes — Run")
        errors = module.validate_c1_call_block(markdown, require_section=True)
        self.assertTrue(any("Suggested" in error for error in errors), errors)

    def test_rejects_non_sentinel_prose(self):
        module = load_module()
        errors = module.validate_c1_call_block(
            "## What the owner needs to decide\n\nNothing for the owner to decide.\n",
            require_section=True,
        )
        self.assertTrue(errors)

    def test_rejects_plain_numbered_questions(self):
        module = load_module()
        markdown = """## What the owner needs to decide
1. Should we run the pilot?
"""
        errors = module.validate_c1_call_block(markdown, require_section=True)
        self.assertTrue(any("bold question" in error for error in errors), errors)

    def test_rejects_missing_required_fields(self):
        module = load_module()
        markdown = """## What the owner needs to decide
1. **Should we run the pilot?**
   - Why it matters: It tests the key assumption.
   - Suggested: Yes – Run the pilot.
   - Sources: [[pilot-note#Evidence]]
"""
        errors = module.validate_c1_call_block(markdown, require_section=True)
        self.assertTrue(any("If you do nothing" in error for error in errors), errors)

    def test_lint_fails_when_decision_calls_are_not_c1(self):
        module = load_module()
        headings = "\n\n".join(section + "\n" + ("word " * 115) for section in module.SECTIONS)
        markdown = "# Test brief\n\n" + headings + "\n\n## What the owner needs to decide\n1. Should we proceed?\n"
        result = module.lint(markdown)
        self.assertFalse(result["ok"])
        self.assertTrue(result["decision_call_errors"])


class PlainBriefProcessVocabularyTests(unittest.TestCase):
    def test_flags_citation_codes(self):
        module = load_module()
        hits = module.process_hits("Seen in [S12], [L3.S4] and [L03-S2 @00:12:34].")
        self.assertEqual(set(hits), {"[S12]", "[L3.S4]", "[L03-S2 @00:12:34]"})

    def test_ignores_link_text_and_urls(self):
        module = load_module()
        md = "The [H100](https://example.com/S12) and [docs](https://example.com/[S3]) and https://x.io/a" + "b" * 40
        self.assertEqual(module.process_hits(md), [])

    def test_keeps_plain_topic_words(self):
        module = load_module()
        self.assertEqual(module.process_hits("A signed receipt proves provenance for each build that passes."), [])

    def test_lint_has_cap_but_no_floor(self):
        module = load_module()
        short = "# Short brief\n\n" + "\n\n".join(s + "\nOne plain sentence here." for s in module.SECTIONS)
        self.assertTrue(module.lint(short)["ok"], module.lint(short))
        long = short.replace("One plain sentence here.", "One plain sentence here. " * 200)
        self.assertFalse(module.lint(long)["ok"])
        self.assertGreater(module.lint(long)["words"], module.MAX_WORDS)


class PlainBriefstage817Tests(unittest.TestCase):
    def test_campaign_frontmatter_uses_research_contributors_and_url_source(self):
        module = load_module()
        ref_text = "---\ntitle: Pack\nsource: https://scx.ai/about\nauthor: Research contributors\n---\n\n# Pack\n"
        fm = module.render_campaign_frontmatter(
            title="Australian sovereign AI",
            date="2026-10-27",
            related='  - "[[Supporting source]]"',
            reference_text=ref_text,
        )
        self.assertIn("author: Research contributors\n", fm)
        self.assertIn("source: https://scx.ai/about\n", fm)
        self.assertNotIn("author: Research agent", fm)

    def test_frontmatter_source_falls_back(self):
        module = load_module()
        self.assertEqual(module.extract_frontmatter_source_url("see https://a.example/x now"), "https://a.example/x")
        self.assertEqual(module.extract_frontmatter_source_url("no url"), "https://hermes-agent.nousresearch.com/docs")

    def test_corrected_draft_fails_fast_after_round_zero_when_editor_blocks(self):
        module = load_module()
        draft = "# Draft\n\n## What we found\nRevenue was A$8 million.\n"
        calls = []

        def respond(role, system, user):
            calls.append(role)
            if role == module.EDITOR:
                return json.dumps({"verdict": "FAIL", "blocking": ["Fix revenue figure"], "minor": []})
            self.fail("corrected draft must not invoke a writer")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            out.mkdir()
            draft_path = Path(tmp) / "draft.md"
            draft_path.write_text(draft, encoding="utf-8")
            p = {"title": "T", "slug": "t", "sources": [], "intent": "test"}
            with offline_plain_client(module, respond):
                _, final = module.rewrite("t", p, out, corrected_draft=draft_path)
            review = json.loads((out / "t.review.json").read_text(encoding="utf-8"))
            written = (out / "t.md").read_text(encoding="utf-8")
        self.assertEqual(calls.count("writer"), 0)
        self.assertEqual(calls.count("brief_reviewer"), 1)
        self.assertEqual(len(review["rounds"]), 1)
        self.assertEqual(final["editor"], "FAIL")
        self.assertIn("Fix revenue figure", review["rounds"][0]["editor"]["blocking"])
        self.assertTrue(review["body_parity"])
        self.assertEqual(review["corrected_draft_path"], str(draft_path))
        self.assertEqual(written.strip(), draft.strip())

    def test_corrected_draft_campaign_brief_keeps_draft_body_without_writer(self):
        module = load_module()
        good_body = "# Short brief\n\n" + "\n\n".join(s + "\nOne plain sentence here." for s in module.SECTIONS)
        calls = []

        def respond(role, system, user):
            calls.append(role)
            if role == module.EDITOR:
                return json.dumps(editor_pass())
            self.fail("corrected draft must not invoke a writer")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "internal").mkdir()
            (root / "internal/reference-pack.md").write_text("---\nsource: https://scx.ai\n---\n# Pack\n", encoding="utf-8")
            (root / "manifest.json").write_text(json.dumps({
                "report_purpose": "factual",
                "output_layout": "brief-and-reference-v2",
                "target": "SCX",
                "internal_outputs": [{"path": "internal/reference-pack.md"}],
                "plain_brief": {"required": True, "path": "internal/plain-brief.md", "review": "qa/plain-brief-review.json"},
            }), encoding="utf-8")
            (root / "qa").mkdir()
            review = {"status": "PASS", "reference_pack_sha256": hashlib.sha256((root / "internal/reference-pack.md").read_bytes()).hexdigest(), **editor_pass()}
            (root / "qa/evidence-privacy-review.json").write_text(json.dumps(review), encoding="utf-8")
            draft_path = root / "draft.md"
            draft_path.write_text(good_body, encoding="utf-8")
            with offline_plain_client(module, respond):
                rec = module.campaign_brief(root, corrected_draft=draft_path)
            brief = (root / "internal/plain-brief.md").read_text(encoding="utf-8")
        self.assertEqual(calls, ["brief_reviewer"])
        self.assertTrue(rec["body_parity"])
        self.assertEqual(rec["writer_skipped"], "corrected_draft")
        self.assertEqual(rec["status"], "FAIL")
        self.assertEqual(rec["acceptance_status"], "HOLD")
        self.assertIsNone(rec["writer_model"])
        self.assertFalse(rec["acceptance_eligible"])
        self.assertEqual(rec["artifact_writers"], [{"role": "corrected_draft", "status": "unknown_author"}])
        self.assertIn("Reviewer independence from actual authors is unverified", rec["editor_blocking"])
        self.assertFalse(rec["readiness"]["publication_qa_allowed"])
        self.assertEqual(module.normalise_body_for_parity(brief), module.normalise_body_for_parity(good_body))


class CallTruncationTests(unittest.TestCase):
    @staticmethod
    def _reply(content, finish):
        return io.BytesIO(json.dumps({"id": "gen-SYNTHETIC-truncation", "model": "anthropic/claude-opus-5.5", "usage": {"cost": 0},
                                      "choices": [{"finish_reason": finish, "message": {"content": content}}]}).encode())

    def test_call_retries_with_more_tokens_when_output_is_cut_off(self):
        module = load_module()
        sent = []

        def transport(req, timeout):
            sent.append(json.loads(req.data)["max_tokens"])
            return self._reply("half a sent", "length") if len(sent) == 1 else self._reply("whole reply.", "stop")

        settings = load_settings(environ={})
        with tempfile.TemporaryDirectory() as tmp, offline_plain_transport(module, transport), \
                module.run_context(settings, Path(tmp) / "raw") as state:
            out = module.call("anthropic/claude-opus-5.5", "s", "u", max_tokens=1000, role="writer")
            saved = json.loads(Path(state.raw_files[0]).read_text())
        self.assertEqual([call["status"] for call in saved["calls"]], ["truncated", "success"])
        self.assertEqual(len(saved["raw_responses"]), 2)
        self.assertEqual(out, "whole reply.")
        self.assertEqual(sent, [1000, 2000])

    def test_call_fails_when_output_stays_cut_off(self):
        module = load_module()
        settings = load_settings(environ={}, overrides={"execution": {"attempts": 2}})
        with tempfile.TemporaryDirectory() as tmp, \
                offline_plain_transport(module, lambda req, timeout: self._reply("cut", "length")), \
                module.run_context(settings, Path(tmp) / "raw") as state:
            with self.assertRaises(module.model_client.ModelCallError) as ctx:
                module.call("anthropic/claude-opus-5.5", "s", "u", max_tokens=1000, role="writer")
            saved = json.loads(Path(state.raw_files[0]).read_text())
        self.assertIn("truncated", str(ctx.exception))
        self.assertEqual([call["status"] for call in saved["calls"]], ["truncated", "truncated"])
        self.assertEqual(len(saved["raw_responses"]), 2)


if __name__ == "__main__":
    unittest.main()
