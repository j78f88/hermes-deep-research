#!/usr/bin/env python3
"""every cited lane finding needs a saved, verbatim quote.

A deep-research pack counts as done only when this check passes inside ``complete_campaign.py``.

For each ``lanes/*.json`` finding that cites a source (non-empty ``source_ids``):

* ``quote`` must be present and at least eight characters long;
* ``source_file`` must be a regular file inside the campaign root (no symlinks, no ``..``), given relative
  to the root or to ``workspace/sources/<lane>/``;
* the quote must appear in that file, ignoring case, whitespace, Markdown markup and curly quotes;
* the file must be listed in a ``workspace/sources/*/index.jsonl`` row whose ``sha256`` matches it now.

A finding with no ``source_ids`` is allowed only when it is labelled as inference or a gap
(``evidence_class`` ``analyst_inference``/``unverified``/``unknown``, or ``evidence_label``
``inference``/``gap``). Anything else is an uncited fact and fails.

Campaigns built before this rule (no quotes and no saved-source index anywhere) HOLD as
``quote_check_legacy``: they cannot be shown to meet it.

Usage: python3 quote_check.py --campaign-root ROOT [--write]
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

INFERENCE_CLASSES = {"analyst_inference", "unverified", "unknown"}
INFERENCE_LABELS = {"inference", "gap", "judgement", "judgment", "controller_inference"}
MIN_QUOTE_CHARS = 8  # short structured quotes such as "license=MIT" are real; "yes" is not


def norm(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # Markdown links -> link text
    text = re.sub(r"[*_`>#|\\]", " ", text)                          # Markdown markup
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2014", "-"), ("\u2013", "-"), ("\u00a0", " ")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inside(root: Path, rel: str) -> Path | None:
    """A regular file inside root reached without symlinks or ``..``, or None."""
    rel = (rel or "").strip().replace("\\", "/")
    pure = PurePosixPath(rel)
    if not rel or ".." in pure.parts:
        return None
    candidate = Path(rel) if pure.is_absolute() else root / rel
    try:
        parts = candidate.relative_to(root).parts
    except ValueError:
        # An absolute path from where the campaign was built (it may since have been archived or
        # copied): re-anchor the part after the campaign folder name under this root.
        marker = f"/{root.name}/"
        if not pure.is_absolute() or marker not in rel:
            return None
        candidate = root / rel.split(marker, 1)[1]
        parts = candidate.relative_to(root).parts
    probe = root
    for part in parts:
        probe = probe / part
        if probe.is_symlink():
            return None
    return candidate if candidate.is_file() else None


def resolve_source(root: Path, lane: str, source_file: str) -> Path | None:
    for rel in (source_file, f"workspace/sources/{lane}/{source_file}"):
        found = _inside(root, rel)
        if found is not None:
            return found
    return None


def load_index(root: Path) -> dict[Path, str]:
    """Saved-source index across all lanes: resolved file -> recorded sha256."""
    index: dict[Path, str] = {}
    sources = root / "workspace" / "sources"
    if not sources.is_dir():
        return index
    for idx in sorted(sources.glob("*/index.jsonl")):
        lane = idx.parent.name
        for line in idx.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            path = resolve_source(root, lane, str(row.get("file") or ""))
            if path is not None and isinstance(row.get("sha256"), str):
                index[path] = row["sha256"]
    return index


def lane_files(root: Path) -> list[Path]:
    lanes = root / "lanes"
    return sorted(p for p in lanes.glob("*.json") if p.is_file() and not p.is_symlink()) if lanes.is_dir() else []


def _is_inference(finding: dict) -> bool:
    return (str(finding.get("evidence_class") or "").lower() in INFERENCE_CLASSES
            or str(finding.get("evidence_label") or "").lower() in INFERENCE_LABELS)


def evaluate(root: Path) -> dict:
    root = Path(root).expanduser().resolve()
    index = load_index(root)
    texts: dict[Path, str] = {}
    problems: list[dict] = []
    findings = quoted = inference = 0
    any_quote = False
    lanes = lane_files(root)
    for lane_path in lanes:
        lane = lane_path.stem
        try:
            data = json.loads(lane_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append({"lane": lane, "id": None, "kind": "lane_json_invalid", "detail": str(exc)})
            continue
        for finding in data.get("findings") or [] if isinstance(data, dict) else []:
            if not isinstance(finding, dict):
                continue
            findings += 1
            fid = finding.get("id")
            quote = str(finding.get("quote") or "").strip()
            any_quote = any_quote or bool(quote)
            if not finding.get("source_ids"):
                if _is_inference(finding):
                    inference += 1
                else:
                    problems.append({"lane": lane, "id": fid, "kind": "uncited_fact", "detail": "no source_ids and not labelled inference or gap"})
                continue
            if len(norm(quote)) < MIN_QUOTE_CHARS:
                problems.append({"lane": lane, "id": fid, "kind": "missing_quote", "detail": quote[:80]})
                continue
            path = resolve_source(root, lane, str(finding.get("source_file") or ""))
            if path is None:
                problems.append({"lane": lane, "id": fid, "kind": "missing_source_file", "detail": str(finding.get("source_file") or "")[:160]})
                continue
            recorded = index.get(path)
            if recorded is None:
                problems.append({"lane": lane, "id": fid, "kind": "source_not_indexed", "detail": str(path.relative_to(root))})
                continue
            if _sha(path) != recorded:
                problems.append({"lane": lane, "id": fid, "kind": "source_changed", "detail": str(path.relative_to(root))})
                continue
            if path not in texts:
                texts[path] = norm(path.read_text(encoding="utf-8", errors="ignore"))
            if norm(quote) not in texts[path]:
                problems.append({"lane": lane, "id": fid, "kind": "quote_not_found", "detail": f"{quote[:80]} | {path.relative_to(root)}"})
                continue
            quoted += 1
    base = {"lanes": len(lanes), "findings": findings, "quoted": quoted, "inference_or_gap": inference,
            "indexed_sources": len(index), "problems": problems}
    if not lanes or (not any_quote and not index):
        return {**base, "verdict": "HOLD", "class": "quote_check_legacy",
                "reasons": ["no lane finding carries a saved quote and no saved-source index exists (campaign predates the quote rule)"]}
    if problems:
        kinds: dict[str, int] = {}
        for p in problems:
            kinds[p["kind"]] = kinds.get(p["kind"], 0) + 1
        reasons = [f"{n} {k}" for k, n in sorted(kinds.items())] + [f"{p['lane']}/{p['id']}: {p['kind']} {p['detail']}" for p in problems[:30]]
        return {**base, "verdict": "FAIL", "class": "quote_check_failed", "reasons": reasons}
    return {**base, "verdict": "PASS", "class": "ok", "reasons": []}


def main() -> int:
    ap = argparse.ArgumentParser(description="Saved-quote check for deep-research lane findings")
    ap.add_argument("--campaign-root", required=True)
    ap.add_argument("--write", action="store_true", help="also write receipts/quote-check.json")
    args = ap.parse_args()
    result = evaluate(Path(args.campaign_root))
    if args.write:
        out = Path(args.campaign_root).expanduser().resolve() / "receipts" / "quote-check.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "problems"} | {"problems_shown": result["problems"][:20]}, indent=2))
    return {"PASS": 0, "HOLD": 2}.get(result["verdict"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
