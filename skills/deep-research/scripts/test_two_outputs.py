#!/usr/bin/env python3
"""new campaigns produce two outputs (plain brief + reference pack); legacy campaigns are unchanged.

Layout v2 is selected by ``manifest.output_layout == "brief-and-reference-v2"``. A missing or other
value is the legacy four-output layout, which keeps every earlier check. The legacy helpers in
``test_deep_research_campaign_tools`` and ``test_plain_brief_campaign_gate`` are reused as the
legacy control.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import test_deep_research_campaign_tools as base
import test_plain_brief_campaign_gate as gate
from reader_test_support import editor_pass, offline_plain_client, refresh_fixture_acceptance_bindings, semantic_review_fields
from publication_test_support import add_generated_provenance, bind_fixture_review_packet, run_synthetic_export

SCRIPT_DIR = Path(__file__).resolve().parent
LAYOUT = "brief-and-reference-v2"
REF_REL = "internal/reference-pack.md"
PLAIN_REL = "internal/plain-brief.md"
REVIEW_REL = "qa/plain-brief-review.json"
ACTION_REL = "synthesis/action-register.md"
REF_NOTE = "source-research-example-product-reference-pack-2026-10-20"
PLAIN_NOTE = "source-research-example-product-plain-brief-2026-10-20"
LEGACY_ONLY_FILES = [
    "external/original-full-research-pack.md",
    "internal/executive-brief.md",
    "internal/full-research-pack.md",
    "synthesis/board-brief.md",
    "synthesis/full-pack.md",
]
DROPPED_HOLD_PREFIXES = (
    "external_output_missing", "external_pack_hash", "external_pack_frozen_hash_missing", "external_privacy_scan",
    "external_evidence_provenance_label_missing", "internal_full_pack_layers_missing",
    "internal_synthesis_provenance_labels_missing", "internal_reconciliation_table_missing", "board_brief_",
    "external_pack_duplicate",
)


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- v2 fixture ------------------------------------------------------------------------------------

REFERENCE_FRONT = (
    "---\ntitle: Example Product, reference pack\ncreated: 2026-10-20\ntype: source\nstage: import\n"
    "source: https://example.com\nauthor: Research agent / Hermes internal synthesis\ncaptured_via: agent-deep-research\n"
    "tags: [source/adapted]\nrelated:\n  - \"[[" + PLAIN_NOTE + "]]\"\n---\n"
)


def reference_text(extra: str = "") -> str:
    text = REFERENCE_FRONT + "# Example Product reference pack\n\n"
    text += "## What the sources say\n" + "word [S1] " * 3000 + "\n\n"
    text += "## Where the sources disagree\n" + "word [S2] " * 1500 + "\n\n"
    text += "## Source table\n| Claim area | Evidence | Confidence |\n|---|---|---|\n| Fit | [S2] | medium |\n\n"
    text += "## Visual decision surface\n### Exhibit E1 — System map\n### Exhibit E2 — Option matrix\n### Exhibit E3 — Evidence heatmap\n\n"
    text += "## Gaps\nNothing public covers long-term use.\n\n"
    text += "## Detail\n" + "detail [S3] " * 1500 + "\n"
    return text + (extra + "\n" if extra else "")


def action_register_text(rows: int = 5) -> str:
    return "# Action Register\n\n| ID | Action | Evidence |\n|---|---|---|\n" + "".join(f"| AR{i} | candidate {i} | evidence |\n" for i in range(1, rows + 1))


def plain_file_v2(body: str = gate.GOOD_BODY) -> str:
    return (
        "---\ntitle: Example Product — whether to adopt it now\ncreated: 2026-10-20\ntype: source\nstage: import\n"
        "source: plain brief of the reference pack\nauthor: Research agent / Hermes internal synthesis\n"
        "captured_via: agent-deep-research\ntags: [source/adapted]\nrelated:\n"
        f"  - \"[[{REF_NOTE}]]\"\nverified_count: 0\n---\n\n"
        + body.strip()
        + f"\n\n---\nDetail and sources: [[{REF_NOTE}|reference pack]].\n"
    )


def write_v2_pass_fixture(
    root: Path,
    *,
    with_visuals: bool = True,
    target_type: str = "idea",
    deliverable_export_enabled: bool = True,
    exported: bool = True,
    export_root: Path | None = None,
    receipt_overrides: dict[str, dict] | None = None,
    include_preseed: bool = True,
    with_plain_brief: bool = True,
    reference_extra: str = "",
    action_rows: int = 5,
    plain_body: str = gate.GOOD_BODY,
    review: dict | None = None,
    report_purpose: str = "factual",
) -> None:
    """A complete layout-v2 campaign: reference pack, plain brief, sidecars, receipts bound to the reference pack."""
    for d in ["lanes", "workers", "synthesis", "history", "internal", "context", "qa/packets", "export", "receipts"]:
        (root / d).mkdir(parents=True)
    lanes = [{"id": f"lane-{i}", "name": f"Lane {i}"} for i in range(6)]
    for lane in lanes:
        (root / "lanes" / f"{lane['id']}.md").write_text("# lane\nstatus: PASS\n", encoding="utf-8")
        (root / "lanes" / f"{lane['id']}.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
        (root / "workers" / f"{lane['id']}-packet.md").write_text("# packet\nresearch lane evidence only\n", encoding="utf-8")
    ref_text = reference_text(reference_extra)
    ref = root / REF_REL
    ref.write_text(ref_text, encoding="utf-8")
    ref_hash = sha_text(ref_text)
    (root / ACTION_REL).write_text(action_register_text(action_rows), encoding="utf-8")
    context = {
        "schema_version": "deep-research-context-brief-v1",
        "status": "complete",
        "items": [{"source_path": "/home/example/knowledge-base/Knowledge.md", "freshness": "2026-08-01", "authority": "owner-approved", "privacy_classification": "private", "exclusions": ["do not export externally"]}],
    }
    (root / "context" / "context-brief.md").write_text("# Context Brief\nPrivate the owner context.\n", encoding="utf-8")
    (root / "context" / "context-brief.json").write_text(json.dumps(context), encoding="utf-8")
    (root / "history" / "relevant-history.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    (root / "citations.json").write_text(json.dumps({"sources": [{"id": f"S{i}"} for i in range(25)]}), encoding="utf-8")
    if with_visuals:
        (root / "synthesis" / "visual-exhibits.json").write_text(json.dumps({
            "schema_version": "deep-research-visual-exhibits-v2",
            "status": "complete",
            "topic_requires_diagram": target_type in {"product", "company", "market", "category", "repo", "tool", "architecture", "internal-system", "workflow", "venture", "operating-model"},
            "exhibits": [
                {"id": "E1", "title": "System map", "type": "diagram", "output_class": "reference_pack", "appears_in": ["reference_pack"], "anchor": "Exhibit E1 — System map", "evidence_refs": ["S1"], "source_data_refs": ["S1"], "decision_use": "Shows workflow gates.", "render_method": "technical-diagrams", "source_artifact": "synthesis/diagrams/e1.json", "validation_receipt": "qa/diagram-e1-validation.json", "reviewer": "visual-qa"},
                {"id": "E2", "title": "Option matrix", "type": "matrix", "output_class": "reference_pack", "appears_in": ["reference_pack"], "anchor": "Exhibit E2 — Option matrix", "evidence_refs": ["S2"], "source_data_refs": ["S2"], "decision_use": "Compares build and watch options.", "reviewer": "visual-qa"},
                {"id": "E3", "title": "Evidence heatmap", "type": "heatmap", "output_class": "reference_pack", "appears_in": ["reference_pack"], "anchor": "Exhibit E3 — Evidence heatmap", "evidence_refs": ["S3"], "source_data_refs": ["S3"], "decision_use": "Separates strong from weak claims.", "reviewer": "visual-qa"},
            ],
        }), encoding="utf-8")
        (root / "qa" / "diagram-e1-validation.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    receipts = {
        "context-handoff-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "context-qa", "producers": ["taxonomy"], "checks": {"isolation": "PASS", "privacy_classification": "PASS"}},
        "evidence-privacy-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "evidence-qa", "producers": ["research-worker", "controller"], "controller": "controller", "reference_pack_sha256": ref_hash, "privacy_scan_findings": [], "blocking": [], **semantic_review_fields()},
        "visual-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "visual-qa", "producers": ["controller"], "output_classes": {"reference_pack": "PASS", "plain_brief": "PASS"}},
        "taxonomy-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "taxonomy-qa", "producers": ["controller"], "reviewed_paths": [REF_REL, PLAIN_REL], "live_taxonomy_hash": "abc123", "routing_invariant": "PASS", "tags": {"allowed": ["source/adapted"], "invalid": []}, "internal_links": {"status": "PASS"}},
        "librarian-repair-receipt.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "librarian-packaging", "producers": ["controller"], "changed_paths": []},
        "final-assurance.json": {"schema_version": "deep-research-final-assurance-v1", "status": "PASS", "reviewer": "research-final", "producers": ["controller"], "reference_pack_sha256": ref_hash},
    }
    if not deliverable_export_enabled:
        receipts["taxonomy-review.json"] = {"schema_version": "deep-research-qa-receipt-v1", "status": "N/A", "reason": "deliverable export explicitly suppressed", "reviewer": "taxonomy-qa", "producers": ["controller"]}
        receipts["librarian-repair-receipt.json"] = {"schema_version": "deep-research-qa-receipt-v1", "status": "N/A", "reason": "deliverable export explicitly suppressed", "reviewer": "librarian-packaging", "producers": ["controller"]}
    for name, override in (receipt_overrides or {}).items():
        receipts[name].update(override)
    for name, data in receipts.items():
        (root / "qa" / name).write_text(json.dumps(data), encoding="utf-8")
    export = export_root or root.parent / f"{root.name}-export-readback"
    auto = export
    brief_path = root / PLAIN_REL
    if with_plain_brief:
        brief_path.write_text(plain_file_v2(plain_body), encoding="utf-8")
        record = {
            "schema_version": "deep-research-plain-brief-review-v1", "status": "PASS", "editor_verdict": "PASS",
            "editor_blocking": [], **semantic_review_fields(),
            "writer_model": "anthropic/claude-opus-5.5", "editor_model": "openai/gpt-6.1-sol-pro",
            "brief_path": PLAIN_REL, "brief_sha256": sha(brief_path), "full_pack_path": REF_REL, "full_pack_sha256": ref_hash,
        }
        add_generated_provenance(root, record)
        (root / REVIEW_REL).write_text(json.dumps(record), encoding="utf-8")
    exports = []
    if deliverable_export_enabled and exported:
        auto.mkdir(parents=True)
        for rel, note in ((REF_REL, REF_NOTE), (PLAIN_REL, PLAIN_NOTE)):
            dest = auto / f"{note}.md"
            dest.write_bytes((root / rel).read_bytes())
            digest = sha(root / rel)
            exports.append({"staged_path": rel, "export_path": str(dest), "sha256": digest, "readback_sha256": digest})
    manifest_payload = {
        "report_purpose": report_purpose,
        "root": str(root),
        "output_layout": LAYOUT,
        "target": "Example Product",
        "decision_question": "Should the owner adopt Example Product?",
        "target_type": target_type,
        "lanes": lanes,
        "internal_outputs": [{"path": REF_REL}],
        "plain_brief": {"required": True, "path": PLAIN_REL, "review": REVIEW_REL, "status": "reviewed_pass" if with_plain_brief else "not_started"},
        "context": {"markdown": "context/context-brief.md", "json": "context/context-brief.json"},
        "qa": {"receipts": {"context_handoff": "qa/context-handoff-review.json", "evidence_privacy": "qa/evidence-privacy-review.json", "visual": "qa/visual-review.json", "taxonomy_review": "qa/taxonomy-review.json", "librarian_repair": "qa/librarian-repair-receipt.json", "final_assurance": "qa/final-assurance.json"}},
        "deliverable_exports": {
            "enabled": deliverable_export_enabled,
            "status": ("copied_to_export" if exported else "staged_not_copied") if deliverable_export_enabled else "suppressed_by_no_deliverable_export",
            "export_root": str(export), "intended_directory": ".",
            "allowlist": [REF_REL, PLAIN_REL],
            "reference_pack_staging": REF_REL, "plain_brief_staging": PLAIN_REL,
            "reference_pack_intended_export_path": str(auto / f"{REF_NOTE}.md"),
            "plain_brief_intended_export_path": str(auto / f"{PLAIN_NOTE}.md"),
            "exports": exports,
        },
        "visual_exhibits": {"path": "synthesis/visual-exhibits.json"},
    }
    if include_preseed:
        universe, charter = base.passing_preseed_payloads()
        from preseed import scaffold_preseed

        manifest_payload["pre_seed"] = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
        (root / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
        base.attach_preseed(root, universe, charter)
    else:
        (root / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")

    if with_plain_brief:
        from test_acceptance_repairs import seal_test_acceptance
        seal_test_acceptance(root)
        if review:
            record = json.loads((root / REVIEW_REL).read_text())
            record.update(review)
            (root / REVIEW_REL).write_text(json.dumps(record), encoding="utf-8")
            bind_fixture_review_packet(root)


def read_manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text())


def write_manifest(root: Path, manifest: dict) -> None:
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def v2_campaign(tmp_path: Path, **kwargs) -> Path:
    root = tmp_path / "campaign"
    write_v2_pass_fixture(root, **kwargs)
    return root


def init_campaign(tmp_path: Path, *extra: str) -> Path:
    root = tmp_path / "scaffold"
    base.run([sys.executable, str(base.INIT), "--target", "Example Product", "--target-type", "product", "--root", str(root), *extra], check=True)
    return root


# --- scaffold ----------------------------------------------------------------------------------------

def test_init_default_scaffold_is_v2(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    manifest = read_manifest(root)
    assert manifest["output_layout"] == LAYOUT
    for rel in [REF_REL, ACTION_REL, "context/context-brief.md", "context/context-brief.json", "qa/packets/evidence-privacy.md",
                "qa/packets/visual.md", "qa/packets/taxonomy.md", "qa/packets/librarian-repair.md", "qa/context-handoff-review.json",
                "qa/evidence-privacy-review.json", "qa/visual-review.json", "qa/taxonomy-review.json", "qa/librarian-repair-receipt.json",
                "qa/final-assurance.json", "synthesis/visual-exhibits.json", "citations.json", "campaign_state.json"]:
        assert (root / rel).is_file(), rel
    for rel in LEGACY_ONLY_FILES:
        assert not (root / rel).exists(), rel
    assert not (root / "external").exists()
    assert len(list((root / "workers").glob("*-packet.md"))) == len(manifest["lanes"]) >= 6
    assert "external_output" not in manifest
    assert [item["path"] for item in manifest["internal_outputs"]] == [REF_REL]
    assert manifest["plain_brief"]["required"] is True
    assert manifest["plain_brief"]["path"] == PLAIN_REL
    assert manifest["plain_brief"]["review"] == REVIEW_REL
    ve = manifest["deliverable_exports"]
    assert ve["allowlist"] == [REF_REL, PLAIN_REL]
    assert ve["reference_pack_staging"] == REF_REL and ve["plain_brief_staging"] == PLAIN_REL
    assert "-reference-pack-" in ve["reference_pack_intended_export_path"] and ve["reference_pack_intended_export_path"].endswith(".md")
    assert ve["reference_pack_intended_export_path"].rsplit("/", 1)[-1].startswith("source-research-example-product-")
    assert "board_brief_staging" not in ve and "full_pack_staging" not in ve
    assert not any("executive" in item or item.startswith("external/") for item in ve["allowlist"])
    assert ve["denylist"] and "external/original-full-research-pack.md" not in ve["denylist"]
    assert manifest["synthesis_sidecars"]["action_register"] == ACTION_REL
    assert manifest["controller_state"]["phase_order"] == ["intake", "lanes", "internal_synthesis", "qa", "plain_brief", "export_closeout"]
    assert manifest["visual_exhibits"]["board_brief_min_exhibits"] == 0
    assert manifest["context"]["release"]["rule"] == "read_only_after_all_research_lanes_finish"
    assert manifest["context"]["release"]["released_at"] is None
    assert json.loads((root / "context" / "context-brief.json").read_text())["release_gate"] == "after_all_research_lanes_finished"


def test_init_v2_receipts_bind_reference_pack_not_external_pack(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    ref_hash = sha(root / REF_REL)
    for rel in ("qa/evidence-privacy-review.json", "qa/final-assurance.json"):
        data = json.loads((root / rel).read_text())
        assert data["reference_pack_sha256"] == ref_hash
        assert "external_pack_sha256" not in data
    exhibits = json.loads((root / "synthesis" / "visual-exhibits.json").read_text())
    assert exhibits["thresholds"]["board_brief_min_exhibits"] == 0
    assert all(ex["appears_in"] == ["reference_pack"] for ex in exhibits["exhibits"])
    text = (root / ACTION_REL).read_text()
    assert text.startswith("# Action Register") and "| ID |" in text


def test_init_v2_scaffold_names_no_removed_output(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    offenders = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "campaign_state.lock":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for needle in ("executive-brief", "original-full-research-pack", "board-brief.md", "external_freeze", "full-research-pack"):
            if needle in text:
                offenders.append((str(path.relative_to(root)), needle))
    assert offenders == []


def test_init_v2_reference_pack_has_no_layer_headings_or_process_records(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    text = (root / REF_REL).read_text()
    for needle in ("What external evidence says", "internal_context", "controller_inference", "external_evidence", "Internal reconciliation table", "Action Register", "ActionRegister"):
        assert needle not in text, needle
    assert text.startswith("---\n") and "related:" in text


def test_init_legacy_flag_keeps_four_output_scaffold(tmp_path: Path) -> None:
    root = init_campaign(tmp_path, "--output-layout", "legacy")
    manifest = read_manifest(root)
    assert "output_layout" not in manifest or manifest["output_layout"] == "legacy"
    for rel in LEGACY_ONLY_FILES[:3] + ["synthesis/board-brief.md", "synthesis/full-pack.md"]:
        assert (root / rel).is_file(), rel
    assert [item["path"] for item in manifest["internal_outputs"]] == ["internal/full-research-pack.md", "internal/executive-brief.md", PLAIN_REL]
    assert "external_freeze" in manifest["controller_state"]["phase_order"]
    assert not (root / REF_REL).exists()
    assert not (root / ACTION_REL).exists()


def test_fresh_v2_scaffold_holds_without_dropped_checks(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    rc, payload = gate.verify(root)
    assert rc == 2 and payload["status"] == "HOLD"
    assert any(h.startswith("plain_brief_missing") for h in payload["holds"])
    assert not any(h.startswith("internal_full_pack_words") for h in payload["holds"])
    stale = [h for h in payload["holds"] if h.startswith(DROPPED_HOLD_PREFIXES)]
    assert stale == [], stale
    assert payload["metrics"]["output_layout"] == LAYOUT


def test_init_v2_alias_and_no_deliverable_export(tmp_path: Path) -> None:
    root = init_campaign(tmp_path, "--output-layout", "v2", "--no-deliverable-export")
    manifest = read_manifest(root)
    assert manifest["output_layout"] == LAYOUT
    ve = manifest["deliverable_exports"]
    assert ve["enabled"] is False and ve["allowlist"] == [REF_REL, PLAIN_REL]
    rc, payload = gate.verify(root)
    assert rc == 2
    assert not any(h.startswith(("deliverable_export_allowlist_invalid", "deliverable_export_staging_invalid")) for h in payload["holds"])


def test_init_v2_worker_packet_has_context_release_rule(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    packet = next((root / "workers").glob("*-packet.md")).read_text()
    assert "only after all research lanes finish" in packet
    assert "Context Brief material" not in packet and "internal source paths" in packet


# --- verifier: v2 pass and the dropped checks ------------------------------------------------------------

def test_v2_pass_fixture_passes_without_external_pack_or_executive_brief(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    for rel in LEGACY_ONLY_FILES:
        assert not (root / rel).exists()
    rc, payload = gate.verify(root)
    assert rc == 0, payload
    assert payload["status"] == "PASS" and payload["classification"] == "true_deep_research"
    metrics = payload["metrics"]
    assert metrics["output_layout"] == LAYOUT
    assert metrics["internal_full_pack_exists"] is True
    assert metrics["internal_full_pack_words"] >= 8000
    assert metrics["action_register_rows"] == 5
    assert metrics["full_pack_exhibit_count"] == 3
    assert metrics["diagram_count"] == 1
    assert metrics["plain_brief_lint_ok"] is True
    assert "external_pack_exists" not in metrics and "board_brief_words" not in metrics


def test_v2_pass_without_deliverable_export(tmp_path: Path) -> None:
    rc, payload = gate.verify(v2_campaign(tmp_path, deliverable_export_enabled=False))
    assert rc == 0, payload


def test_v2_missing_reference_pack_holds(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    (root / REF_REL).unlink()
    rc, payload = gate.verify(root)
    assert rc == 2 and payload["status"] == "HOLD"
    assert any(h.startswith("internal_output_missing") and REF_REL in h for h in payload["holds"])


def test_v2_action_register_comes_from_the_sidecar(tmp_path: Path) -> None:
    # Decision assessments retain the action-count gate and assessment sections.
    body = gate.GOOD_BODY + "\n## Technical judgement\nThe trial is a bounded test, not an adoption commitment.\n\n## Alternatives and trade-offs\nWaiting avoids setup work but leaves alerts untested.\n"
    root = v2_campaign(tmp_path, action_rows=2, report_purpose="decision_assessment", plain_body=body)
    rc, payload = gate.verify(root)
    assert rc == 2
    assert any(h.startswith("action_register_rows 2 < 5") for h in payload["holds"])
    (root / ACTION_REL).unlink()
    rc, payload = gate.verify(root)
    assert rc == 2
    assert any(h.startswith("action_register_rows 0 < 5") for h in payload["holds"])


def test_v2_factual_campaign_does_not_require_action_rows(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, action_rows=0)
    rc, payload = gate.verify(root)
    assert rc == 0, payload
    assert payload["metrics"]["action_register_rows"] == 0
    assert not any(h.startswith("action_register_rows") for h in payload["holds"])


@pytest.mark.parametrize("extra", [
    "| AR9 | queue the pilot | open |",
    "## Action Register\nNext moves.",
    "Hash " + "a1b2c3d4" * 8 + " was recorded.",
    "The call was controller_inference only.",
    "This is internal_context from the export.",
    "Tagged external_evidence.",
    "QA receipts show a pass.",
    "See the Context Brief for detail.",
])
def test_v2_reference_pack_process_records_fail(tmp_path: Path, extra: str) -> None:
    root = v2_campaign(tmp_path, reference_extra=extra)
    rc, payload = gate.verify(root)
    assert rc == 3, payload
    assert payload["status"] == "FAIL"
    assert payload["classification"] == "reference_pack_process_records"
    assert any(f.startswith("reference_pack_process_records") for f in payload["failures"])


def test_v2_reference_pack_allows_citation_codes_and_url_hashes(tmp_path: Path) -> None:
    extra = "The vendor says so [S12] and [L3.S4]. Commit https://github.com/example/repo/commit/" + "a1b2c3d4" * 5 + " fixed it."
    rc, payload = gate.verify(v2_campaign(tmp_path, reference_extra=extra))
    assert rc == 0, payload


def test_v2_receipts_bind_the_current_reference_pack(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    receipt = root / "qa" / "evidence-privacy-review.json"
    data = json.loads(receipt.read_text())
    data["reference_pack_sha256"] = "0" * 64
    receipt.write_text(json.dumps(data), encoding="utf-8")
    rc, payload = gate.verify(root)
    assert rc == 2
    assert "reference_pack_hash_mismatch_evidence_privacy" in payload["holds"]
    data.pop("reference_pack_sha256")
    receipt.write_text(json.dumps(data), encoding="utf-8")
    rc, payload = gate.verify(root)
    assert "reference_pack_hash_missing_evidence_privacy" in payload["holds"]


def test_v2_final_assurance_binds_the_reference_pack(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, receipt_overrides={"final-assurance.json": {"reference_pack_sha256": "f" * 64}})
    rc, payload = gate.verify(root)
    assert rc == 2 and "reference_pack_hash_mismatch_final_assurance" in payload["holds"]


def test_v2_visual_receipt_needs_reference_pack_and_plain_brief_classes(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, receipt_overrides={"visual-review.json": {"output_classes": {"reference_pack": "PASS"}}})
    rc, payload = gate.verify(root)
    assert rc == 2
    assert "visual_qa_output_class_plain_brief_not_pass" in payload["holds"]
    assert not any("external_full_pack" in h or "internal_executive_brief" in h for h in payload["holds"])


def test_v2_visual_gate_counts_reference_pack_exhibits_and_needs_no_board_exhibits(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    ledger = json.loads((root / "synthesis" / "visual-exhibits.json").read_text())
    ledger["exhibits"] = ledger["exhibits"][:2]
    (root / "synthesis" / "visual-exhibits.json").write_text(json.dumps(ledger), encoding="utf-8")
    rc, payload = gate.verify(root)
    assert rc == 2
    assert "full_pack_exhibits 2 < 3" in payload["holds"]
    assert not any(h.startswith("board_brief_exhibits") for h in payload["holds"])


def test_v2_reference_pack_exhibit_anchor_must_be_present(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    ref = root / REF_REL
    ref.write_text(ref.read_text().replace("### Exhibit E3 — Evidence heatmap", "### Heatmap"), encoding="utf-8")
    rc, payload = gate.verify(root)
    assert rc in (2, 3)
    assert any("visual_exhibit_E3" in h for h in payload["holds"])


def test_v2_context_brief_still_required(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    (root / "context" / "context-brief.json").unlink()
    rc, payload = gate.verify(root)
    assert rc == 2 and any(h.startswith("context_brief_missing") for h in payload["holds"])


def test_v2_plain_brief_gate_is_unchanged(tmp_path: Path) -> None:
    body = gate.GOOD_BODY.replace("Nobody outside the vendor", "The lane outputs [L2.S1] show nobody outside the vendor")
    rc, payload = gate.verify(v2_campaign(tmp_path, plain_body=body))
    assert rc == 3 and payload["classification"] == "human_output_unreadable"
    assert any(f.startswith("plain_brief_process_vocabulary") for f in payload["failures"])


def test_v2_stale_plain_brief_after_reference_pack_change_holds(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, review={"full_pack_sha256": "0" * 64})
    rc, payload = gate.verify(root)
    assert rc == 2 and any(h.startswith("plain_brief_stale_full_pack") for h in payload["holds"])


def test_v2_requires_the_plain_brief_declaration(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    manifest = read_manifest(root)
    manifest.pop("plain_brief")
    write_manifest(root, manifest)
    rc, payload = gate.verify(root)
    assert rc == 2 and "plain_brief_required_in_v2_layout" in payload["holds"]


def test_v2_allowlist_with_executive_brief_or_external_pack_holds(tmp_path: Path) -> None:
    for extra in ("internal/executive-brief.md", "external/original-full-research-pack.md", "internal/full-research-pack.md"):
        root = tmp_path / extra.replace("/", "_")
        root.mkdir()
        write_v2_pass_fixture(root / "c")
        root = root / "c"
        manifest = read_manifest(root)
        manifest["deliverable_exports"]["allowlist"] = [REF_REL, extra, PLAIN_REL]
        write_manifest(root, manifest)
        rc, payload = gate.verify(root)
        assert rc == 2, payload
        assert "deliverable_export_allowlist_invalid" in payload["holds"]
        assert any(h.startswith("deliverable_export_allowlist_forbidden_output") and extra in h for h in payload["holds"]), payload["holds"]


def test_v2_export_readback_uses_reference_pack_destination_key(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    manifest = read_manifest(root)
    manifest["deliverable_exports"]["reference_pack_intended_export_path"] = str(Path(manifest["deliverable_exports"]["export_root"]) / "other-name.md")
    write_manifest(root, manifest)
    rc, payload = gate.verify(root)
    assert rc == 2 and "deliverable_export_destination_path_mismatch" in payload["holds"]


# --- forged records still fail in v2 -----------------------------------------------------------------------

def test_v2_forged_review_is_receipt_fabricated(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    (root / REVIEW_REL).write_text(json.dumps({
        "status": "PASS", "word_count": 576, "cap_status": "under_cap", "lint_status": "clean",
        "editor_model": "claude-opus-5.5-mock", "hashes": {"plain_brief": "mock-hash", "full_pack": "mock-hash"},
    }), encoding="utf-8")
    rc, payload = gate.verify(root)
    assert rc == 3
    assert payload["classification"] == "receipt_fabricated"
    assert any(f.startswith("receipt_fabricated") and "mock-hash" in f for f in payload["failures"]), payload["failures"]


def test_v2_forged_qa_receipt_is_receipt_fabricated(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, receipt_overrides={"evidence-privacy-review.json": {"reviewer": "mock-reviewer"}})
    rc, payload = gate.verify(root)
    assert rc == 3 and payload["classification"] == "receipt_fabricated"


# --- exporter ------------------------------------------------------------------------------------------------

def _export_v2(tmp_path: Path, **kwargs) -> tuple[Path, Path]:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    base.prepare_test_export(export)
    write_v2_pass_fixture(root, exported=False, export_root=export, **kwargs)
    return root, export


def _export(root: Path, export: Path, *extra: str):
    return run_synthetic_export(root, export, *extra)


def test_v2_exporter_copies_exactly_reference_pack_and_plain_brief(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path)
    proc = _export(root, export)
    assert proc.returncode == 0, proc.stdout
    payload = json.loads(proc.stdout)
    assert [item["staged_path"] for item in payload["exports"]] == [REF_REL, PLAIN_REL]
    auto = export
    assert sorted(p.name for p in auto.iterdir()) == sorted([f"{REF_NOTE}.md", f"{PLAIN_NOTE}.md"])
    assert (auto / f"{REF_NOTE}.md").read_bytes() == (root / REF_REL).read_bytes()
    assert (auto / f"{PLAIN_NOTE}.md").read_bytes() == (root / PLAIN_REL).read_bytes()
    rc, verdict = gate.verify(root)
    assert rc == 0, verdict


def test_v2_exporter_dry_run_copies_nothing(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path)
    proc = _export(root, export, "--dry-run")
    assert proc.returncode == 0, proc.stdout
    assert [item["staged_path"] for item in json.loads(proc.stdout)["exports"]] == [REF_REL, PLAIN_REL]
    assert not export.exists()


@pytest.mark.parametrize("forbidden", ["internal/executive-brief.md", "external/original-full-research-pack.md"])
def test_v2_exporter_refuses_removed_outputs_in_allowlist(tmp_path: Path, forbidden: str) -> None:
    root, export = _export_v2(tmp_path)
    manifest = read_manifest(root)
    manifest["deliverable_exports"]["allowlist"] = [REF_REL, forbidden, PLAIN_REL]
    write_manifest(root, manifest)
    (root / forbidden).parent.mkdir(parents=True, exist_ok=True)
    (root / forbidden).write_text("# removed output\n", encoding="utf-8")
    proc = _export(root, export)
    assert proc.returncode == 3
    assert "executive brief and external pack" in proc.stdout or "not exportable" in proc.stdout
    assert not export.exists()


def test_v2_exporter_refuses_executive_brief_staging(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path)
    manifest = read_manifest(root)
    manifest["deliverable_exports"]["board_brief_staging"] = "internal/executive-brief.md"
    write_manifest(root, manifest)
    proc = _export(root, export)
    assert proc.returncode == 3
    assert not export.exists()


def test_v2_exporter_taxonomy_must_review_exactly_reference_pack_and_plain_brief(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path, receipt_overrides={"taxonomy-review.json": {"reviewed_paths": [REF_REL]}})
    proc = _export(root, export)
    assert proc.returncode == 3 and "Taxonomy reviewer must review exactly" in proc.stdout
    assert not export.exists()


def test_v2_exporter_checks_reference_pack_binding_and_visual_classes(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path, receipt_overrides={"evidence-privacy-review.json": {"reference_pack_sha256": "0" * 64}})
    proc = _export(root, export)
    assert proc.returncode == 3 and "reference pack hash mismatch" in proc.stdout
    root2 = tmp_path / "two"
    root2.mkdir()
    export2 = tmp_path / "export2"
    base.prepare_test_export(export2)
    write_v2_pass_fixture(root2 / "c", exported=False, export_root=export2, receipt_overrides={"visual-review.json": {"output_classes": {"reference_pack": "PASS"}}})
    proc = _export(root2 / "c", export2)
    assert proc.returncode == 3 and "Visual QA output classes" in proc.stdout


def test_v2_exporter_refuses_forged_review_by_name(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path)
    (root / REVIEW_REL).write_text(json.dumps({
        "status": "PASS", "editor_model": "claude-opus-5.5-mock", "hashes": {"plain_brief": "mock-hash", "full_pack": "mock-hash"},
    }), encoding="utf-8")
    proc = _export(root, export)
    assert proc.returncode == 3 and "receipt_fabricated" in proc.stdout
    assert not export.exists()


def test_v2_exporter_refuses_stale_plain_brief(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path, review={"full_pack_sha256": "0" * 64})
    proc = _export(root, export, "--dry-run")
    assert proc.returncode == 3 and "full_pack_sha256 does not bind current reader bytes" in proc.stdout


def test_v2_exporter_needs_a_plain_brief_declaration(tmp_path: Path) -> None:
    root, export = _export_v2(tmp_path)
    manifest = read_manifest(root)
    manifest.pop("plain_brief")
    write_manifest(root, manifest)
    proc = _export(root, export)
    assert proc.returncode == 3 and "plain brief" in proc.stdout
    assert not export.exists()


# --- legacy control ----------------------------------------------------------------------------------------------

def test_legacy_fixture_without_layout_field_passes_as_before(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    assert "output_layout" not in read_manifest(root)
    rc, payload = gate.verify(root)
    assert rc == 0, payload
    assert payload["metrics"]["external_pack_hash_stable"] is True
    assert payload["metrics"]["board_brief_exhibit_count"] == 2
    assert "output_layout" not in payload["metrics"] or payload["metrics"]["output_layout"] == "legacy"


def test_unknown_layout_value_is_legacy(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    manifest = read_manifest(root)
    manifest["output_layout"] = "something-else"
    write_manifest(root, manifest)
    refresh_fixture_acceptance_bindings(root)
    bind_fixture_review_packet(root)
    rc, payload = gate.verify(root)
    assert rc == 0, payload
    (root / "internal" / "executive-brief.md").unlink()
    (root / "synthesis" / "board-brief.md").unlink()
    rc, payload = gate.verify(root)
    assert rc == 2 and any(h.startswith("internal_output_missing") for h in payload["holds"])


def test_legacy_still_holds_for_removed_output_checks(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    (root / "external" / "original-full-research-pack.md").unlink()
    rc, payload = gate.verify(root)
    assert rc == 2 and any(h.startswith("external_output_missing") for h in payload["holds"])


def test_legacy_reference_pack_process_terms_are_not_checked(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    full = root / "internal" / "full-research-pack.md"
    assert "controller_inference" in full.read_text()
    rc, payload = gate.verify(root)
    assert rc == 0 and not payload["failures"]


def test_legacy_exporter_still_copies_three_files(tmp_path: Path) -> None:
    root, export = gate._export_campaign(tmp_path)
    proc = _export(root, export)
    assert proc.returncode == 0, proc.stdout
    assert [item["staged_path"] for item in json.loads(proc.stdout)["exports"]] == [
        "internal/full-research-pack.md", "internal/executive-brief.md", PLAIN_REL]


# --- plain brief generator -----------------------------------------------------------------------------------------

def test_v2_campaign_mode_links_only_the_reference_pack(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, deliverable_export_enabled=False, with_plain_brief=False)
    module = gate.load_plain()
    prompts: list[str] = []

    def respond(role, system, user):
        prompts.append(user)
        if role == module.EDITOR:
            return json.dumps(editor_pass())
        assert "Should the owner adopt Example Product?" in user
        return gate.GOOD_BODY

    with offline_plain_client(module, respond):
        record = module.campaign_brief(root)

    assert record["status"] == "PASS"
    assert record["full_pack_path"] == REF_REL
    assert record["full_pack_sha256"] == sha(root / REF_REL)
    assert record["brief_sha256"] == sha(root / PLAIN_REL)
    assert all("executive-brief" not in p and "executive brief" not in p.lower() for p in prompts)
    assert "reference-pack.md" in prompts[0]
    text = (root / PLAIN_REL).read_text()
    assert text.rstrip().endswith("Detail and sources: [[" + REF_NOTE + "|reference pack]].")
    assert "executive brief" not in text and "full research pack" not in text
    front = text.split("\n---\n", 1)[0]
    assert front.count("[[") == 1 and f"[[{REF_NOTE}]]" in front
    assert "plain brief of the reference pack" in front
    assert module.check_file(root / PLAIN_REL)["ok"] is True
    assert read_manifest(root)["plain_brief"]["status"] == "reviewed_pass"
    rc, payload = gate.verify(root)
    assert rc == 0, payload


def test_v2_plain_brief_paths_have_no_executive_brief() -> None:
    module = gate.load_plain()
    manifest = {"output_layout": LAYOUT, "internal_outputs": [{"path": REF_REL}], "plain_brief": {"path": PLAIN_REL, "review": REVIEW_REL}}
    assert module.campaign_plain_brief_paths(manifest) == (PLAIN_REL, REVIEW_REL, REF_REL, None)
    legacy = {"internal_outputs": [{"path": "internal/full-research-pack.md"}, {"path": "internal/executive-brief.md"}], "plain_brief": {}}
    assert module.campaign_plain_brief_paths(legacy)[2:] == ("internal/full-research-pack.md", "internal/executive-brief.md")


def test_legacy_campaign_mode_footer_is_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    base.write_depth_pass_fixture(root, deliverable_export_enabled=False)
    manifest = read_manifest(root)
    manifest.update({"target": "Example Product", "decision_question": "Should the owner adopt Example Product?"})
    manifest["plain_brief"] = {"required": True, "path": PLAIN_REL, "review": REVIEW_REL, "status": "not_started"}
    manifest["deliverable_exports"]["allowlist"].append(PLAIN_REL)
    write_manifest(root, manifest)
    module = gate.load_plain()

    def respond(role, system, user):
        return json.dumps(editor_pass()) if role == module.EDITOR else gate.GOOD_BODY

    with offline_plain_client(module, respond):
        module.campaign_brief(root)
    text = (root / PLAIN_REL).read_text()
    assert "Detail and sources: [[full-research-pack|full research pack]], [[executive-brief|executive brief]]." in text
    assert "source: plain brief of the internal full research pack (internal/full-research-pack.md)" in text


# --- controller ------------------------------------------------------------------------------------------------------

def _complete_all_lanes(controller, root: Path) -> None:
    for lane in read_manifest(root)["lanes"]:
        claim = controller.claim_lane(root, lane["id"], route="primary", worker_id="w1")
        controller.ingest_completion_event(root, {"event_id": f"e-{lane['id']}", "lane_id": lane["id"], "attempt_id": claim["attempt_id"], "status": "completed"})


def test_v2_controller_phase_order_skips_external_freeze(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    controller = base.load_controller_module()
    state = controller.load_state(root)
    assert state["phase_order"] == ["intake", "lanes", "internal_synthesis", "qa", "plain_brief", "export_closeout"]
    assert "external_freeze" not in state["phases"]
    _complete_all_lanes(controller, root)
    state = controller.load_state(root)
    assert state["phases"]["lanes"]["status"] == "complete"
    assert "external_freeze" not in state["phases"]
    assert controller.resume_selection(root)["next_phase"] == "internal_synthesis"
    # a re-run of the scaffold step must not bring the removed phase back
    controller.initialize_campaign_state(root, read_manifest(root))
    assert "external_freeze" not in controller.load_state(root)["phases"]


def test_legacy_controller_phase_order_keeps_external_freeze(tmp_path: Path) -> None:
    root = init_campaign(tmp_path, "--output-layout", "legacy")
    controller = base.load_controller_module()
    state = controller.load_state(root)
    assert state["phase_order"] == ["intake", "lanes", "external_freeze", "internal_synthesis", "qa", "plain_brief", "export_closeout"]
    _complete_all_lanes(controller, root)
    assert controller.resume_selection(root)["next_phase"] == "external_freeze"


def test_v2_context_release_only_after_all_lanes_finish(tmp_path: Path) -> None:
    root = init_campaign(tmp_path)
    controller = base.load_controller_module()
    refused = controller.record_context_release(root)
    assert refused["status"] == "refused" and refused["reason"] == "research_lanes_not_finished"
    assert "context_release" not in controller.load_state(root)
    _complete_all_lanes(controller, root)
    released = controller.record_context_release(root)
    assert released["status"] == "released" and released["released_at"]
    state = controller.load_state(root)
    assert state["context_release"]["released_at"] == released["released_at"]
    assert any(e.get("type") == "context_release" for e in state["events"])
    # recording again keeps the first time
    assert controller.record_context_release(root)["released_at"] == released["released_at"]


def test_legacy_controller_refuses_context_release_helper(tmp_path: Path) -> None:
    root = init_campaign(tmp_path, "--output-layout", "legacy")
    controller = base.load_controller_module()
    assert controller.record_context_release(root)["status"] == "refused"
    assert controller.record_context_release(root)["reason"] == "not_in_brief_and_reference_layout"


def test_v2_record_plain_brief_phase_completes(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    controller = base.load_controller_module()
    controller.initialize_campaign_state(root, read_manifest(root))
    assert controller.record_plain_brief(root)["status"] == "complete"


# --- completion check ------------------------------------------------------------------------------------------------

def test_completion_check_hashes_the_reference_pack_in_v2(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    cc = load("complete_campaign")
    result = cc.evaluate(root, online=False, homes=[], pin_checker=False)
    files = result["checked_files_sha256"]
    assert files[REF_REL] == sha(root / REF_REL)
    assert "internal/full-research-pack.md" not in files
    assert files[PLAIN_REL] == sha(root / PLAIN_REL) and files["manifest.json"] == sha(root / "manifest.json")


def test_completion_check_reference_pack_path_follows_the_manifest(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    manifest = read_manifest(root)
    manifest["internal_outputs"] = [{"path": "internal/other-reference.md"}]
    write_manifest(root, manifest)
    (root / "internal" / "other-reference.md").write_text("x", encoding="utf-8")
    cc = load("complete_campaign")
    files = cc.evaluate(root, online=False, homes=[], pin_checker=False)["checked_files_sha256"]
    assert files["internal/other-reference.md"] == sha(root / "internal" / "other-reference.md")


def test_completion_check_legacy_still_hashes_the_full_pack(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    cc = load("complete_campaign")
    files = cc.evaluate(root, online=False, homes=[], pin_checker=False)["checked_files_sha256"]
    assert files["internal/full-research-pack.md"] == sha(root / "internal" / "full-research-pack.md")
    assert REF_REL not in files


def test_completion_check_v2_fails_forged_review(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    (root / REVIEW_REL).write_text(json.dumps({"status": "PASS", "editor_model": "claude-opus-5.5-mock", "hashes": {"plain_brief": "mock-hash"}}), encoding="utf-8")
    cc = load("complete_campaign")
    result = cc.evaluate(root, online=False, homes=[], pin_checker=False)
    assert result["status"] == "FAIL" and "receipt_fabricated" in result["classes"]


def test_completion_checker_files_are_unchanged_in_scope() -> None:
    cc = load("complete_campaign")
    assert cc.CHECKER_FILES == ["complete_campaign.py", "verify_campaign_depth.py", "plain_brief_rewrite.py", "receipt_integrity.py",
                                "web_search_status.py", "quote_check.py", "export_deliverables.py"]


# --- web search brief lookup -----------------------------------------------------------------------------------------

def test_web_search_brief_text_ignores_executive_brief_in_v2(tmp_path: Path) -> None:
    wss = load("web_search_status")
    root = tmp_path / "c"
    (root / "internal").mkdir(parents=True)
    (root / "internal" / "executive-brief.md").write_text("web search was down", encoding="utf-8")
    assert wss.brief_text(root, {"output_layout": LAYOUT, "plain_brief": {"path": PLAIN_REL}}) == ""
    assert wss.brief_text(root, {"plain_brief": {"path": PLAIN_REL}}) == "web search was down"
    (root / PLAIN_REL).write_text("plain text", encoding="utf-8")
    assert wss.brief_text(root, {"output_layout": LAYOUT, "plain_brief": {"path": PLAIN_REL}}) == "plain text"


def test_v2_reference_pack_has_no_word_floor(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path)
    pack = root / REF_REL
    lines = pack.read_text(encoding="utf-8").splitlines()
    short = "\n".join(lines[:120]) + "\n"
    pack.write_text(short, encoding="utf-8")
    rc, payload = gate.verify(root)
    assert not any(h.startswith("internal_full_pack_words") for h in payload.get("holds", []) + payload.get("failures", [])), payload


# --- the brief must pass the owner's plain-prose checker in layout v2 -------------------------------------

THICK = gate.GOOD_BODY.replace(
    "There is no independent review of how it behaves over a year.",
    "There is no independent review of how it behaves over a year; the vendor's own notes \u2014 which are thin \u2014 "
    "cover the first month, the second month, the third month and the setup steps, and they say nothing about upgrades, "
    "rollbacks, backups, restores or what happens when the service is unavailable for more than a day.")


def test_v2_brief_that_fails_the_plain_prose_checker_fails(tmp_path: Path) -> None:
    pbr = load("plain_brief_rewrite")
    assert pbr.prose_result(gate.GOOD_BODY)["ok"] is True
    result = pbr.prose_result(THICK)
    assert result["ok"] is False and any("semicolons" in f for f in result["fails"])
    root = v2_campaign(tmp_path, plain_body=THICK)
    rc, payload = gate.verify(root)
    assert rc == 3, payload
    assert any(f.startswith("plain_brief_prose_check_failed") for f in payload["failures"])


def test_legacy_brief_is_not_held_to_the_plain_prose_checker(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path, body=THICK)
    rc, payload = gate.verify(root)
    assert not any(f.startswith("plain_brief_prose_check_failed") for f in payload["failures"])


def test_v2_writer_gets_the_plain_prose_rules_and_the_lint_reports_them(tmp_path: Path) -> None:
    pbr = load("plain_brief_rewrite")
    seen = []

    def respond(role, system, user):
        seen.append((role, system))
        if role == pbr.EDITOR:
            return json.dumps(editor_pass())
        return gate.GOOD_BODY

    out = tmp_path / "rounds"
    out.mkdir()
    with offline_plain_client(pbr, respond):
        _, final = pbr.rewrite("x", {"title": "x", "slug": "x", "sources": [], "prose": True}, out)
    assert any(role == "writer" and "Do not alter the prose checker" in system for role, system in seen)
    assert final["lint"]["prose"]["ok"] is True and final["lint"]["ok"] is True
    with offline_plain_client(pbr, respond):
        _, legacy = pbr.rewrite("y", {"title": "y", "slug": "y", "sources": []}, out)
    assert "prose" not in legacy["lint"]


def test_c1_decision_fields_do_not_count_as_nested_bullets() -> None:
    pbr = load("plain_brief_rewrite")
    block = ("## What the owner needs to decide\n\n1. **Should the trial wait?**\n"
             "   - Why it matters: It adds an always-on server.\n"
             "   - If you do nothing: The tool stays on the watch list.\n"
             "   - Suggested: Yes \u2013 keep the July trigger.\n"
             "   - Sources: https://example.com/docs\n")
    assert pbr.prose_result(block)["ok"] is True
    other = "## What we found\n\n- A finding.\n  - A nested detail that is not part of the form.\n"
    assert "nested bullets" in pbr.prose_result(other)["fails"]


# --- a diagram dropped after repairs stopped helping ------------------------------------------------------

OMISSION_NOTE = "Diagram omitted: the system map kept overlapping header text after repairs."
EXTRA_ANCHOR = "Exhibit E4 — Timeline table"


def _add_table(root: Path) -> None:
    """A fourth real exhibit, so these tests isolate the diagram rule from the exhibit floor."""
    path = root / "synthesis" / "visual-exhibits.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["topic_requires_diagram"] = True  # the diagram rule only applies when the topic needs one
    data["exhibits"].append({"id": "E4", "title": "Timeline table", "type": "matrix", "output_class": "reference_pack",
        "appears_in": ["reference_pack"], "anchor": EXTRA_ANCHOR, "evidence_refs": ["S4"], "source_data_refs": ["S4"],
        "decision_use": "Orders the build steps.", "reviewer": "visual-qa"})
    path.write_text(json.dumps(data), encoding="utf-8")


def _omit_diagram(root: Path, **changes) -> None:
    path = root / "synthesis" / "visual-exhibits.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    exhibit = data["exhibits"][0]
    assert exhibit["type"] == "diagram"
    for key in ("render_method", "source_artifact", "validation_receipt"):
        exhibit.pop(key, None)
    exhibit.update({"status": "omitted", "anchor": OMISSION_NOTE,
                    "omission_reason": "R3 overlapping header text failed after two repairs"}, **changes)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_v2_omitted_diagram_with_note_and_reason_passes(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, target_type="workflow", reference_extra="\n" + OMISSION_NOTE + "\n" + EXTRA_ANCHOR + "\n")
    _add_table(root)
    _omit_diagram(root)
    rc, payload = gate.verify(root)
    assert rc == 0, payload
    assert payload["metrics"]["topic_requires_diagram"] is True
    assert payload["metrics"]["diagram_count"] == 0
    assert payload["metrics"]["omitted_diagram_count"] == 1


def test_v2_omitted_diagram_without_note_in_pack_holds(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, target_type="workflow", reference_extra="\n" + EXTRA_ANCHOR + "\n")
    _add_table(root)
    _omit_diagram(root)
    rc, payload = gate.verify(root)
    assert rc != 0
    assert any("omission_note_missing" in h for h in payload["holds"] + payload.get("problems", [])), payload


def test_v2_omitted_diagram_without_reason_holds(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, target_type="workflow", reference_extra="\n" + OMISSION_NOTE + "\n" + EXTRA_ANCHOR + "\n")
    _add_table(root)
    _omit_diagram(root, omission_reason="")
    rc, payload = gate.verify(root)
    assert rc != 0
    assert any("omission_reason_missing" in h for h in payload["holds"] + payload.get("problems", [])), payload


def test_v2_missing_diagram_without_omission_record_still_holds(tmp_path: Path) -> None:
    root = v2_campaign(tmp_path, target_type="workflow", reference_extra="\n" + EXTRA_ANCHOR + "\n")
    _add_table(root)
    path = root / "synthesis" / "visual-exhibits.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["exhibits"] = [e for e in data["exhibits"] if e["type"] != "diagram"]
    path.write_text(json.dumps(data), encoding="utf-8")
    rc, payload = gate.verify(root)
    assert rc != 0
    assert any(h.startswith("diagram_count 0 <") for h in payload["holds"]), payload
