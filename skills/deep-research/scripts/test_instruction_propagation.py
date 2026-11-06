"""instruction-only controls; these do not certify research completeness.

For RED replay, point RESEARCH_TEST_INIT and RESEARCH_TEST_SKILL at git-show copies of the
base module and static guidance. No completion/checker behaviour is replaced.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL = Path(os.environ.get("RESEARCH_TEST_SKILL", SCRIPT_DIR.parent))
INIT = Path(os.environ.get("RESEARCH_TEST_INIT", SCRIPT_DIR / "init_campaign.py"))
sys.path.insert(0, str(SCRIPT_DIR))
spec = importlib.util.spec_from_file_location("init_under_test", INIT)
assert spec is not None and spec.loader is not None
init = importlib.util.module_from_spec(spec)
spec.loader.exec_module(init)


def args(layout="brief-and-reference-v2"):
    return SimpleNamespace(
        target="Example platform", target_type="product", primary_source="https://example.com",
        decision_question="Build a news digest delivered to Discord?", audience="owner",
        output_boundary="private research only", evidence_cutoff="2026-10-24",
        date="2026-10-24", output_layout=layout,
    )


class InstructionPropagationTests(unittest.TestCase):
    def packets(self):
        lane = init.lane_plan("product")[0]
        for layout in ("brief-and-reference-v2", "legacy"):
            yield layout, init.worker_packet(args(layout), lane, Path("campaign"))

    def assert_terms(self, text, terms):
        for term in terms:
            self.assertTrue(term.casefold() in text.casefold(), f"missing instruction: {term}")

    def test_both_worker_layouts_attach_factual_inventory_and_preflight_before_dispatch(self):
        for layout, text in self.packets():
            with self.subTest(layout=layout):
                self.assert_terms(text, ["context/owned-assets.md", ".json", "receipts/tool-preflight.json",
                                         "before dispatch", "facts", "presence only", "controller"])

    def test_both_worker_layouts_preserve_context_and_legacy_privacy_isolation(self):
        for layout, text in self.packets():
            with self.subTest(layout=layout):
                self.assert_terms(text, ["Do not use the Context Brief", "private IDs/paths",
                                         "legacy external outputs", "shareable asset names", "internal sidecars"])
                self.assertNotIn("internal notes, related repos", text)
        legacy = dict(self.packets())["legacy"]
        self.assertIn("frozen and hashed", legacy)
        self.assertIn("only after all research lanes finish", dict(self.packets())["brief-and-reference-v2"])

    def test_both_worker_layouts_require_exact_name_findings_and_evidence_dispositions(self):
        for layout, text in self.packets():
            with self.subTest(layout=layout):
                self.assert_terms(text, ["exact name", "reader-facing findings", "owned-asset-dispositions.json",
                                         "name only", "contents read", "proven working", "reuse", "compare",
                                         "unsuitable", "unknown", "omission reason", "current_state_do_nothing",
                                         "evidence budget", "fastest, best use"])

    def test_both_worker_layouts_measure_all_pages_not_advertised_totals(self):
        for layout, text in self.packets():
            with self.subTest(layout=layout):
                self.assert_terms(text, ["at least 15", "category browse", "all pages", "len(items)",
                                         "advertised available", "actually read", "coverage ratio", "shortfall",
                                         "unknown denominator", "HOLD"])

    def test_both_worker_layouts_check_platform_features_and_distinct_surfaces(self):
        for layout, text in self.packets():
            with self.subTest(layout=layout):
                self.assert_terms(text, ["saved tasks", "schedules", "webhooks", "monitoring", "MCP",
                                         "storage", "dedupe", "named product surface", "not researched"])

    def test_both_worker_layouts_offer_conditional_estimated_hours_not_default_roadmap(self):
        for layout, text in self.packets():
            with self.subTest(layout=layout):
                self.assert_terms(text, ["If the ask", "build-now", "estimated hours", "prerequisites",
                                         "owner", "before synthesis", "standing approvals", "missing",
                                         "multi-week roadmap"])

    def test_steering_orders_factual_discovery_and_preflight_before_dispatch(self):
        for layout in ("brief-and-reference-v2", "legacy"):
            text = init.steering(args(layout), init.lane_plan("product"), Path("campaign"))
            with self.subTest(layout=layout):
                self.assert_terms(text, ["before research dispatch", "context/owned-assets.md", ".json",
                                         "receipts/tool-preflight.json", "closed tracker issues", "read-only",
                                         "installed skills", "local apps", "hermes cron list", "recipes",
                                         "Apify monitor recipe", "competitor-news-monitor", "presence only",
                                         "owned-asset-dispositions.json", "at least 15", "category browse",
                                         "len(items)", "platform features", "build-now", "estimated hours"])

    def test_reference_pack_is_question_led_with_coverage_in_worker_instructions(self):
        text = init.reference_pack(args())
        self.assert_terms(text, ["Answer and material limitation", "Findings by research question",
                                 "Conflicting evidence and interpretations", "Limits and gaps", "Sources"])
        for irrelevant_chapter in ("## Build-now", "## Catalogue coverage", "## Strategic option portfolio"):
            self.assertNotIn(irrelevant_chapter, text)
        # Coverage and conditional delivery instructions remain mandatory when applicable,
        # but no longer impose unrelated chapters on every factual reader.
        packet = init.worker_packet(args(), init.lane_plan("product")[0], Path("campaign"))
        self.assert_terms(packet, ["If the ask", "build-now", "estimated hours", "prerequisites",
                                   "exact name", "name only", "contents read", "proven working",
                                   "advertised available", "actually returned", "actually read",
                                   "coverage ratio", "shortfall", "len(items)", "at least 15",
                                   "category browse", "not researched"])
        for forbidden in ("Action Register", "QA receipt", "Context Brief", "controller_inference"):
            self.assertNotIn(forbidden, text)

    def test_independent_evidence_privacy_packet_checks_original_gaps_and_holds_catalogue(self):
        text = init.qa_packet("evidence-privacy", "Verify evidence fidelity and privacy.")
        self.assert_terms(text, ["reviewer must be distinct", "Apify monitor recipe", "competitor-news-monitor",
                                 "owned-assets", "tool-preflight", "before dispatch", "exact name",
                                 "owned-asset-dispositions.json", "len(items)", "at least 15", "category browse",
                                 "platform features", "build-now", "HOLD", "incomplete catalogue",
                                 "do not prove", "private IDs/paths", "legacy external"])
        self.assertNotIn("Apify monitor recipe", init.qa_packet("visual", "Verify rendered diagrams."))

    def test_static_worker_template_propagates_coverage_and_privacy_contract(self):
        text = (SKILL / "templates/worker-packet.md").read_text()
        self.assert_terms(text, ["BOTH", "legacy", "before dispatch", "receipts/tool-preflight.json",
                                 "owned-asset-dispositions.json", "name only", "contents read", "proven working",
                                 "at least 15", "category browse", "len(items)", "advertised available",
                                 "coverage ratio", "HOLD", "private IDs/paths", "build-now", "estimated hours"])

    def test_static_router_assigns_existing_lanes_not_a_new_engine(self):
        text = (SKILL / "references/target-type-router.md").read_text()
        self.assert_terms(text, ["before research dispatch", "owned-assets", "tool-preflight", "Apify monitor recipe",
                                 "competitor-news-monitor", "at least 15", "category browse", "len(items)",
                                 "owned-asset-dispositions.json", "build-now", "estimated hours",
                                 "Existing checker/verifier passes do not prove"])

    def test_guidance_does_not_claim_deterministic_catalogue_enforcement(self):
        text = (SKILL / "references/owned-assets-coverage-and-build-now.md").read_text()
        self.assertNotIn("The depth check treats a catalogue lane without coverage numbers as HOLD", text)
        self.assert_terms(text, ["independent", "HOLD", "do not", "deterministic"])

    def test_real_scaffold_keeps_receipts_honestly_incomplete_in_both_layouts(self):
        output = os.environ.get("RESEARCH_TEST_OUTPUT")
        with tempfile.TemporaryDirectory(prefix="stage784-", dir=output) as directory:
            for layout in ("brief-and-reference-v2", "legacy"):
                root = Path(directory) / layout
                argv = [str(INIT), "--target", "Example platform", "--target-type", "product",
                        "--root", str(root), "--date", "2026-10-24", "--evidence-cutoff", "2026-10-24",
                        "--output-layout", layout, "--no-deliverable-export"]
                with patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(init.main(), 0)
                manifest = json.loads((root / "manifest.json").read_text())
                for lane in manifest["lanes"]:
                    text = (root / "workers" / (lane["id"] + "-packet.md")).read_text()
                    self.assertIn("receipts/tool-preflight.json", text)
                receipt = json.loads((root / "qa/evidence-privacy-review.json").read_text())
                self.assertEqual(receipt["status"], "HOLD")
                self.assertIsNone(receipt["reviewer"])
                self.assertIn("incomplete catalogue", (root / "qa/packets/evidence-privacy.md").read_text())
                self.assertFalse((root / "receipts/tool-preflight.json").exists())
                self.assertEqual(manifest["pre_seed"]["status"], "HOLD")
                if layout == "legacy":
                    external = (root / "external/original-full-research-pack.md").read_text()
                    self.assertNotIn("Apify monitor recipe", external)
                    self.assertNotIn("owned-asset-dispositions.json", external)


if __name__ == "__main__":
    unittest.main(verbosity=2)
