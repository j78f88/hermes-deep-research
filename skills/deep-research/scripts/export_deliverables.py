#!/usr/bin/env python3
"""Copy the allowed internal deep-research exports into the real export.

Reads <campaign-root>/manifest.json, enforces a strict allowlist of internal
outputs, copies only those files to EXPORT_ROOT, updates the
manifest, and appends a verification receipt. It does not git add/commit/push.

The allowlist is the internal full pack and executive brief. Campaigns that
declare a ``plain_brief`` block also export the plain brief, which is
what the owner reads first; it must pass the plain-brief lint and its independent
editor review before anything is copied.

Layout ``brief-and-reference-v2`` (ISSUE-770, the default for new scaffolds) has two
outputs: the allowlist is exactly the reference pack and the plain brief. An
executive brief or an external pack in a v2 allowlist is refused. Any other
``output_layout`` value, or none, is the legacy layout and behaves as before.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

ALLOWED_EXPORTS = ["internal/full-research-pack.md", "internal/executive-brief.md"]
PLAIN_BRIEF_OUTPUT = "internal/plain-brief.md"
OUTPUT_LAYOUT_V2 = "brief-and-reference-v2"
REFERENCE_PACK_OUTPUT = "internal/reference-pack.md"
V2_FORBIDDEN_EXPORTS = ("internal/executive-brief.md", "internal/full-research-pack.md", "external/original-full-research-pack.md")
V2_VISUAL_CLASSES = ("reference_pack", "plain_brief")
DENIED_PREFIXES = ("external/", "context/", "qa/", "lanes/", "receipts/", "raw/", "transcripts/")
DENIED_EXACT = {"manifest.json", "citations.json", "lane_plan.json", "steering.md"}
EXPORT_KEYS = ("full_pack_staging", "board_brief_staging")
REQUIRED_QA = {
    "context_handoff": "qa/context-handoff-review.json",
    "evidence_privacy": "qa/evidence-privacy-review.json",
    "visual": "qa/visual-review.json",
    "taxonomy_review": "qa/taxonomy-review.json",
    "librarian_repair": "qa/librarian-repair-receipt.json",
    "final_assurance": "qa/final-assurance.json",
}
PRIVATE_PATTERNS = {
    "export_wikilink": re.compile(r"\[\[[^\]]+\]\]"),
    # Keep issue-key detection case-sensitive; ordinary lowercase URL slugs
    # such as `chapter-1`, `app-5`, and `cps-230` are not Linear IDs.
    # Match the depth verifier: public model names and developer docs are not private issue references.
    "linear_id": re.compile(r"\b(?!(?:GPT|GLM)[-\u2010-\u2015])[A-Z]{2,10}[-\u2010-\u2015]\d+\b|(?i:linear\.app/(?!developers(?:/|\b)))"),
    "session_ref": re.compile(r"@session:|\bsession[_ -]?(?:id|ref|excerpt)\b", re.I),
    "private_path": re.compile(r"/(?:home/[^/\s]+|Users/[^/\s]+|mnt/(?:hermes|archive))[^\s)]*", re.I),
    "internal_hostname": re.compile(r"\b(?:localhost|127\.0\.0\.1|host\.docker\.internal|\.local\b|internal\.)", re.I),
    "owner_specific": re.compile(r"\b(?:the owner-stack|future the owner|the owner's|the owner\s+specific)\b", re.I),
    "context_brief": re.compile(r"context(?:/|\\|\s+|_|-)+brief(?:\.md|\.json)?|internal(?:\s+|_|-)+context|taxonomy|librarian", re.I),
}


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(root: Path) -> dict:
    path = root / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"manifest invalid JSON: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise SystemExit("manifest must be a JSON object")
    return data


def normalize_rel(value: object) -> str:
    return str(value or "").strip().replace("\\", "/")


def confined_path(root: Path, rel: str, *, must_exist: bool = False) -> Path:
    """Resolve a manifest-controlled relative path without symlink escapes."""
    rel = normalize_rel(rel)
    relative = Path(rel)
    if not rel or relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe relative path: {rel!r}")
    root_resolved = root.resolve()
    candidate = root / relative
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink not allowed in export path: {rel}")
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path escapes campaign root: {rel}") from exc
    if must_exist and not candidate.is_file():
        raise ValueError(f"staged export missing or not a file: {rel}")
    return candidate


def load_json_confined(root: Path, rel: str) -> dict:
    path = confined_path(root, rel, must_exist=True)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object required: {rel}")
    return data


def is_v2(manifest: dict) -> bool:
    return isinstance(manifest, dict) and manifest.get("output_layout") == OUTPUT_LAYOUT_V2


def plain_brief_rel(manifest: dict) -> str | None:
    """Relative path of the required plain brief, or None for legacy campaigns."""
    pb = manifest.get("plain_brief")
    if not isinstance(pb, dict) or pb.get("required", True) is False:
        return None
    return normalize_rel(pb.get("path")) or PLAIN_BRIEF_OUTPUT


def allowed_exports(manifest: dict) -> list[str]:
    plain = plain_brief_rel(manifest)
    if is_v2(manifest):
        if not plain:
            raise SystemExit("the brief-and-reference layout needs a required plain brief declaration")
        return [REFERENCE_PACK_OUTPUT, plain]
    return ALLOWED_EXPORTS + ([plain] if plain else [])


def validate_plain_brief(root: Path, manifest: dict) -> None:
    """Fail closed unless the plain brief passes lint and a fresh independent editor review."""
    from research_qa_repair import campaign_readiness
    from report_purpose import resolve_report_purpose

    purpose = resolve_report_purpose(manifest, root)
    raw_pb = manifest.get("plain_brief")
    pb = raw_pb if isinstance(raw_pb, dict) else {}
    review_rel = pb.get("review") or "qa/plain-brief-review.json"
    readiness = campaign_readiness(root, review_rel=review_rel)
    if not readiness["publication_qa_allowed"]:
        raise ValueError("campaign readiness: " + "; ".join(readiness["findings"]))
    if plain_brief_rel(manifest) is None:
        if purpose != "legacy":
            raise ValueError("only frozen legacy campaigns may omit the required plain brief")
        return
    import importlib.util

    path = Path(__file__).resolve().parent / "plain_brief_rewrite.py"
    spec = importlib.util.spec_from_file_location("plain_brief_rewrite", path)
    if spec is None or spec.loader is None:
        raise ValueError("plain brief lint unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gate = module.verify_campaign_plain_brief(root, manifest)
    problems = gate["failures"] + gate["holds"]
    if problems:
        raise ValueError("plain brief not ready for export: " + "; ".join(problems))


def validate_receipt_integrity(root: Path, manifest: dict | None = None) -> None:
    """Require fresh provider verification, not a writable saved PASS, before delivery."""
    import importlib.util

    path = Path(__file__).resolve().parent / "receipt_integrity.py"
    spec = importlib.util.spec_from_file_location("receipt_integrity", path)
    if spec is None or spec.loader is None:
        raise ValueError("receipt_fabricated check unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    findings = module.sweep_qa_dir(root)
    if findings:
        raise ValueError("receipt_fabricated: " + "; ".join(findings))
    result = module.verify_campaign_provenance(root, manifest if manifest is not None else load_manifest(root), online=True)
    if result["verdict"] != "PASS":
        raise ValueError(f"{result['verdict']} {result['class']}: " + "; ".join(result["reasons"]))


def validate_qa(root: Path, manifest: dict) -> None:
    receipts = (manifest.get("qa") or {}).get("receipts") or {}
    v2 = is_v2(manifest)
    if v2:
        # Layout v2: receipts bind the reference pack; there is no external pack or freeze.
        reference_path = confined_path(root, REFERENCE_PACK_OUTPUT, must_exist=True)
        frozen_hash = sha256_path(reference_path)
        bind_key, bind_label = "reference_pack_sha256", "reference pack"
    else:
        external = manifest.get("external_output") or {}
        frozen_hash = str(external.get("frozen_sha256") or external.get("sha256") or "")
        external_path = confined_path(root, str(external.get("path") or "external/original-full-research-pack.md"), must_exist=True)
        if not frozen_hash or sha256_path(external_path) != frozen_hash:
            raise ValueError("external pack is not frozen at the manifest SHA-256")
        external_text = external_path.read_text(encoding="utf-8", errors="strict")
        privacy_hits = sorted(name for name, pattern in PRIVATE_PATTERNS.items() if pattern.search(external_text))
        if privacy_hits:
            raise ValueError(f"external pack privacy scan failed: {','.join(privacy_hits)}")
        bind_key, bind_label = "external_pack_sha256", "external"
    for name, default_rel in REQUIRED_QA.items():
        rel = normalize_rel(receipts.get(name) or default_rel)
        data = load_json_confined(root, rel)
        valid_schemas = {"deep-research-qa-receipt-v1"}
        if name == "final_assurance":
            valid_schemas.add("deep-research-final-assurance-v1")
        if data.get("schema_version") not in valid_schemas:
            raise ValueError(f"QA receipt schema invalid: {name}")
        if str(data.get("status") or "").upper() not in {"PASS", "PASSED"}:
            raise ValueError(f"QA receipt is not PASS: {name}")
        reviewer = str(data.get("reviewer") or data.get("reviewer_id") or "").strip()
        if not reviewer:
            raise ValueError(f"QA receipt reviewer missing: {name}")
        if not isinstance(data.get("producers"), list) or not data["producers"]:
            raise ValueError(f"QA receipt producers missing: {name}")
        producers = {str(item).strip() for item in data["producers"] if str(item).strip()}
        controller = str(data.get("controller") or "").strip()
        if reviewer in producers or (controller and reviewer == controller):
            raise ValueError(f"QA receipt reviewer is not independent: {name}")
        if name in {"evidence_privacy", "final_assurance"} and data.get(bind_key) != frozen_hash:
            raise ValueError(f"QA receipt {bind_label} hash mismatch: {name}")
        if name == "context_handoff":
            checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
            if checks.get("isolation") not in {"PASS", "pass", True} or checks.get("privacy_classification") not in {"PASS", "pass", True}:
                raise ValueError("Context handoff isolation/privacy checks are not PASS")
        if name == "evidence_privacy" and data.get("privacy_scan_findings") not in ([], None):
            raise ValueError("Evidence/privacy review contains unresolved findings")
        if name == "visual":
            classes = data.get("output_classes") if isinstance(data.get("output_classes"), dict) else {}
            required_classes = set(V2_VISUAL_CLASSES) if v2 else {"external_full_pack", "internal_full_pack", "internal_executive_brief"}
            if any(classes.get(item) not in {"PASS", "pass", True} for item in required_classes):
                raise ValueError("Visual QA output classes are incomplete or not PASS")
    taxonomy = load_json_confined(root, normalize_rel(receipts.get("taxonomy_review") or REQUIRED_QA["taxonomy_review"]))
    if taxonomy.get("routing_invariant") not in {"PASS", "pass", True}:
        raise ValueError("Taxonomy reviewer routing invariant is not PASS")
    reviewed = [normalize_rel(item) for item in taxonomy.get("reviewed_paths") or []]
    if v2:
        if reviewed != [REFERENCE_PACK_OUTPUT, plain_brief_rel(manifest)]:
            raise ValueError("Taxonomy reviewer must review exactly the reference pack and the plain brief")
    elif reviewed != ALLOWED_EXPORTS:
        raise ValueError("Taxonomy reviewer must review exactly the two internal deliverable exports")
    invalid_tags = taxonomy.get("tags", {}).get("invalid", []) if isinstance(taxonomy.get("tags"), dict) else []
    if invalid_tags:
        raise ValueError("Taxonomy reviewer reported off-vocabulary tags")
    link_status = taxonomy.get("internal_links", {}).get("status") if isinstance(taxonomy.get("internal_links"), dict) else None
    if link_status not in {"PASS", "pass", True}:
        raise ValueError("Taxonomy reviewer internal link review is not PASS")


def validate_context(root: Path, manifest: dict) -> None:
    raw_context = manifest.get("context")
    context: dict = raw_context if isinstance(raw_context, dict) else {}
    markdown_rel = normalize_rel(context.get("markdown") or "context/context-brief.md")
    json_rel = normalize_rel(context.get("json") or "context/context-brief.json")
    confined_path(root, markdown_rel, must_exist=True)
    data = load_json_confined(root, json_rel)
    if data.get("schema_version") != "deep-research-context-brief-v1":
        raise ValueError("Context Brief schema is invalid")
    if str(data.get("status") or "").lower() not in {"complete", "pass", "verified"}:
        raise ValueError("Context Brief is not complete")
    required = {"source_path", "freshness", "authority", "privacy_classification", "exclusions"}
    items = data.get("items") or []
    if not items and not str(data.get("no_relevant_context_reason") or "").strip():
        raise ValueError("Context Brief is empty without a reason")
    for item in items:
        if not isinstance(item, dict) or not required.issubset(item):
            raise ValueError("Context Brief item is incomplete")
        for field in ("source_path", "freshness", "authority", "privacy_classification"):
            value = str(item.get(field) or "").strip()
            if not value or re.match(r"^(?:TBD|TODO|N/?A)(?:\s|:|$)", value, re.I):
                raise ValueError(f"Context Brief item has placeholder {field}")
        exclusions = item.get("exclusions")
        if not isinstance(exclusions, list) or not exclusions:
            raise ValueError("Context Brief item exclusions are incomplete")
        for value in exclusions:
            text = str(value or "").strip()
            if not text or re.match(r"^(?:TBD|TODO|N/?A)(?:\s|:|$)", text, re.I):
                raise ValueError("Context Brief item exclusions contain placeholders")


def staged_values(ve: dict) -> list[str]:
    values: list[str] = []
    for key, value in ve.items():
        if key.endswith("_staging") or key in {"extra_staging", "staging"}:
            if isinstance(value, list):
                values.extend(normalize_rel(v) for v in value)
            else:
                values.append(normalize_rel(value))
    return [v for v in values if v]


def validate_export_plan(ve: dict, allowed: list[str] | None = None) -> list[str]:
    allowed = list(allowed or ALLOWED_EXPORTS)
    raw_allowlist = ve.get("allowlist")
    if not isinstance(raw_allowlist, list) or not raw_allowlist:
        raise SystemExit("deliverable export allowlist is required")
    allowlist = [normalize_rel(v) for v in raw_allowlist]
    if allowed[0] == REFERENCE_PACK_OUTPUT:
        forbidden = [item for item in allowlist if item in V2_FORBIDDEN_EXPORTS]
        if forbidden:
            raise SystemExit(f"{', '.join(forbidden)} is not exportable: the executive brief and external pack are not outputs of the brief-and-reference layout")
        if allowlist != allowed:
            raise SystemExit("deliverable export allowlist must be exactly the reference pack and the plain brief")
    if allowlist != allowed:
        if len(allowed) == 2:
            raise SystemExit("deliverable export allowlist must be exactly the two internal outputs")
        raise SystemExit("deliverable export allowlist must be exactly the two internal outputs plus the plain brief")
    requested = staged_values(ve)
    problems: list[str] = []
    for rel in requested:
        if rel not in allowed:
            problems.append(f"{rel} not in deliverable export allowlist")
        if rel in DENIED_EXACT or rel.startswith(DENIED_PREFIXES):
            problems.append(f"{rel} is denied for deliverable export")
    if problems:
        raise SystemExit("; ".join(problems))
    for required in allowed:
        if required not in requested:
            problems.append(f"required internal export missing from plan: {required}")
    if problems:
        raise SystemExit("; ".join(problems))
    return allowed[:]


def archive_prior_export_note(root: Path, dest: Path) -> dict:
    """Move an existing export note into the campaign's prior-deliverable-exports archive and return its record."""
    if dest.is_symlink() or not dest.is_file():
        raise SystemExit(f"prior export note is not a regular file: {dest}")
    archive_dir = root / "orchestration" / "prior-deliverable-exports"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / dest.name
    counter = 1
    while archive_path.exists() or archive_path.is_symlink():
        archive_path = archive_dir / f"{dest.stem}.{counter}{dest.suffix}"
        counter += 1
    prior_sha = sha256_path(dest)
    prior_bytes = dest.stat().st_size
    shutil.copy2(dest, archive_path)
    if sha256_path(archive_path) != prior_sha:
        archive_path.unlink()
        raise SystemExit(f"archive hash mismatch for prior export note: {dest}")
    dest.unlink()
    return {
        "export_path": str(dest),
        "archived_path": str(archive_path.relative_to(root)),
        "sha256": prior_sha,
        "bytes": prior_bytes,
        "archived_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_destination_available(dest: Path, *, overwrite_approved: bool = False) -> None:
    """Fail closed rather than overwriting an existing export note unless overwrite is approved."""
    if dest.is_symlink():
        raise SystemExit(f"export destination symlink is not allowed: {dest}")
    if dest.exists() and not overwrite_approved:
        raise SystemExit(f"export destination already exists; owner approval required before overwrite: {dest}")


def copy_one(root: Path, source_rel: str, dest: Path) -> dict:
    try:
        src = confined_path(root, source_rel, must_exist=True)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    validate_destination_available(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_hash = sha256_path(src)
    try:
        with src.open("rb") as source, dest.open("xb") as target:
            shutil.copyfileobj(source, target)
        shutil.copystat(src, dest, follow_symlinks=False)
    except FileExistsError as exc:
        raise SystemExit(f"export destination already exists; owner approval required before overwrite: {dest}") from exc
    except Exception as exc:
        raise SystemExit(
            f"deliverable export copy failed after exclusive create; inspect partial destination before retry: {dest}: {type(exc).__name__}: {exc}"
        ) from exc
    dest_hash = sha256_path(dest)
    readback_hash = sha256_path(dest)
    if src_hash != dest_hash or src_hash != readback_hash:
        raise SystemExit(f"hash mismatch after copy: {src} -> {dest}")
    return {
        "staged_path": source_rel,
        "export_path": str(dest),
        "bytes": dest.stat().st_size,
        "sha256": src_hash,
        "readback_sha256": readback_hash,
    }


def load_allowed_tags(export_plan: dict) -> set[str] | None:
    """Use an optional reviewed tag policy, never a destination-side registry."""
    tags = export_plan.get("allowed_tags")
    if tags is None:
        return None
    if not isinstance(tags, list) or not tags or any(not isinstance(tag, str) or not tag.strip() for tag in tags):
        raise ValueError("allowed_tags must be a nonempty list of tag names when configured")
    return set(tags)


def declared_tags(frontmatter: str) -> set[str]:
    """Read simple inline/block tag lists and reject unsupported syntax."""
    match = re.search(r"^tags:([^\n]*(?:\n[ \t]+[^\n]*)*)", frontmatter, re.M)
    value = match.group(1).strip() if match else ""
    if value.startswith("[") and value.endswith("]"):
        items = value[1:-1].split(",") if value[1:-1].strip() else []
    elif value and all(re.fullmatch(r"\s*-\s+.+", line) for line in value.splitlines()):
        items = [re.sub(r"^\s*-\s+", "", line) for line in value.splitlines()]
    else:
        raise ValueError("tags must be an inline list or indented block list")
    tags: set[str] = set()
    for item in items:
        tag = item.strip()
        if len(tag) >= 2 and tag[0] == tag[-1] and tag[0] in "\"'":
            tag = tag[1:-1]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_/-]*", tag):
            raise ValueError("unsupported tag syntax; use simple tag names")
        tags.add(tag)
    return tags


def validate_internal_note(path: Path, allowed_tags: set[str] | None = None,
                           *, companion_stems: set[str] | None = None) -> None:
    text = path.read_text(encoding="utf-8", errors="strict")
    if not text.startswith("---\n"):
        raise ValueError(f"frontmatter missing: {path.name}")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"frontmatter unclosed: {path.name}")
    frontmatter = text[4:end]
    required = {"title", "created", "type", "stage", "source", "author", "captured_via", "tags", "related"}
    found = {match.group(1) for match in re.finditer(r"^([A-Za-z_][A-Za-z0-9_-]*):", frontmatter, re.M)}
    missing = sorted(required - found)
    if missing:
        raise ValueError(f"frontmatter fields missing from {path.name}: {','.join(missing)}")
    tags = declared_tags(frontmatter)
    invalid = sorted(tags - allowed_tags) if allowed_tags is not None else []
    if invalid:
        raise ValueError(f"off-vocabulary tags in {path.name}: {','.join(invalid)}")
    links = {target.strip() for target in re.findall(r"\[\[([^\]|#]+)", text)}
    if not links or (companion_stems is not None and not links.intersection(companion_stems)):
        raise ValueError(f"companion deliverable link missing in {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export deep-research internal export notes")
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--export-root", default=None, help="Override manifest export root")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--overwrite-approved",
        action="store_true",
        default=False,
        help="Owner approved replacing existing export notes; prior notes are archived under orchestration/prior-deliverable-exports/",
    )
    args = parser.parse_args()

    root = Path(args.campaign_root).expanduser().resolve()
    manifest = load_manifest(root)
    ve = manifest.get("deliverable_exports") or {}
    if not ve.get("enabled"):
        print(json.dumps({"ok": False, "status": ve.get("status", "deliverable_export_disabled")}, indent=2))
        return 2

    try:
        validate_receipt_integrity(root, manifest)
        export_rels = validate_export_plan(ve, allowed_exports(manifest))
        validate_context(root, manifest)
        validate_qa(root, manifest)
        validate_plain_brief(root, manifest)
    except (SystemExit, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), flush=True)
        return 3

    admitted_root_raw = Path(ve.get("export_root") or os.environ.get("DELIVERABLES_EXPORT_PATH", "~/research-deliverables")).expanduser().absolute()
    requested_root_raw = Path(args.export_root).expanduser().absolute() if args.export_root else admitted_root_raw
    if any(path.is_symlink() for candidate in (admitted_root_raw, requested_root_raw)
           for path in (candidate, *candidate.parents)):
        print("export root symlink is not allowed", flush=True)
        return 3
    admitted_export_root = admitted_root_raw.resolve()
    export_root = requested_root_raw.resolve()
    if export_root != admitted_export_root:
        print("export root override differs from the accepted manifest destination", flush=True)
        return 3
    if export_root == root or export_root.is_relative_to(root):
        print("export root must be outside the campaign root", flush=True)
        return 3
    intended = normalize_rel(ve.get("intended_directory") or ".")
    if intended != ".":
        print("export intended_directory must be exactly .", flush=True)
        return 3
    target_dir = export_root
    try:
        target_dir.resolve(strict=False).relative_to(export_root)
    except ValueError:
        print("export destination escapes export root", flush=True)
        return 3
    if export_root.is_symlink():
        print(f"export destination symlink is not allowed: {export_root}", flush=True)
        return 3
    try:
        allowed_tags = load_allowed_tags(ve)
    except ValueError as exc:
        print(str(exc), flush=True)
        return 3

    destination_keys = {
        "internal/full-research-pack.md": "full_pack_intended_export_path",
        "internal/executive-brief.md": "board_brief_intended_export_path",
    }
    if is_v2(manifest):
        destination_keys = {REFERENCE_PACK_OUTPUT: "reference_pack_intended_export_path"}
    if plain_brief_rel(manifest):
        destination_keys[plain_brief_rel(manifest)] = "plain_brief_intended_export_path"
    planned_exports = []
    for rel in export_rels:
        configured = str(ve.get(destination_keys[rel]) or "").strip()
        dest = Path(configured).expanduser() if configured else target_dir / Path(rel).name
        if not dest.is_absolute():
            print(f"export destination must be absolute: {dest}", flush=True)
            return 3
        if dest.parent != target_dir or dest.parent.resolve(strict=False) != target_dir.resolve(strict=False):
            print(f"export destination must be directly under export root: {dest}", flush=True)
            return 3
        planned_exports.append((rel, dest))
    try:
        for rel, dest in planned_exports:
            companions = {other.stem for _, other in planned_exports if other != dest}
            validate_internal_note(confined_path(root, rel, must_exist=True), allowed_tags,
                                   companion_stems=companions)
    except ValueError as exc:
        print(str(exc), flush=True)
        return 3
    try:
        for _, dest in planned_exports:
            validate_destination_available(dest, overwrite_approved=args.overwrite_approved)
            if dest.exists() and not dest.is_file():
                raise SystemExit(f"export destination is not a regular file: {dest}")
    except SystemExit as exc:
        print(str(exc), flush=True)
        return 3

    exports = []
    prior_exports: list[dict] = []
    for rel, dest in planned_exports:
        if args.overwrite_approved and not args.dry_run and dest.exists():
            try:
                prior_exports.append(archive_prior_export_note(root, dest))
            except SystemExit as exc:
                print(str(exc), flush=True)
                return 3
        if args.dry_run:
            try:
                src = confined_path(root, rel, must_exist=True)
            except ValueError as exc:
                print(str(exc), flush=True)
                return 3
            exports.append({
                "staged_path": rel,
                "export_path": str(dest),
                "exists": src.exists(),
                "would_copy": True,
                "sha256": sha256_path(src) if src.exists() else None,
            })
        else:
            exports.append(copy_one(root, rel, dest))

    result = {
        "ok": True,
        "dry_run": bool(args.dry_run),
        "campaign_root": str(root),
        "export_root": str(export_root),
        "exports": exports,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    if prior_exports:
        result["prior_exports"] = prior_exports

    if not args.dry_run:
        manifest["deliverable_exports"]["status"] = "copied_to_export"
        manifest["deliverable_exports"]["exported_at"] = result["exported_at"]
        manifest["deliverable_exports"]["exports"] = exports
        if prior_exports:
            existing_priors = manifest["deliverable_exports"].get("prior_exports")
            existing_priors = existing_priors if isinstance(existing_priors, list) else []
            manifest["deliverable_exports"]["prior_exports"] = existing_priors + prior_exports
            manifest["deliverable_exports"]["overwrite_approved"] = True
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        receipt = root / "receipts" / "verification.txt"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("a", encoding="utf-8") as f:
            f.write("deliverable_export_status=copied_to_export\n")
            f.write("deliverable_export_allowlist=" + ",".join(export_rels) + "\n")
            for item in exports:
                f.write(f"deliverable_export={item['export_path']} sha256={item['sha256']} readback_sha256={item['readback_sha256']} bytes={item['bytes']}\n")

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
