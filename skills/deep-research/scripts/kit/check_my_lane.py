#!/usr/bin/env python3
"""Check ONE lane: its JSON against workers/lane-output-schema.json, and every saved quote (kit, DR-897).

Usage: python3 workers/check_my_lane.py LANE_ID

Reads only lanes/LANE_ID.json and workspace/sources/LANE_ID/ under the campaign root (the parent of
workers/), never another lane's files. The quote rule matches scripts/quote_check.py: a finding with
source_ids needs a verbatim quote from a source_file listed in that lane's index.jsonl (key "file")
with a matching sha256; a finding without source_ids must be labelled analyst_inference, unverified or
unknown. Exit 0 when the schema is clean, every quote checks and at least one finding is quoted.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

INFERENCE_CLASSES = {"analyst_inference", "unverified", "unknown"}
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def norm(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[*_`>#|\\]", " ", text)
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2014", "-"),
                 ("\u2013", "-"), ("\u00a0", " ")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def schema_errors(data: dict, schema: dict) -> list[str]:
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema is not installed; schema not checked"]
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message[:160]}"
            for e in jsonschema.Draft202012Validator(schema).iter_errors(data)]


def check_lane(root: Path, lane: str) -> dict:
    schema = json.loads((root / "workers" / "lane-output-schema.json").read_text(encoding="utf-8"))
    lane_path = root / "lanes" / f"{lane}.json"
    try:
        data = json.loads(lane_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"ok": False, "lane": lane, "findings": 0, "quoted": 0, "inference_or_gap": 0,
                "schema_errors": [f"lane JSON unreadable: {type(exc).__name__}"], "quote_problems": []}
    errors = schema_errors(data, schema)
    folder = root / "workspace" / "sources" / lane
    index: dict[Path, str] = {}
    idx = folder / "index.jsonl"
    if idx.is_file():
        for line in idx.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            path = folder / str(row.get("file") or "")
            if path.is_file() and isinstance(row.get("sha256"), str):
                index[path.resolve()] = row["sha256"]
    texts: dict[Path, str] = {}
    problems = []
    findings = quoted = inference = 0
    for finding in data.get("findings") or []:
        findings += 1
        fid = finding.get("id")
        if not finding.get("source_ids"):
            if str(finding.get("evidence_class") or "").lower() in INFERENCE_CLASSES:
                inference += 1
            else:
                problems.append({"id": fid, "kind": "uncited_fact",
                                 "detail": "no source_ids and not labelled analyst_inference/unverified/unknown"})
            continue
        quote = str(finding.get("quote") or "").strip()
        if len(norm(quote)) < 8:
            problems.append({"id": fid, "kind": "missing_quote", "detail": quote[:80]})
            continue
        source_file = str(finding.get("source_file") or "")
        candidate = root / source_file if source_file.startswith("workspace/") else folder / source_file
        try:
            resolved = candidate.resolve()
            resolved.relative_to(folder.resolve())
        except (OSError, ValueError):
            problems.append({"id": fid, "kind": "source_outside_lane", "detail": source_file})
            continue
        if not resolved.is_file():
            problems.append({"id": fid, "kind": "missing_source_file", "detail": source_file})
            continue
        recorded = index.get(resolved)
        if not recorded:
            problems.append({"id": fid, "kind": "source_not_indexed", "detail": source_file})
            continue
        if hashlib.sha256(resolved.read_bytes()).hexdigest() != recorded:
            problems.append({"id": fid, "kind": "source_hash_mismatch", "detail": source_file})
            continue
        if resolved not in texts:
            texts[resolved] = norm(resolved.read_text(encoding="utf-8", errors="ignore"))
        if norm(quote) not in texts[resolved]:
            problems.append({"id": fid, "kind": "quote_not_in_source", "detail": quote[:120], "file": source_file})
            continue
        quoted += 1
    return {"ok": not errors and not problems and quoted > 0, "lane": lane, "findings": findings,
            "quoted": quoted, "inference_or_gap": inference, "schema_errors": errors[:10],
            "quote_problems": problems[:15]}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or not ID_RE.match(args[0]):
        print(json.dumps({"ok": False, "error": "usage: check_my_lane.py LANE_ID"}))
        return 2
    result = check_lane(Path(__file__).resolve().parent.parent, args[0])
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
