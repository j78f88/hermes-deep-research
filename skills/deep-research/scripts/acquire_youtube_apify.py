#!/usr/bin/env python3
"""Acquire a YouTube transcript through Apify for deep-research campaigns.

This script performs a paid Apify actor call only when APIFY_TOKEN is available
(or loaded from an explicitly supplied .env). It writes corpus artifacts under
an output directory and never mutates Linear, Knowledge base, or runtime systems.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import hashlib
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_ACTOR = "scraperhive/youtube-transcript-scraper"
SYNC_PATH = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"


def load_dotenv(path: str | None) -> None:
    if not path:
        return
    p = Path(path).expanduser()
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def video_id_from(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    parsed = urllib.parse.urlparse(value)
    qs = urllib.parse.parse_qs(parsed.query)
    if qs.get("v"):
        return qs["v"][0]
    parts = [p for p in parsed.path.split("/") if p]
    for marker in ("shorts", "embed", "live"):
        if marker in parts:
            idx = parts.index(marker)
            if idx + 1 < len(parts):
                return parts[idx + 1]
    if parsed.netloc.endswith("youtu.be") and parts:
        return parts[0]
    raise SystemExit(f"Could not parse YouTube video id from: {value}")


def actor_ref(actor: str) -> str:
    return actor.strip().replace("/", "~")


def languages() -> list[str]:
    raw = os.environ.get("APIFY_TRANSCRIPT_LANGUAGES", "en,en-US,en-GB")
    return [p.strip() for p in raw.split(",") if p.strip()] or ["en"]


def content_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        chunks = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("transcript") or item.get("content") or item.get("sentence")
                if text:
                    chunks.append(str(text))
        return "\n".join(chunks).strip()
    if isinstance(value, dict):
        return content_to_text(value.get("text") or value.get("transcript") or value.get("content") or value.get("segments") or value.get("items"))
    return str(value).strip()


def extract_text(data: Any) -> str:
    items = data if isinstance(data, list) else data.get("items", []) if isinstance(data, dict) else []
    for item in items:
        if not isinstance(item, dict) or item.get("success") is False:
            continue
        for key in ("transcript", "text", "content", "segments", "items"):
            text = content_to_text(item.get(key))
            if text:
                return text
    return ""


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_line_refs(path: Path, text: str) -> None:
    lines = text.splitlines()
    width = len(str(max(len(lines), 1)))
    path.write_text("\n".join(f"L{str(i).zfill(width)} {line}" for i, line in enumerate(lines, 1)) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Acquire YouTube transcript via Apify")
    parser.add_argument("url_or_id")
    parser.add_argument("--out", required=True, help="Output directory for acquisition artifacts")
    parser.add_argument("--env-file", default=os.environ.get("APIFY_ENV_FILE"),
                        help="Optional .env file holding APIFY_TOKEN. Defaults to $APIFY_ENV_FILE.")
    parser.add_argument("--actor", default=None)
    parser.add_argument("--timeout", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Write request metadata only; do not call Apify")
    args = parser.parse_args()

    load_dotenv(args.env_file)
    video_id = video_id_from(args.url_or_id)
    actor = actor_ref(args.actor or os.environ.get("APIFY_TRANSCRIPT_ACTOR", DEFAULT_ACTOR))
    timeout = args.timeout or int(os.environ.get("APIFY_TRANSCRIPT_TIMEOUT", "180"))
    out = Path(args.out).expanduser() / video_id
    out.mkdir(parents=True, exist_ok=True)

    payload = {
        "videoUrls": [f"https://www.youtube.com/watch?v={video_id}"],
        "languages": languages(),
        "outputFormat": "text",
        "preserveFormatting": False,
    }
    acquisition = {
        "schema_version": "deep-research-youtube-apify-acquisition-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "video_id": video_id,
        "actor": actor.replace("~", "/"),
        "method": "apify_first_youtube_transcript_actor",
        "provider": "apify",
        "request_payload": payload,
        "env_file_checked": args.env_file,
        "token_present": bool(os.environ.get("APIFY_TOKEN")),
        "status": "dry_run" if args.dry_run else "started",
        "coverage": "transcript_unavailable",
        "provenance": "live",
        "degraded_state": [],
        "artifacts": {},
    }

    (out / "request.json").write_text(json.dumps(acquisition, indent=2) + "\n", encoding="utf-8")

    if args.dry_run:
        acquisition["artifacts"]["request_json"] = {"path": "request.json", "sha256": sha256_path(out / "request.json")}
        (out / "acquisition.json").write_text(json.dumps(acquisition, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": True, "dry_run": True, "out": str(out), "token_present": acquisition["token_present"]}, indent=2))
        return 0

    token = os.environ.get("APIFY_TOKEN")
    if not token:
        acquisition["status"] = "degraded"
        acquisition["degraded_state"] = ["apify_token_missing", "transcript_unavailable"]
        (out / "acquisition.json").write_text(json.dumps(acquisition, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": False, "reason": "APIFY_TOKEN missing", "out": str(out)}, indent=2))
        return 2

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        SYNC_PATH.format(actor=urllib.parse.quote(actor, safe="~")),
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "deep-research/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception as e:
        acquisition["status"] = "degraded"
        acquisition["degraded_state"] = ["apify_error", "transcript_unavailable"]
        acquisition["error"] = str(e)
        (out / "acquisition.json").write_text(json.dumps(acquisition, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"ok": False, "reason": "apify_error", "error": str(e), "out": str(out)}, indent=2))
        return 3

    response_path = out / "apify-response.json"
    response_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    text = extract_text(data)
    acquisition["artifacts"]["apify_response_json"] = {"path": "apify-response.json", "sha256": sha256_path(response_path)}

    if text:
        transcript_path = out / "transcript.txt"
        transcript_path.write_text(text + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
        lines_path = out / "transcript-lines.md"
        write_line_refs(lines_path, text)
        acquisition["status"] = "ok"
        acquisition["coverage"] = "transcript_full"
        acquisition["timestamp_or_line_refs_available"] = True
        acquisition["timestamped_evidence_present"] = False
        acquisition["artifacts"]["transcript_txt"] = {"path": "transcript.txt", "bytes": transcript_path.stat().st_size, "sha256": sha256_path(transcript_path)}
        acquisition["artifacts"]["transcript_lines_md"] = {"path": "transcript-lines.md", "bytes": lines_path.stat().st_size, "sha256": sha256_path(lines_path)}
    else:
        acquisition["status"] = "degraded"
        acquisition["degraded_state"] = ["transcript_unavailable"]

    (out / "acquisition.json").write_text(json.dumps(acquisition, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": acquisition["status"] == "ok", "status": acquisition["status"], "out": str(out), "coverage": acquisition["coverage"]}, indent=2))
    return 0 if acquisition["status"] == "ok" else 4


if __name__ == "__main__":
    raise SystemExit(main())
