#!/usr/bin/env python3
"""the plain brief is the canonical human-facing output of a true_deep_research campaign.

Covers the verifier gate (process vocabulary, citation codes, cap-only length, lint, independent
editor review bound to the final full pack), deliverable export of the plain brief, the campaign-mode
generator, and the controller phase. Legacy campaigns without a ``plain_brief`` block are untouched.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import test_deep_research_campaign_tools as base
from reader_test_support import editor_pass, offline_plain_client, semantic_review_fields
from publication_test_support import (
    add_generated_provenance, bind_fixture_review_packet, run_frozen_legacy_cli,
    run_synthetic_export,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PLAIN = SCRIPT_DIR / "plain_brief_rewrite.py"
PLAIN_REL = "internal/plain-brief.md"
REVIEW_REL = "qa/plain-brief-review.json"


def load_plain():
    spec = importlib.util.spec_from_file_location("plain_brief_rewrite", PLAIN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD_BODY = """# Example Product: whether to adopt it now

**Bottom line.** Example Product does the job, but only for small teams. Try it on one project first. Stop if setup takes more than a day.

## What was asked
the owner asked whether Example Product is worth adopting for the home lab.

## What we found
- It sends alerts when a backup fails, according to the [project docs](https://example.com/docs).
- It needs one small server. The docs list 2 GB of memory as the minimum.
- Nobody outside the vendor has published a long-term review yet.

## Options compared
| Option | What it means in practice | For | Against |
|---|---|---|---|
| Adopt now | Install it on the main server this week | Fast feedback | Little outside evidence |
| Wait | Check again in three months | No effort now | Backups stay unwatched |

Trying it on one project gives most of the benefit at low risk.

## What to do
Install it on the test server. Point it at one backup job. Check the alerts for two weeks.

## Risks and when to stop
- Stop if it misses a failed backup.
- Stop if it needs more than a day of setup.
- Stop if the project goes quiet for six months.

## What is still missing
There is no independent review of how it behaves over a year.

## What the owner needs to decide
No call needed.
"""


def plain_file(body: str = GOOD_BODY) -> str:
    return (
        "---\ntitle: Example Product — whether to adopt it now\ncreated: 2026-10-17\ntype: source\nstage: import\n"
        "source: plain brief of the internal full research pack\nauthor: Research agent / Hermes internal synthesis\n"
        "captured_via: agent-deep-research\ntags: [source/adapted]\nrelated:\n"
        "  - \"[[full-research-pack]]\"\n  - \"[[executive-brief]]\"\nverified_count: 0\n---\n\n"
        + body.strip()
        + "\n\n---\nDetail and sources: [[full-research-pack|full research pack]], [[executive-brief|executive brief]].\n"
    )


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_plain_brief(root: Path, *, body: str = GOOD_BODY, review: dict | None = None, write_review: bool = True) -> None:
    """Upgrade the legacy PASS fixture to the ISSUE-747 contract with a passing plain brief."""
    brief = root / PLAIN_REL
    brief.write_text(plain_file(body), encoding="utf-8")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["plain_brief"] = {"required": True, "path": PLAIN_REL, "review": REVIEW_REL, "status": "reviewed_pass"}
    manifest["internal_outputs"].append({"path": PLAIN_REL})
    ve = manifest["deliverable_exports"]
    ve["allowlist"] = ve["allowlist"] + [PLAIN_REL]
    ve["plain_brief_staging"] = PLAIN_REL
    if ve.get("enabled") and ve.get("status") == "copied_to_export":
        dest = Path(ve["export_root"]) / brief.name
        dest.write_bytes(brief.read_bytes())
        ve["exports"].append({"staged_path": PLAIN_REL, "export_path": str(dest), "sha256": sha(brief), "readback_sha256": sha(brief)})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if write_review:
        record = {
            "schema_version": "deep-research-plain-brief-review-v1",
            "status": "PASS",
            "editor_verdict": "PASS",
            "editor_blocking": [],
            **semantic_review_fields(),
            "writer_model": "anthropic/claude-opus-5.5",
            "editor_model": "openai/gpt-6.1-sol-pro",
            "brief_path": PLAIN_REL,
            "brief_sha256": sha(brief),
            "full_pack_sha256": sha(root / "internal" / "full-research-pack.md"),
        }
        add_generated_provenance(root, record)
        (root / REVIEW_REL).write_text(json.dumps(record), encoding="utf-8")
        from test_acceptance_repairs import seal_test_acceptance
        seal_test_acceptance(root)
        if review:
            record = json.loads((root / REVIEW_REL).read_text())
            record.update(review)
            (root / REVIEW_REL).write_text(json.dumps(record), encoding="utf-8")
            bind_fixture_review_packet(root)


def verify(root: Path) -> tuple[int, dict]:
    proc = base.run([sys.executable, str(base.VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])
    return proc.returncode, json.loads(proc.stdout)


def campaign(tmp_path: Path, **kwargs) -> Path:
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root)
    add_plain_brief(root, **kwargs)
    return root


# --- verifier: positive and legacy -------------------------------------------------------------

def test_verifier_passes_campaign_with_clean_plain_brief(tmp_path: Path) -> None:
    rc, payload = verify(campaign(tmp_path))
    assert rc == 0, payload
    assert payload["status"] == "PASS"
    assert payload["metrics"]["plain_brief_lint_ok"] is True
    assert payload["metrics"]["plain_brief_words"] <= payload["thresholds"]["max_plain_brief_words"]


def test_legacy_campaign_without_plain_brief_block_is_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root)
    proc = run_frozen_legacy_cli(root, base.VERIFY, '--campaign-root', str(root), '--json')
    rc, payload = proc.returncode, json.loads(proc.stdout)
    assert rc == 0, payload
    assert payload["metrics"]["plain_brief_status"] == "not_declared"


def test_short_plain_brief_has_no_word_floor(tmp_path: Path) -> None:
    rc, payload = verify(campaign(tmp_path))
    assert payload["metrics"]["plain_brief_words"] < 700  # the old lint floor
    assert rc == 0, payload


# --- verifier: negative tests (ISSUE-747 acceptance) ---------------------------------------------

def test_verifier_fails_plain_brief_with_source_code_citation(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("according to the [project docs](https://example.com/docs)", "according to the project docs [S12]")
    rc, payload = verify(campaign(tmp_path, body=body))
    assert rc == 3
    assert payload["status"] == "FAIL"
    assert payload["classification"] == "human_output_unreadable"
    assert any(f.startswith("plain_brief_process_vocabulary") and "[S12]" in f for f in payload["failures"]), payload["failures"]


def test_verifier_fails_plain_brief_with_lane_citation_code(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("2 GB of memory as the minimum.", "2 GB of memory as the minimum [L3.S4].")
    rc, payload = verify(campaign(tmp_path, body=body))
    assert rc == 3
    assert any("[L3.S4]" in f for f in payload["failures"]), payload["failures"]


def test_verifier_fails_plain_brief_with_process_vocabulary(tmp_path: Path) -> None:
    body = GOOD_BODY.replace(
        "Nobody outside the vendor has published a long-term review yet.",
        "The controller_inference layer and ActionRegister row AR3 cover this, see the QA receipt.",
    )
    rc, payload = verify(campaign(tmp_path, body=body))
    assert rc == 3
    failure = next(f for f in payload["failures"] if f.startswith("plain_brief_process_vocabulary"))
    for term in ("controller_inference", "ActionRegister", "AR3", "QA receipt"):
        assert term in failure, failure


def test_verifier_fails_plain_brief_with_hash_in_reader_text(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("Check the alerts for two weeks.", "Check the alerts for two weeks. Pack " + "a" * 64 + ".")
    rc, payload = verify(campaign(tmp_path, body=body))
    assert rc == 3
    assert any(f.startswith("plain_brief_process_vocabulary") for f in payload["failures"])


def test_verifier_fails_plain_brief_that_fails_lint(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("## What is still missing", "## Remaining questions")
    rc, payload = verify(campaign(tmp_path, body=body))
    assert rc == 3
    assert any(f.startswith("plain_brief_lint_failed") and "What is still missing" in f for f in payload["failures"])


def test_verifier_fails_plain_brief_with_non_c1_decision(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("No call needed.", "1. Should we adopt it?")
    rc, payload = verify(campaign(tmp_path, body=body))
    assert rc == 3
    assert any(f.startswith("plain_brief_lint_failed") and "C1" in f for f in payload["failures"])


def test_verifier_fails_plain_brief_over_word_cap(tmp_path: Path) -> None:
    padding = "\n\n".join("This short line adds words to the brief." for _ in range(300))
    body = GOOD_BODY.replace("## What to do", padding + "\n\n## What to do")
    rc, payload = verify(campaign(tmp_path, body=body))
    assert rc == 3
    assert any(f.startswith("plain_brief_words") for f in payload["failures"])


def test_verifier_fails_when_editor_review_failed(tmp_path: Path) -> None:
    rc, payload = verify(campaign(tmp_path, review={"status": "FAIL", "editor_verdict": "FAIL"}))
    assert rc == 3
    assert any(f.startswith("plain_brief_editor_review_not_pass") for f in payload["failures"])


def test_verifier_holds_without_review_record(tmp_path: Path) -> None:
    rc, payload = verify(campaign(tmp_path, write_review=False))
    assert rc == 2
    assert any(h.startswith("plain_brief_review_missing") for h in payload["holds"])


def test_verifier_holds_when_writer_reviews_itself(tmp_path: Path) -> None:
    rc, payload = verify(campaign(tmp_path, review={"editor_model": "anthropic/claude-fable-5.1"}))
    assert rc == 2
    assert "plain_brief_review_not_independent" in payload["holds"]


def test_verifier_holds_for_distinct_models_from_same_developer(tmp_path: Path) -> None:
    rc, payload = verify(campaign(tmp_path, review={"editor_model": "anthropic/claude-opus-5.5"}))
    assert rc == 2
    assert "plain_brief_review_not_independent" in payload["holds"]


def test_verifier_holds_when_full_pack_changes_after_brief(tmp_path: Path) -> None:
    root = campaign(tmp_path)
    pack = root / "internal" / "full-research-pack.md"
    pack.write_text(pack.read_text() + "\nLate edit after the brief was written.\n", encoding="utf-8")
    rc, payload = verify(root)
    assert rc == 2
    assert any(h.startswith("plain_brief_stale_full_pack") for h in payload["holds"])


def test_verifier_holds_when_brief_edited_after_review(tmp_path: Path) -> None:
    root = campaign(tmp_path)
    brief = root / PLAIN_REL
    brief.write_text(brief.read_text().replace("Try it on one project first.", "Try it on one small project first."), encoding="utf-8")
    rc, payload = verify(root)
    assert rc == 2
    assert any(h.startswith("plain_brief_review_stale") for h in payload["holds"])


def test_verifier_holds_when_plain_brief_missing_from_export_allowlist(tmp_path: Path) -> None:
    root = campaign(tmp_path)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"]["allowlist"] = manifest["deliverable_exports"]["allowlist"][:2]
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    rc, payload = verify(root)
    assert rc == 2
    assert "deliverable_export_allowlist_invalid" in payload["holds"]


def test_fresh_scaffold_requires_plain_brief(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    base.run([sys.executable, str(base.INIT), "--target", "Example Product", "--target-type", "product", "--root", str(root)], check=True)
    rc, payload = verify(root)
    assert rc == 2
    assert any(h.startswith("plain_brief_missing") for h in payload["holds"])
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["controller_state"]["phase_order"][-2:] == ["plain_brief", "export_closeout"]


# --- exporter -----------------------------------------------------------------------------------

def _export_campaign(tmp_path: Path, **kwargs) -> tuple[Path, Path]:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    base.prepare_test_export(export)
    base.write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": ".", "status": "staged_not_copied", "exports": []})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    add_plain_brief(root, **kwargs)
    return root, export


def test_exporter_copies_plain_brief_with_the_two_internal_outputs(tmp_path: Path) -> None:
    root, export = _export_campaign(tmp_path)
    proc = run_synthetic_export(root, export)
    assert proc.returncode == 0, proc.stdout
    payload = json.loads(proc.stdout)
    assert [item["staged_path"] for item in payload["exports"]] == [
        "internal/full-research-pack.md",
        "internal/executive-brief.md",
        PLAIN_REL,
    ]
    dest = export / "plain-brief.md"
    assert dest.read_bytes() == (root / PLAIN_REL).read_bytes()
    rc, verdict = verify(root)
    assert rc == 0, verdict


def test_exporter_refuses_plain_brief_with_process_vocabulary(tmp_path: Path) -> None:
    body = GOOD_BODY.replace("Nobody outside the vendor", "The lane outputs [L2.S1] show nobody outside the vendor")
    root, export = _export_campaign(tmp_path, body=body)
    proc = run_synthetic_export(root, export)
    assert proc.returncode == 3
    assert "plain brief not ready for export" in proc.stdout
    assert not (export / "full-research-pack.md").exists()


def test_exporter_refuses_stale_plain_brief_review(tmp_path: Path) -> None:
    root, export = _export_campaign(tmp_path, review={"full_pack_sha256": "0" * 64})
    proc = run_synthetic_export(root, export, "--dry-run")
    assert proc.returncode == 3
    assert "full_pack_sha256 does not bind current reader bytes" in proc.stdout


# --- generator and controller -------------------------------------------------------------------

def test_campaign_mode_writes_brief_and_review_that_verifier_accepts(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root, deliverable_export_enabled=False)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest.update({"target": "Example Product", "decision_question": "Should the owner adopt Example Product?"})
    manifest["plain_brief"] = {"required": True, "path": PLAIN_REL, "review": REVIEW_REL, "status": "not_started"}
    manifest["deliverable_exports"]["allowlist"].append(PLAIN_REL)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    module = load_plain()
    calls: list[str] = []

    def respond(role, system, user):
        calls.append(role)
        if role == module.EDITOR:
            return json.dumps(editor_pass())
        assert "Should the owner adopt Example Product?" in user
        return GOOD_BODY

    with offline_plain_client(module, respond):
        record = module.campaign_brief(root)

    assert calls == [module.WRITER, module.EDITOR]
    assert record["status"] == "PASS"
    assert record["writer_model"] != record["editor_model"]
    assert record["writer_model"].split("/")[0] != record["editor_model"].split("/")[0]
    assert record["artifact_writers"][0]["served_model"] == record["writer_model"]
    attempts = record["provenance"]["attempts"]
    assert [call["role"] for call in attempts] == ["writer", "brief_reviewer"]
    assert attempts[-1]["identity_verified"] is True
    assert attempts[-1]["served_model"] == record["editor_model"]
    assert record["acceptance_eligible"] is True
    assert record["brief_sha256"] == sha(root / PLAIN_REL)
    assert record["full_pack_sha256"] == sha(root / "internal" / "full-research-pack.md")
    assert module.check_file(root / PLAIN_REL)["ok"] is True
    assert json.loads((root / "manifest.json").read_text())["plain_brief"]["status"] == "reviewed_pass"
    rc, payload = verify(root)
    assert rc == 0, payload

    controller = base.load_controller_module()
    controller.initialize_campaign_state(root, json.loads((root / "manifest.json").read_text()))
    assert controller.record_plain_brief(root)["status"] == "complete"
    (root / PLAIN_REL).write_text(plain_file(GOOD_BODY + "\nSee [S3].\n"), encoding="utf-8")
    blocked = controller.record_plain_brief(root)
    assert blocked["status"] == "blocked"
    assert any("plain_brief_process_vocabulary" in p for p in blocked["problems"])


def test_campaign_mode_adds_related_notes_to_frontmatter_and_footer(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root, deliverable_export_enabled=False)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest.update({"target": "Example Product", "decision_question": "Should the owner adopt Example Product?"})
    manifest["plain_brief"] = {"required": True, "path": PLAIN_REL, "review": REVIEW_REL, "status": "not_started",
                               "related_notes": ["sibling-plain-brief", {"note": "combined-summary", "label": "combined summary"}]}
    manifest["deliverable_exports"]["allowlist"].append(PLAIN_REL)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    module = load_plain()

    def respond(role, system, user):
        return json.dumps(editor_pass()) if role == module.EDITOR else GOOD_BODY

    with offline_plain_client(module, respond):
        record = module.campaign_brief(root)
    text = (root / PLAIN_REL).read_text()
    assert '  - "[[sibling-plain-brief]]"' in text and '  - "[[combined-summary]]"' in text
    assert "Related: [[sibling-plain-brief]], [[combined-summary|combined summary]]." in text
    assert record["status"] == "PASS" and record["brief_sha256"] == sha(root / PLAIN_REL)
    assert module.check_file(root / PLAIN_REL)["ok"] is True
    rc, payload = verify(root)
    assert rc == 0, payload


def test_plain_brief_module_imports_without_secret_file() -> None:
    with patch("builtins.open", side_effect=FileNotFoundError("no env")):
        module = load_plain()
    assert module.lint(GOOD_BODY)["ok"] is True
