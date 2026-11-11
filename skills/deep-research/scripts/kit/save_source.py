#!/usr/bin/env python3
"""Save a public web page as text for one lane and index it (deep-research kit).

Usage: python3 workers/save_source.py LANE_ID SOURCE_ID URL [--direct-only]

The driver copies this file into <campaign root>/workers/. It writes
workspace/sources/LANE_ID/SOURCE_ID.md under the campaign root (the parent of workers/) and appends one
row to that folder's index.jsonl with the keys quote_check.py reads: file, source_id, url, method,
sha256, chars, fetched_at. Never edit a saved file afterwards: its sha256 is bound in the index.

Fetch order: the r.jina.ai reader, then a direct curl (PDFs through pdftotext), then the Wayback copy
through the reader. --direct-only skips the reader and Wayback (offline tests, local URLs).
"""
from __future__ import annotations

import datetime
import hashlib
import html
import json
import re
import subprocess
import sys
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def campaign_root() -> Path:
    return Path(__file__).resolve().parent.parent


def curl(url: str, timeout: int) -> tuple[str, bytes]:
    proc = subprocess.run(["curl", "-sL", "-m", str(timeout), "-A", "Mozilla/5.0", "-w", "\n__HTTP__%{http_code}", url],
                          capture_output=True)
    out = proc.stdout
    if b"\n__HTTP__" not in out:
        return "000", b""
    body, code = out.rsplit(b"\n__HTTP__", 1)
    return code.strip().decode("ascii", "ignore") or "000", body


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text))


def fetch(url: str, direct_only: bool = False) -> tuple[str | None, str, str]:
    """Return (method, text, last_http_code)."""
    if not direct_only:
        code, body = curl("https://r.jina.ai/" + url, 90)
        text = body.decode("utf-8", "ignore")
        if code == "200" and len(text) > 500 and "Just a moment" not in text[:600]:
            return "r.jina.ai", text, code
    code, body = curl(url, 60)
    if code == "200" and len(body) > 80:
        if body.startswith(b"%PDF"):
            proc = subprocess.run(["pdftotext", "-", "-"], input=body, capture_output=True)
            return "direct-pdftotext", proc.stdout.decode("utf-8", "ignore"), code
        decoded = body.decode("utf-8", "ignore")
        return "direct", (strip_html(decoded) if "<html" in decoded[:2000].lower() else decoded), code
    if not direct_only:
        code_w, body_w = curl("https://r.jina.ai/https://web.archive.org/web/2026/" + url, 90)
        text = body_w.decode("utf-8", "ignore")
        if code_w == "200" and len(text) > 500:
            return "wayback-via-jina", text, code_w
    return None, "", code


def save(root: Path, lane: str, sid: str, url: str, method: str, text: str) -> dict:
    folder = root / "workspace" / "sources" / lane
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.md"
    if path.exists():
        raise FileExistsError(f"{lane}/{sid}.md already exists; use a new source id")
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path.write_text(f"<!-- url: {url} | method: {method} | fetched: {stamp} -->\n" + text, encoding="utf-8")
    row = {"file": f"{sid}.md", "source_id": sid, "url": url, "method": method,
           "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "chars": len(text), "fetched_at": stamp}
    with open(folder / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    direct_only = "--direct-only" in args
    args = [a for a in args if a != "--direct-only"]
    if len(args) != 3:
        print(json.dumps({"ok": False, "error": "usage: save_source.py LANE_ID SOURCE_ID URL [--direct-only]"}))
        return 2
    lane, sid, url = args
    if not ID_RE.match(lane) or not ID_RE.match(sid) or not re.match(r"^https?://", url):
        print(json.dumps({"ok": False, "error": "lane and source ids are [A-Za-z0-9._-]; URL must be http(s)"}))
        return 2
    method, text, code = fetch(url, direct_only)
    if not method or len(text.strip()) < 80:
        print(json.dumps({"ok": False, "url": url, "last_code": code}))
        return 1
    try:
        row = save(campaign_root(), lane, sid, url, method, text)
    except FileExistsError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(json.dumps({"ok": True, **row}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
