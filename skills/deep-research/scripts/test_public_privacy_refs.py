"""Focused privacy-pattern regressions. Run with Python's stdlib unittest."""
import importlib.util
from pathlib import Path
import unittest


class PublicReferencePrivacyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).with_name("verify_campaign_depth.py")
        spec = importlib.util.spec_from_file_location("privacy_verify_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_public_linear_developer_docs_are_not_private_issues(self):
        for text in ["https://linear.app/developers/aig", "https://linear.app/developers/agent-interaction"]:
            self.assertEqual(self.module.external_privacy_holds(text), [])

    def test_public_model_labels_are_not_issue_keys(self):
        for text in ["GPT-6.1 Sol", "GLM-5.3", "GLM-5.3-Flash"]:
            self.assertEqual(self.module.external_privacy_holds(text), [])

    def test_private_issue_keys_and_workspace_urls_still_fail(self):
        for text in ["ISSUE-520", "LAB–123", "https://linear.app/private-team/issue/ISSUE-520/example"]:
            self.assertTrue(self.module.external_privacy_holds(text))

    def test_private_context_paths_and_wikilinks_still_fail(self):
        for text in ["[[Private Note]]", "context/context-brief.md", "internal context"]:
            self.assertTrue(self.module.external_privacy_holds(text))


class ExportPrivacyParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).with_name("export_deliverables.py")
        spec = importlib.util.spec_from_file_location("privacy_export_under_test", path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def test_export_accepts_public_docs_and_model_labels(self):
        for text in ["https://linear.app/developers/aig", "GPT-6.1 Sol", "GLM-5.3-Flash"]:
            self.assertFalse(self.module.PRIVATE_PATTERNS["linear_id"].search(text), text)

    def test_export_still_rejects_private_issue_references(self):
        for text in ["ISSUE-520", "LAB–123", "https://linear.app/private-team/issue/ISSUE-520/example"]:
            self.assertTrue(self.module.PRIVATE_PATTERNS["linear_id"].search(text), text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
