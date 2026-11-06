#!/usr/bin/env python3
"""Verify a deep-research campaign has enough depth to close.

This verifier is intentionally fail-closed for `true_deep_research`: the dual-output
contract, clean-room external pack, independent QA receipts, visual output-class
consistency, and export routing invariants must all be present before PASS.

The script is read-only. It does not mutate campaign artifacts, exports, Linear, or
runtime state.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import hashlib
import json
import re
import sys
# Match the read-only completion entry point; never generate checker bytecode.
sys.dont_write_bytecode = True
from pathlib import Path
from typing import Any

try:
    from preseed import verify_preseed
except ImportError:  # pragma: no cover - importlib callers can inject explicitly
    verify_preseed = None

# Word floors apply only to the internal full pack (reference depth). Human-facing outputs get caps,
# never floors: the plain brief is capped by plain_brief_rewrite.MAX_WORDS, and a board brief
# that is the only human output is capped by max_board_words. 0 means no cap.
PLAIN_BRIEF_MAX_WORDS = 2000
DEFAULT_THRESHOLDS = {
    "brief": {"min_lanes": 0, "min_lane_artifacts": 0, "min_sources": 3, "min_full_words": 800, "min_action_rows": 0, "max_board_words": 0, "max_plain_brief_words": PLAIN_BRIEF_MAX_WORDS, "min_full_exhibits": 0, "min_board_exhibits": 0, "min_diagrams": 0},
    "board_brief_only": {"min_lanes": 0, "min_lane_artifacts": 0, "min_sources": 5, "min_full_words": 0, "min_action_rows": 0, "max_board_words": 2000, "max_plain_brief_words": PLAIN_BRIEF_MAX_WORDS, "min_full_exhibits": 0, "min_board_exhibits": 0, "min_diagrams": 0},
    "refresh_delta": {"min_lanes": 2, "min_lane_artifacts": 2, "min_sources": 8, "min_full_words": 1500, "min_action_rows": 1, "max_board_words": 0, "max_plain_brief_words": PLAIN_BRIEF_MAX_WORDS, "min_full_exhibits": 0, "min_board_exhibits": 0, "min_diagrams": 0},
    "true_deep_research": {"min_lanes": 6, "min_lane_artifacts": 6, "min_sources": 15, "min_full_words": 8000, "min_action_rows": 5, "max_board_words": 0, "max_plain_brief_words": PLAIN_BRIEF_MAX_WORDS, "min_full_exhibits": 3, "min_board_exhibits": 2, "min_diagrams": 1},
}

PRODUCT_TYPES = {"product", "company", "market", "category"}
STRUCTURAL_TYPES = PRODUCT_TYPES | {"repo", "tool", "architecture", "internal-system", "workflow", "venture", "operating-model"}
VALID_EXHIBIT_TYPES = {"diagram", "map", "matrix", "scorecard", "table", "heatmap", "portfolio", "timeline"}
PASSING_VISUAL_STATUSES = {"complete", "pass", "passed", "verified"}
PASSING_QA_STATUSES = {"PASS", "PASSED"}
EXPORT_ONLY_RECEIPTS = {"taxonomy_review", "librarian_repair"}
REQUIRED_QA_RECEIPTS = {
    "context_handoff": "qa/context-handoff-review.json",
    "evidence_privacy": "qa/evidence-privacy-review.json",
    "visual": "qa/visual-review.json",
    "taxonomy_review": "qa/taxonomy-review.json",
    "librarian_repair": "qa/librarian-repair-receipt.json",
    "final_assurance": "qa/final-assurance.json",
}
INTERNAL_OUTPUTS = ["internal/full-research-pack.md", "internal/executive-brief.md"]
PLAIN_BRIEF_OUTPUT = "internal/plain-brief.md"
EXTERNAL_OUTPUT = "external/original-full-research-pack.md"
# layout v2 has two outputs, the plain brief and the reference pack. Any other layout value
# (or none) is the legacy four-output layout and keeps every earlier check.
OUTPUT_LAYOUT_V2 = "brief-and-reference-v2"
REFERENCE_PACK_OUTPUT = "internal/reference-pack.md"
ACTION_REGISTER_SIDECAR = "synthesis/action-register.md"
V2_FORBIDDEN_EXPORTS = (INTERNAL_OUTPUTS[1], INTERNAL_OUTPUTS[0], EXTERNAL_OUTPUT)
V2_VISUAL_CLASSES = ("reference_pack", "plain_brief")
# Names a reference pack may be tagged with; v2 reads them as process records. Citation codes such as [S12] stay allowed.
REFERENCE_PACK_LABEL_TERMS = [r"\binternal_context\b", r"\bexternal_evidence\b"]
PRIVATE_PATTERNS = {
    "export_wikilink": re.compile(r"\[\[[^\]]+\]\]"),
    # Keep issue keys case-sensitive so ordinary lowercase URL slugs such as
    # `chapter-1`, `app-5`, and `cps-230` are not mistaken for Linear IDs.
    # The Linear hostname itself remains case-insensitive.
    # Public model labels and Linear's public developer docs are not private issue refs.
    # Preserve matching for workspace URLs and all other uppercase issue-like prefixes.
    "linear_id": re.compile(r"\b(?!(?:GPT|GLM)[-\u2010-\u2015])[A-Z]{2,10}[-\u2010-\u2015]\d+\b|(?i:\bissue[-\u2010-\u2015]\d+\b)|(?i:linear\.app/(?!developers(?:/|\b)))"),
    "session_ref": re.compile(r"@session:|\bsession[_ -]?(?:id|ref|excerpt)\b", re.I),
    "private_path": re.compile(r"/(?:home/[^/\s]+|Users/[^/\s]+|mnt/(?:hermes|archive))[^\s)]*", re.I),
    "internal_hostname": re.compile(r"\b(?:localhost|127\.0\.0\.1|host\.docker\.internal|\.local\b|internal\.)", re.I),
    "owner_specific": re.compile(r"\b(?:the owner-stack|future the owner|the owner's|the owner\s+specific)\b", re.I),
    "context_brief": re.compile(r"context(?:/|\\|\s+|_|-)+brief(?:\.md|\.json)?|internal(?:\s+|_|-)+context|taxonomy|librarian", re.I),
}


QUANT_PATTERN = re.compile(
    r"(?:A\$|US\$|\$)\s?\d+(?:\.\d+)?(?:\s?(?:m|b|k|million|billion))?"
    r"|\b\d+(?:\.\d+)?%"
    r"|\b\d{1,3}(?:,\d{3})+\b"
    r"|\b\d+(?:\.\d+)?\s?(?:GB|TB|GW|MW|STU|GPUs?|tokens?|customers?|parameters?)\b",
    re.I,
)


def load_lane_findings(lane_json_paths: list[Path]) -> list[dict]:
    """Combine `findings` dicts from lane JSON files; unreadable files are skipped."""
    findings: list[dict] = []
    for path in sorted(lane_json_paths):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - unreadable lane JSON is ignored here
            continue
        items = data.get("findings") if isinstance(data, dict) else None
        if isinstance(items, list):
            findings.extend(f for f in items if isinstance(f, dict))
    return findings


def quantitative_retention_holds(
    lane_findings: list[dict], pack_text: str, *, target_type: str = ""
) -> tuple[dict[str, int | float], list[str]]:
    lane_quant = [
        f for f in lane_findings
        if isinstance(f, dict) and QUANT_PATTERN.search(str(f.get("claim") or ""))
    ]
    pack_body = pack_text.split("\n# Detailed evidence\n")[0]
    pack_hits = set(m.group(0).lower() for m in QUANT_PATTERN.finditer(pack_body))
    metrics = {
        "lane_quantitative_claims": len(lane_quant),
        "pack_quantitative_tokens": len(pack_hits),
    }
    # This is only a deterministic screen, never semantic proof. The existing
    # full-pack/editor QA must compare important quantities and qualifications
    # with saved evidence, including subject, unit, period and uncertainty.
    metrics.update({"semantic_retention_verified": False, "quantity_check_kind": "screening_only"})
    holds: list[str] = []
    folded_pack = " ".join(pack_text.lower().split())
    missing_quantities = set()
    missing_qualifications = set()
    for finding in lane_findings:
        if not isinstance(finding, dict) or finding.get("important") is False:
            continue
        claim = str(finding.get("claim") or "")
        for match in QUANT_PATTERN.finditer(claim):
            quantity = " ".join(match.group(0).lower().split())
            if quantity not in folded_pack:
                missing_quantities.add(quantity)
        qualifiers = finding.get("qualifications")
        if not isinstance(qualifiers, list):
            qualifiers = re.findall(r"\b(?:not commercial(?: production)?|not confirmed|planned|proposed|estimated?|trial|unverified)\b", claim, re.I)
        for qualifier in qualifiers:
            if str(qualifier).strip() and " ".join(str(qualifier).lower().split()) not in folded_pack:
                missing_qualifications.add(str(qualifier))
    if missing_quantities:
        holds.append("quantitative_evidence_dropped_in_synthesis missing important quantities: " + ", ".join(sorted(missing_quantities)))
    if missing_qualifications:
        holds.append("material_qualifications_missing_in_synthesis " + ", ".join(sorted(missing_qualifications)))
    return metrics, holds


def load_plain_brief_module():
    """Import plain_brief_rewrite.py from this directory (it reads no secret at import time)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "plain_brief_rewrite.py"
    spec = importlib.util.spec_from_file_location("plain_brief_rewrite", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_receipt_integrity():
    """Import receipt_integrity.py from this directory (offline checks only; no secret is read)."""
    import importlib.util

    path = Path(__file__).resolve().parent / "receipt_integrity.py"
    spec = importlib.util.spec_from_file_location("receipt_integrity", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fabricated_receipt_failures(root: Path, manifest: dict) -> list[str]:
    """name hand-written QA records (placeholder models or non-SHA-256 hashes) as receipt_fabricated."""
    try:
        module = load_receipt_integrity()
    except Exception as exc:  # noqa: BLE001 - fail closed when the check cannot load
        return [f"receipt_fabricated check unavailable {type(exc).__name__}"]
    skip = {plain_brief_review_rel(manifest)} if isinstance(manifest.get("plain_brief"), dict) else set()
    return ["receipt_fabricated " + item for item in module.sweep_qa_dir(root) if item.split(":", 1)[0] not in skip]


def is_v2(manifest: dict) -> bool:
    return isinstance(manifest, dict) and manifest.get("output_layout") == OUTPUT_LAYOUT_V2


def plain_brief_review_rel(manifest: dict) -> str:
    pb = manifest.get("plain_brief") if isinstance(manifest.get("plain_brief"), dict) else {}
    return rel_path(pb.get("review")) or "qa/plain-brief-review.json"


def plain_brief_required(manifest: dict) -> bool:
    pb = manifest.get("plain_brief")
    return isinstance(pb, dict) and pb.get("required", True) is not False


def plain_brief_rel(manifest: dict) -> str:
    pb = manifest.get("plain_brief") if isinstance(manifest.get("plain_brief"), dict) else {}
    return rel_path(pb.get("path")) or PLAIN_BRIEF_OUTPUT


def deliverable_exportable_outputs(manifest: dict) -> list[str]:
    """Legacy: the two internal outputs plus the plain brief when declared. Layout v2: reference pack and plain brief."""
    if is_v2(manifest):
        return [reference_pack_rel(manifest), plain_brief_rel(manifest)]
    return INTERNAL_OUTPUTS + ([plain_brief_rel(manifest)] if plain_brief_required(manifest) else [])


def reference_pack_rel(manifest: dict) -> str:
    internals = manifest.get("internal_outputs")
    first = internals[0] if isinstance(internals, list) and internals else None
    return rel_path(first.get("path") if isinstance(first, dict) else first) or REFERENCE_PACK_OUTPUT


def reference_pack_process_records(text: str) -> list[str]:
    """Process records that belong in sidecars, not in the v2 reference pack.

    Reuses plain_brief_rewrite.PROCESS_TERMS (Action Register, QA/PASS/HOLD receipts, controller_inference,
    40-64 hex hashes and so on) plus the other provenance labels. Link targets and bare URLs are ignored,
    so a commit URL is fine. Citation codes such as [S12] are allowed here.
    """
    module = load_plain_brief_module()
    scan = re.sub(r"(?<!\[)\[([^\[\]]*)\]\([^)]*\)", r"\1", text)
    scan = re.sub(r"<https?://[^>]*>|https?://\S+", " ", scan)
    hits = {m.group(0) for pattern in list(module.PROCESS_TERMS) + REFERENCE_PACK_LABEL_TERMS for m in re.finditer(pattern, scan, re.I)}
    return sorted(hits)


def plain_brief_gate(root: Path, manifest: dict) -> dict:
    # Final acceptance is mandatory even when the delegated legacy brief is optional.
    # A pack-only pre-generation PASS is not publication permission.
    readiness_holds: list[str] = []
    try:
        from research_qa_repair import campaign_readiness

        readiness = campaign_readiness(root, require_brief=True, review_rel=plain_brief_review_rel(manifest))
        if readiness.get("status") != "PASS" or readiness.get("publication_qa_allowed") is not True:
            reasons = "; ".join(readiness.get("findings") or [])
            readiness_holds.append(f"campaign_readiness {readiness.get('status')}: {reasons or 'publication QA not allowed'}")
    except Exception as exc:  # noqa: BLE001 - no acceptance when the shared gate is unavailable
        readiness_holds.append(f"campaign_readiness_unavailable {type(exc).__name__}")
    try:
        module = load_plain_brief_module()
    except Exception as exc:  # noqa: BLE001 - fail closed when the lint cannot load
        return {"declared": True, "required": True, "metrics": {"plain_brief_status": "lint_unavailable"}, "failures": [], "holds": readiness_holds + [f"plain_brief_lint_unavailable {type(exc).__name__}"]}
    gate = module.verify_campaign_plain_brief(root, manifest)
    gate["holds"].extend(readiness_holds)
    return gate


def words(text: str) -> int:
    return len(re.findall(r"\w+", text))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""


def sha256_path(path: Path) -> str | None:
    if not path.exists():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def manifest_path(manifest: dict, dotted: str, default: str) -> str:
    cur: Any = manifest
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return rel_path(cur) or default


def count_sources(root: Path, full_text: str, external_text: str = "") -> int:
    citations = 0
    cit_path = root / "citations.json"
    if cit_path.exists():
        try:
            data = json.loads(cit_path.read_text())
            citations = len(data.get("sources", []))
        except Exception:
            citations = 0
    combined = full_text + "\n" + external_text
    bracket_ids = len(set(re.findall(r"\[[A-Z]*\d+\]", combined)))
    url_count = len(set(re.findall(r"https?://[^\s)]+", combined)))
    return max(citations, bracket_ids, url_count)


def action_rows(text: str) -> int:
    if "ActionRegister" not in text and "Action Register" not in text:
        return 0
    return len(re.findall(r"^\|\s*(?:[A-Z]+-)?(?:AR|A)\d+\b", text, re.M))


def duplicate_prose_metrics(text: str) -> dict[str, int]:
    """Count exact repeated prose blocks while ignoring small table rows."""
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
    counts: dict[str, int] = {}
    for block in blocks:
        if len(block) < 120 or block.startswith("|"):
            continue
        counts[block] = counts.get(block, 0) + 1
    repeated = {block: count for block, count in counts.items() if count > 1}
    return {
        "duplicate_prose_groups": len(repeated),
        "duplicate_prose_instances": sum(count - 1 for count in repeated.values()),
    }


def is_placeholder(value: object) -> bool:
    text = str(value or "").strip()
    return not text or bool(re.match(r"^(?:TBD|TODO|N/?A)(?:\s|:|$)", text, re.I))


def normalize_token(value: object) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def normalize_appears_in(value: object) -> set[str]:
    if isinstance(value, str):
        return {normalize_token(value)}
    if isinstance(value, list):
        return {normalize_token(item) for item in value if str(item).strip()}
    return set()


def class_present(appears: set[str], *names: str) -> bool:
    aliases = {normalize_token(name) for name in names}
    if "both" in appears:
        return True
    return bool(appears & aliases)


def load_visual_exhibits(root: Path, manifest: dict) -> tuple[dict | None, list[str]]:
    rel = manifest_path(manifest, "visual_exhibits.path", "synthesis/visual-exhibits.json")
    path, unsafe = confined_path(root, rel)
    if unsafe:
        return None, [f"visual_exhibits_unsafe_path {unsafe}"]
    if not path.exists():
        return None, [f"visual_exhibits_missing {rel}"]
    try:
        data = json.loads(path.read_text())
    except Exception as exc:
        return None, [f"visual_exhibits_invalid_json {type(exc).__name__}"]
    return data, []


def _check_rendered_diagram(root: Path, exhibit: dict, problems: list[str]) -> None:
    render_method = normalize_token(exhibit.get("render_method"))
    if render_method in {"technical_diagrams", "technical_diagram", "rendered_html_svg", "html_svg", "svg"}:
        receipt = rel_path(exhibit.get("validation_receipt"))
        receipt_path, unsafe = confined_path(root, receipt)
        if unsafe:
            problems.append(f"visual_exhibit_{exhibit.get('id')}_validation_receipt_unsafe")
            return
        if not receipt or not receipt_path.exists():
            problems.append(f"visual_exhibit_{exhibit.get('id')}_validation_receipt_missing")
            return
        try:
            data = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception:
            problems.append(f"visual_exhibit_{exhibit.get('id')}_validation_receipt_invalid")
            return
        if str(data.get("status", "")).upper() not in PASSING_QA_STATUSES:
            problems.append(f"visual_exhibit_{exhibit.get('id')}_validation_receipt_status {data.get('status')}")


def _omitted_diagram(exhibit: dict, pack_text: str) -> tuple[bool, str | None]:
    """A diagram dropped after repairs stopped helping, disclosed in the pack.

    It counts toward the diagram requirement only when the ledger gives a reason
    and the pack carries the omission note at the recorded anchor.
    """
    if normalize_token(exhibit.get("type")) != "diagram" or normalize_token(exhibit.get("status")) != "omitted":
        return False, None
    ex_id = str(exhibit.get("id", "?"))
    anchor = str(exhibit.get("anchor") or "").strip()
    if is_placeholder(exhibit.get("omission_reason")):
        return True, f"visual_exhibit_{ex_id}_omission_reason_missing"
    if not anchor or anchor not in pack_text:
        return True, f"visual_exhibit_{ex_id}_omission_note_missing"
    return True, None


def visual_metrics(root: Path, manifest: dict, internal_full_text: str, internal_brief_text: str, external_text: str) -> tuple[dict, list[str]]:
    if is_v2(manifest):
        return visual_metrics_v2(root, manifest, internal_full_text)
    data, problems = load_visual_exhibits(root, manifest)
    manifest_gate = manifest.get("visual_exhibits", {}) if isinstance(manifest.get("visual_exhibits"), dict) else {}
    target_type = str(manifest.get("target_type", "")).lower().strip().replace("_", "-")
    topic_requires_diagram = bool(target_type in STRUCTURAL_TYPES or manifest_gate.get("topic_requires_diagram") or (data or {}).get("topic_requires_diagram"))
    metrics = {
        "visual_exhibits_status": "missing" if data is None else str(data.get("status", "missing")),
        "topic_requires_diagram": topic_requires_diagram,
        "full_pack_exhibit_count": 0,
        "board_brief_exhibit_count": 0,
        "diagram_count": 0,
        "omitted_diagram_count": 0,
        "valid_visual_exhibit_count": 0,
    }
    if data is None:
        return metrics, problems
    exhibits = data.get("exhibits", [])
    if not isinstance(exhibits, list):
        return metrics, problems + ["visual_exhibits_not_list"]
    if str(data.get("status", "")).lower().strip() not in PASSING_VISUAL_STATUSES:
        problems.append(f"visual_exhibits_status {data.get('status')}")
    for idx, exhibit in enumerate(exhibits, start=1):
        if not isinstance(exhibit, dict):
            problems.append(f"visual_exhibit_{idx}_not_object")
            continue
        ex_id = str(exhibit.get("id", idx))
        omitted, omission_problem = _omitted_diagram(exhibit, internal_full_text)
        if omitted:
            if omission_problem:
                problems.append(omission_problem)
            else:
                metrics["omitted_diagram_count"] += 1
            continue
        typ = normalize_token(exhibit.get("type"))
        appears = normalize_appears_in(exhibit.get("appears_in")) | normalize_appears_in(exhibit.get("output_class"))
        evidence_refs = exhibit.get("evidence_refs") or []
        source_data_refs = exhibit.get("source_data_refs") or []
        evidence_basis = exhibit.get("evidence_basis")
        has_evidence = bool(evidence_refs) or bool(source_data_refs) or not is_placeholder(evidence_basis)
        anchor = str(exhibit.get("anchor") or "").strip()
        internal_full_present = bool(anchor) and anchor in internal_full_text
        internal_brief_present = bool(anchor) and anchor in internal_brief_text
        external_present = bool(anchor) and anchor in external_text
        artifact = rel_path(exhibit.get("artifact") or exhibit.get("rendered_artifact") or exhibit.get("source_artifact")).split("#", 1)[0]
        artifact_path, artifact_unsafe = confined_path(root, artifact) if artifact else (root / "__missing_artifact__", None)
        if artifact_unsafe:
            problems.append(f"visual_exhibit_{ex_id}_artifact_unsafe")
        artifact_exists = bool(artifact) and not artifact_unsafe and artifact_path.exists()
        base_valid = (
            not is_placeholder(exhibit.get("id"))
            and not is_placeholder(exhibit.get("title"))
            and typ in VALID_EXHIBIT_TYPES
            and appears
            and not is_placeholder(exhibit.get("decision_use"))
            and has_evidence
            and (internal_full_present or internal_brief_present or external_present or artifact_exists)
        )
        if class_present(appears, "internal_executive_brief", "board_brief", "brief") and not internal_brief_present and not artifact_exists:
            problems.append(f"visual_exhibit_{ex_id}_internal_executive_brief_anchor_missing")
            base_valid = False
        if class_present(appears, "internal_full_pack", "full_pack", "full") and not internal_full_present and not artifact_exists:
            problems.append(f"visual_exhibit_{ex_id}_internal_full_pack_anchor_missing")
            base_valid = False
        if class_present(appears, "external_full_pack", "external") and bool(exhibit.get("uses_internal_context")):
            problems.append(f"visual_exhibit_{ex_id}_internal_context_in_external")
            base_valid = False
        if class_present(appears, "external_full_pack", "external") and not external_present and not artifact_exists:
            problems.append(f"visual_exhibit_{ex_id}_external_full_pack_anchor_missing")
            base_valid = False
        if typ == "diagram":
            _check_rendered_diagram(root, exhibit, problems)
        if not base_valid:
            problems.append(f"visual_exhibit_{ex_id}_invalid_or_placeholder")
            continue
        metrics["valid_visual_exhibit_count"] += 1
        if class_present(appears, "internal_full_pack", "full_pack", "full"):
            metrics["full_pack_exhibit_count"] += 1
        if class_present(appears, "internal_executive_brief", "board_brief", "brief"):
            metrics["board_brief_exhibit_count"] += 1
        if typ == "diagram":
            metrics["diagram_count"] += 1
    return metrics, problems


def visual_metrics_v2(root: Path, manifest: dict, reference_text: str) -> tuple[dict, list[str]]:
    """Exhibit ledger check for layout v2: exhibits live in the reference pack; the plain brief carries none."""
    data, problems = load_visual_exhibits(root, manifest)
    manifest_gate = manifest.get("visual_exhibits", {}) if isinstance(manifest.get("visual_exhibits"), dict) else {}
    target_type = str(manifest.get("target_type", "")).lower().strip().replace("_", "-")
    topic_requires_diagram = bool(target_type in STRUCTURAL_TYPES or manifest_gate.get("topic_requires_diagram") or (data or {}).get("topic_requires_diagram"))
    metrics = {
        "visual_exhibits_status": "missing" if data is None else str(data.get("status", "missing")),
        "topic_requires_diagram": topic_requires_diagram,
        "full_pack_exhibit_count": 0,
        "diagram_count": 0,
        "omitted_diagram_count": 0,
        "valid_visual_exhibit_count": 0,
    }
    if data is None:
        return metrics, problems
    exhibits = data.get("exhibits", [])
    if not isinstance(exhibits, list):
        return metrics, problems + ["visual_exhibits_not_list"]
    if str(data.get("status", "")).lower().strip() not in PASSING_VISUAL_STATUSES:
        problems.append(f"visual_exhibits_status {data.get('status')}")
    for idx, exhibit in enumerate(exhibits, start=1):
        if not isinstance(exhibit, dict):
            problems.append(f"visual_exhibit_{idx}_not_object")
            continue
        ex_id = str(exhibit.get("id", idx))
        omitted, omission_problem = _omitted_diagram(exhibit, reference_text)
        if omitted:
            if omission_problem:
                problems.append(omission_problem)
            else:
                metrics["omitted_diagram_count"] += 1
            continue
        typ = normalize_token(exhibit.get("type"))
        appears = normalize_appears_in(exhibit.get("appears_in")) | normalize_appears_in(exhibit.get("output_class"))
        has_evidence = bool(exhibit.get("evidence_refs")) or bool(exhibit.get("source_data_refs")) or not is_placeholder(exhibit.get("evidence_basis"))
        anchor = str(exhibit.get("anchor") or "").strip()
        in_reference = bool(anchor) and anchor in reference_text
        artifact = rel_path(exhibit.get("artifact") or exhibit.get("rendered_artifact") or exhibit.get("source_artifact")).split("#", 1)[0]
        artifact_path, artifact_unsafe = confined_path(root, artifact) if artifact else (root / "__missing_artifact__", None)
        if artifact_unsafe:
            problems.append(f"visual_exhibit_{ex_id}_artifact_unsafe")
        artifact_exists = bool(artifact) and not artifact_unsafe and artifact_path.exists()
        base_valid = (
            not is_placeholder(exhibit.get("id"))
            and not is_placeholder(exhibit.get("title"))
            and typ in VALID_EXHIBIT_TYPES
            and bool(appears)
            and not is_placeholder(exhibit.get("decision_use"))
            and has_evidence
            and (in_reference or artifact_exists)
        )
        if class_present(appears, "reference_pack", "internal_full_pack", "full_pack", "full") and not in_reference and not artifact_exists:
            problems.append(f"visual_exhibit_{ex_id}_reference_pack_anchor_missing")
            base_valid = False
        if typ == "diagram":
            _check_rendered_diagram(root, exhibit, problems)
        if not base_valid:
            problems.append(f"visual_exhibit_{ex_id}_invalid_or_placeholder")
            continue
        metrics["valid_visual_exhibit_count"] += 1
        if class_present(appears, "reference_pack", "internal_full_pack", "full_pack", "full"):
            metrics["full_pack_exhibit_count"] += 1
        if typ == "diagram":
            metrics["diagram_count"] += 1
    return metrics, problems


def load_receipt(root: Path, rel: str) -> tuple[dict | None, str | None]:
    path, unsafe = confined_path(root, rel)
    if unsafe:
        return None, unsafe
    if not path.exists():
        return None, f"qa_receipt_missing {rel}"
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"qa_receipt_invalid {rel} {type(exc).__name__}"


def confined_path(root: Path, rel: str) -> tuple[Path, str | None]:
    rel = rel_path(rel)
    relative = Path(rel)
    if not rel or relative.is_absolute() or ".." in relative.parts:
        return root / "__invalid__", f"unsafe_campaign_path {rel!r}"
    root_resolved = root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return current, f"symlink_campaign_path {rel}"
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError:
        return candidate, f"campaign_path_escape {rel}"
    return candidate, None


def context_holds(root: Path, manifest: dict) -> list[str]:
    holds: list[str] = []
    context = manifest.get("context") if isinstance(manifest.get("context"), dict) else {}
    md_rel = rel_path(context.get("markdown") or "context/context-brief.md")
    json_rel = rel_path(context.get("json") or "context/context-brief.json")
    md_path, md_unsafe = confined_path(root, md_rel)
    json_path, json_unsafe = confined_path(root, json_rel)
    if md_unsafe:
        holds.append(md_unsafe)
    elif not md_path.is_file():
        holds.append(f"context_brief_missing {md_rel}")
    if json_unsafe:
        holds.append(json_unsafe)
        return holds
    if not json_path.is_file():
        holds.append(f"context_brief_missing {json_rel}")
        return holds
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as exc:
        holds.append(f"context_brief_invalid {type(exc).__name__}")
        return holds
    if not isinstance(data, dict) or data.get("schema_version") != "deep-research-context-brief-v1":
        holds.append("context_brief_schema_invalid")
        return holds
    if str(data.get("status") or "").lower() not in {"complete", "pass", "verified"}:
        holds.append(f"context_brief_status {data.get('status')}")
    required = {"source_path", "freshness", "authority", "privacy_classification", "exclusions"}
    items = data.get("items") or []
    if not items and is_placeholder(data.get("no_relevant_context_reason")):
        holds.append("context_brief_empty_without_reason")
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not required.issubset(item):
            holds.append(f"context_brief_item_{index}_incomplete")
            continue
        scalar_fields = ("source_path", "freshness", "authority", "privacy_classification")
        if any(is_placeholder(item.get(field)) for field in scalar_fields):
            holds.append(f"context_brief_item_{index}_placeholder_value")
        exclusions = item.get("exclusions")
        if not isinstance(exclusions, list) or not exclusions or any(is_placeholder(value) for value in exclusions):
            holds.append(f"context_brief_item_{index}_exclusions_incomplete")
    return holds


def qa_holds(root: Path, manifest: dict, deliverable_export_enabled: bool, external_hash: str | None) -> list[str]:
    """QA receipt holds. In layout v2 ``external_hash`` is the reference pack hash and receipts bind ``reference_pack_sha256``."""
    v2 = is_v2(manifest)
    bind_key = "reference_pack_sha256" if v2 else "external_pack_sha256"
    bind_label = "reference_pack" if v2 else "external_pack"
    holds: list[str] = []
    receipts = manifest.get("qa", {}).get("receipts", {}) if isinstance(manifest.get("qa"), dict) else {}
    for name, default_rel in REQUIRED_QA_RECEIPTS.items():
        rel = rel_path(receipts.get(name) or default_rel)
        data, problem = load_receipt(root, rel)
        if problem:
            holds.append(f"qa_receipt_{name} {problem}")
            continue
        data_obj = data or {}
        if data_obj.get("schema_version") != "deep-research-qa-receipt-v1" and name != "final_assurance":
            holds.append(f"qa_receipt_{name} schema_invalid")
        if name == "final_assurance" and data_obj.get("schema_version") not in {"deep-research-qa-receipt-v1", "deep-research-final-assurance-v1"}:
            holds.append("qa_receipt_final_assurance schema_invalid")
        status = str(data_obj.get("status", "")).upper()
        if status in {"N/A", "NA"}:
            if name not in EXPORT_ONLY_RECEIPTS or deliverable_export_enabled or is_placeholder(data_obj.get("reason")):
                holds.append(f"qa_receipt_{name} invalid_na")
            continue
        if status not in PASSING_QA_STATUSES:
            holds.append(f"qa_receipt_{name} status {data_obj.get('status')}")
        reviewer = str(data_obj.get("reviewer") or data_obj.get("reviewer_id") or "").strip()
        raw_producers = data_obj.get("producers")
        producers = {str(p).strip() for p in raw_producers if str(p).strip()} if isinstance(raw_producers, list) else set()
        controller = str(data_obj.get("controller") or "").strip()
        if not reviewer:
            holds.append(f"qa_receipt_{name} reviewer_missing")
        if not producers:
            holds.append(f"qa_receipt_{name} producers_missing")
        if reviewer and (reviewer in producers or reviewer == controller):
            holds.append(f"qa_independence_{name} reviewer_matches_producer")
        if name in {"evidence_privacy", "final_assurance"} and external_hash and data_obj.get(bind_key) and data_obj.get(bind_key) != external_hash:
            holds.append(f"{bind_label}_hash_mismatch_{name}")
        if name in {"evidence_privacy", "final_assurance"} and external_hash and not data_obj.get(bind_key):
            holds.append(f"{bind_label}_hash_missing_{name}")
        if name == "context_handoff":
            checks = data_obj.get("checks") if isinstance(data_obj.get("checks"), dict) else {}
            if checks.get("isolation") not in {"PASS", "pass", True} or checks.get("privacy_classification") not in {"PASS", "pass", True}:
                holds.append("context_handoff_checks_not_pass")
        if name == "evidence_privacy" and data_obj.get("privacy_scan_findings") not in ([], None):
            holds.append("evidence_privacy_findings_present")
        if name == "visual":
            classes = data_obj.get("output_classes") if isinstance(data_obj.get("output_classes"), dict) else {}
            for output_class in (V2_VISUAL_CLASSES if v2 else ("external_full_pack", "internal_full_pack", "internal_executive_brief")):
                if classes.get(output_class) not in {"PASS", "pass", True}:
                    holds.append(f"visual_qa_output_class_{output_class}_not_pass")
        if name == "taxonomy_review":
            invalid_tags = data_obj.get("tags", {}).get("invalid", []) if isinstance(data_obj.get("tags"), dict) else []
            if invalid_tags:
                holds.append(f"export_tags_invalid {','.join(map(str, invalid_tags))}")
            if deliverable_export_enabled and data_obj.get("routing_invariant") not in {"PASS", "pass", True}:
                holds.append("export_routing_invariant_not_pass")
            link_status = data_obj.get("internal_links", {}).get("status") if isinstance(data_obj.get("internal_links"), dict) else None
            if deliverable_export_enabled and link_status not in {"PASS", "pass", "N/A", "NA"}:
                holds.append(f"internal_link_coverage {link_status}")
    return holds


def frontmatter_holds(root: Path, rels: list[str]) -> list[str]:
    holds: list[str] = []
    required = {"title", "created", "type", "stage", "source", "author", "captured_via", "tags"}
    for rel in rels:
        path, unsafe = confined_path(root, rel)
        if unsafe:
            holds.append(unsafe)
            continue
        text = read(path)
        if not text.startswith("---\n"):
            holds.append(f"frontmatter_missing {rel}")
            continue
        end = text.find("\n---\n", 4)
        if end == -1:
            holds.append(f"frontmatter_unclosed {rel}")
            continue
        fm = text[4:end]
        found = {m.group(1) for m in re.finditer(r"^([A-Za-z_][A-Za-z0-9_-]*):", fm, re.M)}
        missing = sorted(required - found)
        if missing:
            holds.append(f"frontmatter_required_missing {rel} {','.join(missing)}")
    return holds


def external_privacy_holds(text: str) -> list[str]:
    hits = [name for name, pattern in PRIVATE_PATTERNS.items() if pattern.search(text)]
    return [f"external_privacy_scan {','.join(hits)}"] if hits else []


def output_paths(manifest: dict) -> tuple[str, str, str]:
    external_rel = manifest_path(manifest, "external_output.path", EXTERNAL_OUTPUT)
    internals = manifest.get("internal_outputs")
    if isinstance(internals, list) and len(internals) >= 2:
        full_rel = rel_path(internals[0].get("path") if isinstance(internals[0], dict) else internals[0]) or INTERNAL_OUTPUTS[0]
        brief_rel = rel_path(internals[1].get("path") if isinstance(internals[1], dict) else internals[1]) or INTERNAL_OUTPUTS[1]
    else:
        full_rel = INTERNAL_OUTPUTS[0]
        brief_rel = INTERNAL_OUTPUTS[1]
    return external_rel, full_rel, brief_rel


def routing_holds(root: Path, manifest: dict, deliverable_export_enabled: bool) -> list[str]:
    holds: list[str] = []
    v2 = is_v2(manifest)
    if v2:
        full_rel, brief_rel = reference_pack_rel(manifest), plain_brief_rel(manifest)
        internals = manifest.get("internal_outputs")
        internal_paths = [rel_path(i.get("path") if isinstance(i, dict) else i) for i in internals] if isinstance(internals, list) else []
        if internal_paths != [REFERENCE_PACK_OUTPUT]:
            holds.append(f"internal_output_paths_invalid {','.join(internal_paths)}")
        checked_paths = (full_rel, brief_rel)
    else:
        external_rel, full_rel, brief_rel = output_paths(manifest)
        if external_rel != EXTERNAL_OUTPUT:
            holds.append(f"external_output_path_invalid {external_rel}")
        if [full_rel, brief_rel] != INTERNAL_OUTPUTS:
            holds.append(f"internal_output_paths_invalid {full_rel},{brief_rel}")
        checked_paths = (external_rel, full_rel, brief_rel)
    raw_ve = manifest.get("deliverable_exports")
    ve: dict = raw_ve if isinstance(raw_ve, dict) else {}
    exportable = deliverable_exportable_outputs(manifest)
    allowlist = [rel_path(item) for item in ve.get("allowlist") or []]
    if allowlist != exportable:
        holds.append("deliverable_export_allowlist_invalid")
    if v2:
        for item in allowlist:
            if item in V2_FORBIDDEN_EXPORTS:
                holds.append(f"deliverable_export_allowlist_forbidden_output {item} is not exportable in the brief-and-reference layout")
    requested = []
    for key, value in ve.items():
        if key.endswith("_staging") or key in {"extra_staging", "staging"}:
            requested.extend(value if isinstance(value, list) else [value])
    requested = [rel_path(item) for item in requested if rel_path(item)]
    if deliverable_export_enabled and requested != exportable:
        holds.append(f"deliverable_export_staging_invalid {','.join(requested)}")
    if deliverable_export_enabled and ve.get("status") == "copied_to_export":
        exports = ve.get("exports")
        if not isinstance(exports, list) or len(exports) != len(exportable):
            holds.append("deliverable_export_readback_missing")
        else:
            staged = [rel_path(item.get("staged_path")) for item in exports if isinstance(item, dict)]
            if staged != exportable:
                holds.append("deliverable_export_readback_paths_invalid")
            for item in exports:
                if not isinstance(item, dict) or not item.get("sha256") or item.get("sha256") != item.get("readback_sha256"):
                    holds.append("deliverable_export_readback_hash_invalid")
                    break
                staged_rel = rel_path(item.get("staged_path"))
                staged_path_obj, staged_unsafe = confined_path(root, staged_rel)
                if staged_unsafe or not staged_path_obj.is_file() or sha256_path(staged_path_obj) != item.get("sha256"):
                    holds.append("deliverable_export_staged_source_hash_mismatch")
                    break
                export_path = Path(str(item.get("export_path") or "")).expanduser()
                export_root_value = str(ve.get("export_root") or "").strip()
                intended = rel_path(ve.get("intended_directory") or ".")
                if not export_root_value or intended != ".":
                    holds.append("deliverable_export_destination_contract_invalid")
                    break
                export_root_raw = Path(export_root_value).expanduser()
                if not export_root_raw.is_absolute() or not export_path.is_absolute():
                    holds.append("deliverable_export_destination_not_absolute")
                    break
                export_root = export_root_raw.resolve(strict=False)
                expected_dir_raw = export_root_raw
                expected_dir = expected_dir_raw.resolve(strict=False)
                if export_root == root or export_root.is_relative_to(root):
                    holds.append("deliverable_export_root_inside_campaign")
                    break
                chain = [export_root_raw, export_path]
                if any(path.exists() and path.is_symlink() for path in chain):
                    holds.append("deliverable_export_destination_symlink")
                    break
                try:
                    resolved_export_path = export_path.resolve(strict=False)
                    resolved_export_path.relative_to(expected_dir)
                    if export_path.parent != expected_dir_raw:
                        raise ValueError("deliverable destination must be directly under export root")
                except ValueError:
                    holds.append("deliverable_export_destination_outside_allowlisted_directory")
                    break
                expected_keys = {
                    "internal/full-research-pack.md": "full_pack_intended_export_path",
                    "internal/executive-brief.md": "board_brief_intended_export_path",
                    plain_brief_rel(manifest): "plain_brief_intended_export_path",
                }
                if v2:
                    expected_keys = {reference_pack_rel(manifest): "reference_pack_intended_export_path", plain_brief_rel(manifest): "plain_brief_intended_export_path"}
                expected_key = expected_keys.get(staged_rel)
                configured_expected = str(ve.get(expected_key) or "").strip() if expected_key else ""
                expected_export_path = Path(configured_expected).expanduser().resolve(strict=False) if configured_expected else (expected_dir_raw / Path(staged_rel).name).resolve(strict=False)
                if resolved_export_path != expected_export_path:
                    holds.append("deliverable_export_destination_path_mismatch")
                    break
                if resolved_export_path == root or resolved_export_path.is_relative_to(root):
                    holds.append("deliverable_export_destination_inside_campaign")
                    break
                if export_path.is_symlink() or not export_path.is_file() or sha256_path(export_path) != item.get("readback_sha256"):
                    holds.append("deliverable_export_destination_readback_invalid")
                    break
    for rel in checked_paths:
        _, unsafe = confined_path(root, rel)
        if unsafe:
            holds.append(unsafe)
    return holds


def evidence_bundle_holds(root: Path, manifest: dict) -> list[str]:
    """gate: verify a declared frozen evidence-bundle-v1 with the
    immediate TOCTOU rehash.

    Backward-compatible: campaigns whose manifest has no
    ``research_orchestration.evidence_bundle`` block are unaffected (returns []).
    When the block is present and required:

      * a ``not_started`` / missing artifact HOLDs;
      * a frozen bundle whose body hash drifted, or any manifest artifact whose
        bytes changed after freeze, HOLDs with ``blocked_stale_hash`` reasons.

    Read-only: reads local JSON + rehashes artifact files; no mutation.
    """
    ro = manifest.get("research_orchestration")
    if not isinstance(ro, dict):
        return []
    eb_meta = ro.get("evidence_bundle")
    if not isinstance(eb_meta, dict):
        return []

    holds: list[str] = []
    if not eb_meta.get("required"):
        return holds
    if eb_meta.get("status") in {None, "not_started"}:
        return ["research_orchestration_evidence_bundle not_started"]

    rel = eb_meta.get("path", "orchestration/evidence-bundle.json")
    path, unsafe = confined_path(root, rel)
    if unsafe:
        return [f"research_orchestration_evidence_bundle {unsafe}"]
    if not path.is_file():
        return [f"research_orchestration_evidence_bundle artifact missing {rel}"]
    try:
        frozen = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [f"research_orchestration_evidence_bundle invalid json {type(exc).__name__}"]

    try:
        import importlib.util

        eb_path = Path(__file__).resolve().parent / "evidence_bundle.py"
        spec = importlib.util.spec_from_file_location("evidence_bundle", eb_path)
        assert spec is not None and spec.loader is not None
        eb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(eb)
    except Exception as exc:  # noqa: BLE001 - fail-closed if the verifier can't load
        return [f"research_orchestration_evidence_bundle verifier_unavailable {type(exc).__name__}"]

    result = eb.verify_frozen_bundle(frozen, root=root)
    if result["status"] != "verified":
        for reason in result.get("reasons", []):
            holds.append(f"research_orchestration_evidence_bundle {result['status']}: {reason}")
    return holds


def research_orchestration_holds(root: Path, manifest: dict) -> list[str]:
    """gate: required prior-work/playbook (and improvement) artifacts.

    Backward-compatible: campaigns whose manifest carries no
    ``research_orchestration`` block are unaffected (returns []). When the block
    is present:

      * a required artifact that is still ``not_started`` / missing HOLDs;
      * a filled required artifact that fails its deterministic evaluator HOLDs
        with the evaluator's own state/reasons;
      * ``automatic_mutation_allowed`` must be false (owner-gate invariant).

    Read-only: only reads local JSON and runs pure evaluators.
    """
    ro = manifest.get("research_orchestration")
    if not isinstance(ro, dict):
        return []

    holds: list[str] = []

    if ro.get("automatic_mutation_allowed") is not False:
        holds.append("research_orchestration_owner_gate automatic_mutation_allowed must be false")

    # Lazy import so the evaluator dependency is only needed when the block exists.
    try:
        import importlib.util

        loop_path = Path(__file__).resolve().parent / "prior_work_playbook_loop.py"
        spec = importlib.util.spec_from_file_location("prior_work_playbook_loop", loop_path)
        assert spec is not None and spec.loader is not None
        plw = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plw)
    except Exception as exc:  # noqa: BLE001 - fail-closed if the evaluator can't load
        return holds + [f"research_orchestration_evaluator_unavailable {type(exc).__name__}"]

    def _load(rel: str) -> tuple[dict | None, str | None]:
        path, unsafe = confined_path(root, rel)
        if unsafe:
            return None, unsafe
        if not path.is_file():
            return None, "artifact missing"
        try:
            return json.loads(path.read_text(encoding="utf-8")), None
        except Exception as exc:  # noqa: BLE001
            return None, f"invalid json {type(exc).__name__}"

    pw_meta = ro.get("prior_work_context", {}) if isinstance(ro.get("prior_work_context"), dict) else {}
    pb_meta = ro.get("playbook", {}) if isinstance(ro.get("playbook"), dict) else {}
    imp_meta = ro.get("improvement_record", {}) if isinstance(ro.get("improvement_record"), dict) else {}

    context_obj: dict | None = None

    # --- prior-work context (required) ---
    if pw_meta.get("required"):
        if pw_meta.get("status") in {None, "not_started"}:
            holds.append("research_orchestration_prior_work not_started")
        else:
            ctx, err = _load(pw_meta.get("path", "orchestration/prior-work-context.json"))
            if err:
                holds.append(f"research_orchestration_prior_work {err}")
            else:
                context_obj = ctx
                verdict = plw.evaluate_prior_work_context(ctx)
                if verdict["status"] != "PASS":
                    holds.append(
                        f"research_orchestration_prior_work {verdict['state']}: "
                        + "; ".join(verdict["reasons"])
                    )

    # --- playbook (required) ---
    if pb_meta.get("required"):
        if pb_meta.get("status") in {None, "not_started"}:
            holds.append("research_orchestration_playbook not_started")
        else:
            pb, err = _load(pb_meta.get("path", "orchestration/implementation-operating-playbook.json"))
            if err:
                holds.append(f"research_orchestration_playbook {err}")
            elif context_obj is None:
                holds.append("research_orchestration_playbook cannot evaluate without a valid prior-work context")
            else:
                verdict = plw.evaluate_playbook(pb, context_obj)
                if verdict["status"] != "PASS":
                    holds.append(
                        f"research_orchestration_playbook {verdict['state']}: "
                        + "; ".join(verdict["reasons"])
                    )

    # --- improvement record (optional; only evaluated when filled) ---
    if imp_meta.get("required") and imp_meta.get("status") in {None, "not_started"}:
        holds.append("research_orchestration_improvement_record not_started")
    elif imp_meta.get("status") not in {None, "not_started"}:
        rec, err = _load(imp_meta.get("path", "orchestration/research-improvement-record.json"))
        if err:
            holds.append(f"research_orchestration_improvement_record {err}")
        else:
            verdict = plw.evaluate_improvement_record(rec)
            if verdict["status"] != "PASS":
                holds.append(
                    f"research_orchestration_improvement_record {verdict['state']}: "
                    + "; ".join(verdict["reasons"])
                )

    return holds


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify deep-research campaign depth")
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--mode", choices=sorted(DEFAULT_THRESHOLDS), default="true_deep_research")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = Path(args.campaign_root).expanduser().resolve()
    manifest_path_obj = root / "manifest.json"
    if manifest_path_obj.is_symlink() or not manifest_path_obj.is_file():
        raise SystemExit(f"manifest not found: {manifest_path_obj}")
    try:
        manifest = json.loads(manifest_path_obj.read_text())
    except json.JSONDecodeError as exc:
        result = {"status": "FAIL", "classification": "invalid_manifest", "metrics": {}, "failures": [f"manifest_invalid_json {type(exc).__name__}"], "holds": [], "thresholds": {}}
        print(json.dumps(result, indent=2) if args.json else f"FAIL invalid_manifest\n{json.dumps(result, indent=2)}")
        return 3
    if not isinstance(manifest, dict):
        result = {"status": "FAIL", "classification": "invalid_manifest", "metrics": {}, "failures": ["manifest_not_object"], "holds": [], "thresholds": {}}
        print(json.dumps(result, indent=2) if args.json else f"FAIL invalid_manifest\n{json.dumps(result, indent=2)}")
        return 3
    mode = args.mode
    thresholds = dict(DEFAULT_THRESHOLDS[mode])
    target_type = str(manifest.get("target_type", "")).lower().strip().replace("_", "-")
    if mode == "true_deep_research" and target_type in PRODUCT_TYPES:
        thresholds["min_sources"] = 25
    if mode == "true_deep_research" and target_type not in STRUCTURAL_TYPES:
        thresholds["min_diagrams"] = 0

    try:
        purpose = load_plain_brief_module()._purpose_module().resolve_report_purpose(manifest, root)
    except ValueError:
        purpose = None  # plain_brief_gate reports the exact invalid-purpose reason
    if purpose == "factual":
        thresholds["min_action_rows"] = 0
    v2 = is_v2(manifest)
    if v2:
        # Layout v2: no external pack, no executive brief. The reference pack takes the "internal full pack" slot.
        external_rel = internal_brief_rel = ""
        internal_full_rel = reference_pack_rel(manifest)
        external_path = internal_brief = root / "__not_in_this_layout__"
        internal_full, internal_full_unsafe = confined_path(root, internal_full_rel)
        if internal_full_unsafe:
            internal_full = root / "__invalid_internal_full__"
        legacy_full = legacy_board = root / "__not_in_this_layout__"
        external_text = internal_brief_text = ""
        internal_full_text = read(internal_full)
        external_hash = frozen_hash = None
        reference_hash = sha256_path(internal_full)
        action_text = read(root / ACTION_REGISTER_SIDECAR)
        thresholds["min_board_exhibits"] = 0
        thresholds["max_board_words"] = 0
        # ISSUE-759 item 8: the reference pack has no length floor. Depth comes from lanes, sources,
        # saved quotes (quote_check.py) and the Action Register, not from word count.
        thresholds["min_full_words"] = 0
    else:
        external_rel, internal_full_rel, internal_brief_rel = output_paths(manifest)
        external_path, external_unsafe = confined_path(root, external_rel)
        internal_full, internal_full_unsafe = confined_path(root, internal_full_rel)
        internal_brief, internal_brief_unsafe = confined_path(root, internal_brief_rel)
        if external_unsafe:
            external_path = root / "__invalid_external__"
        if internal_full_unsafe:
            internal_full = root / "__invalid_internal_full__"
        if internal_brief_unsafe:
            internal_brief = root / "__invalid_internal_brief__"
        legacy_full = root / "synthesis" / "full-pack.md"
        legacy_board = root / "synthesis" / "board-brief.md"
        external_text = read(external_path)
        internal_full_text = read(internal_full) or read(legacy_full)
        internal_brief_text = read(internal_brief) or read(legacy_board)
        external_hash = sha256_path(external_path)
        frozen_hash = manifest.get("external_output", {}).get("frozen_sha256") or manifest.get("external_output", {}).get("sha256") if isinstance(manifest.get("external_output"), dict) else None
        reference_hash = None
        action_text = internal_full_text

    lanes = manifest.get("lanes", [])
    lane_md = list((root / "lanes").glob("*.md"))
    lane_json = list((root / "lanes").glob("*.json"))
    worker_packets = list((root / "workers").glob("*-packet.md"))
    vis_metrics, vis_problems = visual_metrics(root, manifest, internal_full_text, internal_brief_text, external_text)
    preseed_verdict = verify_preseed(root, manifest) if verify_preseed is not None else None
    preseed_holds: list[str] = []
    if preseed_verdict is None:
        if "pre_seed" in manifest:
            preseed_holds.append("pre_seed_verifier_unavailable")
    elif preseed_verdict.status == "LEGACY" and mode == "true_deep_research":
        # Phase 0 is mandatory only for comparative or seeded-selection work.
        # Policy/legal landscape campaigns can explicitly record N/A when they
        # do not compare a product, vendor, architecture, or duration.
        non_applicable = manifest.get("non_applicable_controls", {})
        preseed_na = isinstance(non_applicable, dict) and bool(str(non_applicable.get("pre_seed") or "").strip())
        if not preseed_na:
            preseed_holds.append("pre_seed_required_for_true_deep_research")
    elif preseed_verdict.status == "HOLD":
        preseed_holds.extend(preseed_verdict.holds)
    hist = root / "history" / "relevant-history.json"
    hist_status = "missing"
    if hist.exists():
        try:
            hist_status = json.loads(hist.read_text()).get("status", "unknown")
        except Exception:
            hist_status = "invalid"
    ve = manifest.get("deliverable_exports", {}) if isinstance(manifest.get("deliverable_exports"), dict) else {}
    deliverable_export_enabled = bool(ve.get("enabled"))
    plain_gate = plain_brief_gate(root, manifest)
    quant_metrics: dict[str, int | float] = {}
    quant_holds: list[str] = []
    if v2 and lane_json:
        quant_metrics, quant_holds = quantitative_retention_holds(
            load_lane_findings(lane_json), internal_full_text, target_type=str(manifest.get("target_type") or "")
        )

    if v2:
        metrics = {
            "mode": mode,
            "output_layout": OUTPUT_LAYOUT_V2,
            "lane_count": len(lanes),
            "lane_markdown_count": len(lane_md),
            "lane_json_count": len(lane_json),
            "worker_packet_count": len(worker_packets),
            "source_count_estimate": count_sources(root, internal_full_text),
            "internal_full_pack_words": words(internal_full_text),
            "action_register_rows": action_rows(action_text),
            "internal_full_pack_duplicate_prose_groups": duplicate_prose_metrics(internal_full_text)["duplicate_prose_groups"],
            "internal_full_pack_duplicate_prose_instances": duplicate_prose_metrics(internal_full_text)["duplicate_prose_instances"],
            "history_status": hist_status,
            "deliverable_export_enabled": deliverable_export_enabled,
            "deliverable_export_status": ve.get("status"),
            "internal_full_pack_exists": internal_full.exists(),
            "reference_pack_exists": internal_full.exists(),
            **({} if preseed_verdict is None else preseed_verdict.metrics),
            **vis_metrics,
            **plain_gate["metrics"],
            **quant_metrics,
            "quantitative_screening_warnings": quant_holds,
        }
    else:
        metrics = {
            "mode": mode,
            "lane_count": len(lanes),
            "lane_markdown_count": len(lane_md),
            "lane_json_count": len(lane_json),
            "worker_packet_count": len(worker_packets),
            "source_count_estimate": count_sources(root, internal_full_text, external_text),
            "internal_full_pack_words": words(internal_full_text),
            "external_full_pack_words": words(external_text),
            "board_brief_words": words(internal_brief_text),
            "action_register_rows": action_rows(internal_full_text),
            "internal_full_pack_duplicate_prose_groups": duplicate_prose_metrics(internal_full_text)["duplicate_prose_groups"],
            "internal_full_pack_duplicate_prose_instances": duplicate_prose_metrics(internal_full_text)["duplicate_prose_instances"],
            "board_brief_duplicate_prose_groups": duplicate_prose_metrics(internal_brief_text)["duplicate_prose_groups"],
            "board_brief_duplicate_prose_instances": duplicate_prose_metrics(internal_brief_text)["duplicate_prose_instances"],
            "external_pack_duplicate_prose_groups": duplicate_prose_metrics(external_text)["duplicate_prose_groups"],
            "external_pack_duplicate_prose_instances": duplicate_prose_metrics(external_text)["duplicate_prose_instances"],
            "history_status": hist_status,
            "deliverable_export_enabled": deliverable_export_enabled,
            "deliverable_export_status": ve.get("status"),
            "external_pack_exists": external_path.exists(),
            "internal_full_pack_exists": internal_full.exists() or legacy_full.exists(),
            "board_brief_exists": internal_brief.exists() or legacy_board.exists(),
            "external_pack_hash_stable": bool(external_hash and frozen_hash and external_hash == frozen_hash),
            **({} if preseed_verdict is None else preseed_verdict.metrics),
            **vis_metrics,
            **plain_gate["metrics"],
        }
    # Backward-compatible metric names for older callers.
    metrics["full_pack_words"] = metrics["internal_full_pack_words"]
    metrics["full_pack_exists"] = metrics["internal_full_pack_exists"]

    failures: list[str] = []
    holds: list[str] = []
    if metrics["lane_count"] < thresholds["min_lanes"]:
        failures.append(f"lane_count {metrics['lane_count']} < {thresholds['min_lanes']}")
    if metrics["lane_markdown_count"] < thresholds["min_lane_artifacts"]:
        failures.append(f"lane_artifacts {metrics['lane_markdown_count']} < {thresholds['min_lane_artifacts']}")
    if metrics["worker_packet_count"] < thresholds["min_lane_artifacts"]:
        failures.append(f"worker_packets {metrics['worker_packet_count']} < {thresholds['min_lane_artifacts']}")
    if metrics["source_count_estimate"] < thresholds["min_sources"]:
        holds.append(f"source_count {metrics['source_count_estimate']} < {thresholds['min_sources']}")
    if metrics["internal_full_pack_words"] < thresholds["min_full_words"]:
        holds.append(f"internal_full_pack_words {metrics['internal_full_pack_words']} < {thresholds['min_full_words']}")
    if metrics["action_register_rows"] < thresholds["min_action_rows"]:
        holds.append(f"action_register_rows {metrics['action_register_rows']} < {thresholds['min_action_rows']}")
    if not v2 and thresholds["max_board_words"] and metrics["board_brief_words"] > thresholds["max_board_words"]:
        holds.append(f"board_brief_words {metrics['board_brief_words']} > {thresholds['max_board_words']}")
    # Human-facing plain brief: process vocabulary, citation codes, over-cap length, or a failed
    # editor review are failures; a missing or stale review record is a hold.
    failures.extend(plain_gate["failures"])
    holds.extend(plain_gate["holds"])
    failures.extend(fabricated_receipt_failures(root, manifest))
    if v2 and internal_full_text:
        # The reference pack carries evidence and sources only. Process records live in the sidecars.
        try:
            process_hits = reference_pack_process_records(internal_full_text)
        except Exception as exc:  # noqa: BLE001 - fail closed when the lint cannot load
            process_hits = []
            holds.append(f"reference_pack_lint_unavailable {type(exc).__name__}")
        if process_hits:
            failures.append("reference_pack_process_records " + ", ".join(process_hits))
    if mode == "true_deep_research":
        for output_name in (("internal_full_pack",) if v2 else ("internal_full_pack", "board_brief", "external_pack")):
            groups = metrics[f"{output_name}_duplicate_prose_groups"]
            if groups:
                holds.append(f"{output_name}_duplicate_prose_groups {groups} > 0")
        holds.extend(routing_holds(root, manifest, deliverable_export_enabled))
        holds.extend(context_holds(root, manifest))
        if v2:
            if not internal_full.exists():
                holds.append(f"internal_output_missing {internal_full_rel}")
            if not plain_brief_required(manifest):
                holds.append("plain_brief_required_in_v2_layout")
            holds.extend(qa_holds(root, manifest, deliverable_export_enabled, reference_hash))
            # The hash-bound existing full-pack QA, not lexical matching or number
            # counts, decides semantic retention. Screening warnings stay diagnostic.
            holds.extend(frontmatter_holds(root, [internal_full_rel] + ([plain_brief_rel(manifest)] if plain_gate["required"] and (root / plain_brief_rel(manifest)).is_file() else [])))
        else:
            if not external_path.exists():
                holds.append(f"external_output_missing {external_rel}")
            if not internal_full.exists() and not legacy_full.exists():
                holds.append(f"internal_output_missing {internal_full_rel}")
            if not internal_brief.exists() and not legacy_board.exists():
                holds.append(f"internal_output_missing {internal_brief_rel}")
            if external_hash and frozen_hash and external_hash != frozen_hash:
                holds.append("external_pack_hash_changed")
            elif external_path.exists() and not frozen_hash:
                holds.append("external_pack_frozen_hash_missing")
            holds.extend(external_privacy_holds(external_text))
            if "external_evidence" not in external_text and "## External evidence says" not in external_text:
                holds.append("external_evidence_provenance_label_missing")
            if "## What external evidence says" not in internal_full_text or "## What the owner's existing knowledge and decisions say" not in internal_full_text or "## What follows when those layers are reconciled" not in internal_full_text:
                holds.append("internal_full_pack_layers_missing")
            if "internal_context" not in internal_full_text or "controller_inference" not in internal_full_text:
                holds.append("internal_synthesis_provenance_labels_missing")
            if "Internal reconciliation table" not in internal_full_text:
                holds.append("internal_reconciliation_table_missing")
            holds.extend(qa_holds(root, manifest, deliverable_export_enabled, external_hash))
            holds.extend(frontmatter_holds(root, [internal_full_rel, internal_brief_rel] + ([plain_brief_rel(manifest)] if plain_gate["required"] and (root / plain_brief_rel(manifest)).is_file() else [])))
        holds.extend(research_orchestration_holds(root, manifest))
        holds.extend(evidence_bundle_holds(root, manifest))
        holds.extend(vis_problems)
        if metrics["full_pack_exhibit_count"] < thresholds["min_full_exhibits"]:
            holds.append(f"full_pack_exhibits {metrics['full_pack_exhibit_count']} < {thresholds['min_full_exhibits']}")
        if not v2 and metrics["board_brief_exhibit_count"] < thresholds["min_board_exhibits"]:
            holds.append(f"board_brief_exhibits {metrics['board_brief_exhibit_count']} < {thresholds['min_board_exhibits']}")
        # A disclosed omission satisfies the requirement; a silent gap does not.
        diagrams_accounted = metrics["diagram_count"] + metrics.get("omitted_diagram_count", 0)
        if metrics["topic_requires_diagram"] and diagrams_accounted < thresholds["min_diagrams"]:
            holds.append(f"diagram_count {metrics['diagram_count']} < {thresholds['min_diagrams']}")
    holds.extend(preseed_holds)
    if mode == "true_deep_research" and hist_status in {"missing", "not_started", "invalid"}:
        holds.append(f"history_scan {hist_status}")
    if mode == "true_deep_research" and deliverable_export_enabled and ve.get("status") not in {"copied_to_export"}:
        holds.append(f"deliverable_export_status {ve.get('status')}")

    if failures:
        status = "FAIL"
        if any(f.startswith("receipt_fabricated") for f in failures):
            classification = "receipt_fabricated"
        else:
            if all(f.startswith("plain_brief_") for f in failures):
                classification = "human_output_unreadable"
            elif all(f.startswith("reference_pack_process_records") for f in failures):
                classification = "reference_pack_process_records"
            else:
                classification = "missing_required_lanes"
    elif holds:
        status = "HOLD"
        classification = "memo_depth"
    else:
        status = "PASS"
        classification = mode

    result = {"status": status, "classification": classification, "metrics": metrics, "failures": failures, "holds": holds, "thresholds": thresholds}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{status} {classification}")
        print(json.dumps(result, indent=2))
    return 0 if status == "PASS" else (2 if status == "HOLD" else 3)


if __name__ == "__main__":
    raise SystemExit(main())
