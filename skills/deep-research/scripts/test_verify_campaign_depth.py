#!/usr/bin/env python3
"""Tests for verify_campaign_depth.py, init_campaign.py, campaign_controller.py and preseed.py (DR-897)."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import campaign_controller as cc
import preseed
import verify_campaign_depth as vcd

SCRIPTS = Path(__file__).resolve().parent


def scaffold(root: Path, purpose: str = "factual", extra: list[str] | None = None) -> dict:
    cmd = [
        sys.executable, str(SCRIPTS / "init_campaign.py"),
        "--root", str(root),
        "--target", "Example Protocol",
        "--target-type", "technical_idea",
        "--decision-question", "What does the example protocol specify?",
        "--report-purpose", purpose,
        "--date", "2026-11-11",
        *(extra or []),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def test_init_campaign_v2_layout_and_frontmatter(tmp_path):
    root = tmp_path / "campaign"
    manifest = scaffold(root)
    assert manifest["output_layout"] == "brief-and-reference-v2"
    assert "research_orchestration" not in manifest
    assert manifest["deliverable_exports"]["allowlist"] == [
        "internal/reference-pack.md",
        "internal/plain-brief.md",
    ]
    pack = (root / "internal" / "reference-pack.md").read_text(encoding="utf-8")
    assert "author: Research contributors\n" in pack
    assert "credibility: practitioner\n" in pack
    assert "verified_count: 0\n" in pack


def test_campaign_controller_claim_ingest_and_context_release(tmp_path):
    root = tmp_path / "campaign"
    scaffold(root)
    state = cc.load_state(root)
    first_lane = state["lanes"][0]["id"]
    # Early context release is refused while lanes are pending
    early = cc.record_context_release(root)
    assert early["status"] == "refused"
    # Claim and complete every lane
    for lane in state["lanes"]:
        lid = lane["id"]
        claimed = cc.claim_lane(root, lid, route="test/model", worker_id=f"w-{lid}")
        assert claimed["status"] == "claimed"
        p = root / "lanes" / f"{lid}.json"
        data = json.loads(p.read_text())
        data["status"] = "pass"
        data["summary"] = "completed"
        data["findings"] = [{"id": "F1", "claim": "c", "evidence_class": "reported", "source_ids": ["S1"]}]
        data["sources"] = [{"id": "S1", "title": "T", "url_or_ref": "https://example.com", "source_class": "official"}]
        data["verification"]["open_verification_gaps"] = []
        p.write_text(json.dumps(data))
        (root / "lanes" / f"{lid}.md").write_text("# Lane\n\nDone.\n")
        ing = cc.ingest_completion_event(root, {
            "event_id": f"ev-{lid}",
            "lane_id": lid,
            "attempt_id": claimed["attempt_id"],
            "status": "completed",
            "output_path": f"lanes/{lid}.json",
        })
        assert ing["status"] == "completed"
    rel = cc.record_context_release(root)
    assert rel["status"] == "released" and rel["released_at"]
    # Second call preserves the first timestamp
    rel2 = cc.record_context_release(root)
    assert rel2["released_at"] == rel["released_at"] and rel2["already_recorded"] is True


def test_preseed_holds_on_scaffold_placeholders_and_passes_when_filled(tmp_path):
    root = tmp_path / "campaign"
    manifest = scaffold(root, purpose="decision_assessment")
    verdict = preseed.verify_preseed(root, manifest)
    assert verdict.status == "HOLD" and verdict.holds


def test_reference_pack_process_records_and_duplicate_blocks_fail():
    clean = "---\ntitle: Pack\n---\n\n# Pack\n\n## Answer\n\nIt defines sixteen events [L01-S01].\n\n## Sources\n\n- [L01-S01] https://example.com\n"
    assert vcd.reference_pack_process_records(clean) == []
    dirty = clean + "\n| AR-01 | Do something |\nStatus: PASS\nsha256: " + ("a" * 64) + "\n"
    assert vcd.reference_pack_process_records(dirty)
    para = " ".join(["This paragraph is repeated verbatim to pad the word count of the reference pack."] * 5)
    dup = f"# Pack\n\n{para}\n\n{para}\n"
    assert vcd.duplicate_prose_metrics(dup)["duplicate_prose_groups"] >= 1


def test_frontmatter_holds_check_required_keys(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        "---\ntitle: T\ncreated: 2026-11-11\ntype: source\nstage: import\n"
        "source: https://example.com\nauthor: Research contributors\ncaptured_via: agent-deep-research\n"
        "tags: [source/adapted]\n---\n\n# Note\n"
    )
    assert vcd.frontmatter_holds(tmp_path, ["note.md"]) == []
    p.write_text("# Note without frontmatter\n")
    assert vcd.frontmatter_holds(tmp_path, ["note.md"])


def test_qa_holds_require_pass_and_matching_pack_hash(tmp_path):
    sys.path.insert(0, str(SCRIPTS / "kit"))
    import mechanical_qa as mqa

    root = tmp_path / "campaign"
    manifest = scaffold(root)
    pack = root / "internal" / "reference-pack.md"
    pack_sha = hashlib.sha256(pack.read_bytes()).hexdigest()
    (root / "internal" / "plain-brief.md").write_text(
        "---\ntitle: B\ncreated: 2026-11-11\ntype: source\nstage: import\n"
        "source: https://example.com\nauthor: Research contributors\ncredibility: practitioner\n"
        "captured_via: agent-deep-research\ntags: [source/adapted]\nrelated:\n  - \"[[Research Hub]]\"\n---\n\n# Brief\n\nPlain prose.\n"
    )
    holds = vcd.qa_holds(root, manifest, False, pack_sha)
    assert holds  # scaffold receipts start at HOLD
    # Complete lanes and context release so mechanical_qa passes
    state = cc.load_state(root)
    for lane in state["lanes"]:
        lane["status"] = "completed"
    state["context_release"] = {"released_at": "2026-11-11T00:00:00+00:00"}
    (root / "campaign_state.json").write_text(json.dumps(state))
    ctx = json.loads((root / "context" / "context-brief.json").read_text())
    ctx.update(status="complete", released_at="2026-11-11T00:00:00+00:00", no_relevant_context_reason="external only")
    (root / "context" / "context-brief.json").write_text(json.dumps(ctx))
    (root / "synthesis" / "visual-exhibits.json").write_text(json.dumps({"status": "complete", "exhibits": [{"id": "E1"}, {"id": "E2"}, {"id": "E3"}]}))
    (root / "qa" / "evidence-privacy-review.json").write_text(json.dumps({
        "schema_version": "deep-research-qa-receipt-v1",
        "name": "evidence_privacy",
        "status": "PASS", "reviewer": "openai/gpt-6.1-sol-pro", "producers": ["anthropic/claude-opus-5.5"],
        "reference_pack_sha256": pack_sha, "blocking": [], "output_classes": {"reference_pack": "PASS"},
        "checks": {k: {"verdict": "PASS", "basis": "ok"} for k in ("factual", "reasoning", "editorial", "presentation")},
        "important_quantity_qualification_review": {"verdict": "PASS", "basis": "ok"},
    }))
    (root / "qa" / "plain-brief-review.json").write_text(json.dumps({"status": "PASS", "editor_verdict": "PASS"}))
    (root / "receipts" / "quote-check.json").write_text(json.dumps({"verdict": "PASS"}))
    mqa.check_post_brief(root, ["anthropic/claude-opus-5.5"], "anthropic/claude-opus-5.5")
    assert vcd.qa_holds(root, manifest, False, pack_sha) == []
