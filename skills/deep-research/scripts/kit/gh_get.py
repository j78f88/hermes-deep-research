#!/usr/bin/env python3
"""Read-only GitHub API GET for public data, saved as a lane source (deep-research kit).

Usage: python3 workers/gh_get.py LANE_ID SOURCE_ID 'API_PATH'
Examples: 'repos/OWNER/REPO'
          'repos/OWNER/REPO/contents/README.md'
          'repos/OWNER/REPO/git/trees/main?recursive=1'
          'search/code?q=repo:OWNER/REPO+term&per_page=100&page=1'

Only GET through the gh CLI. Paths must start with repos/, search/, orgs/ or users/. It refuses
the authenticated account's own paths (user, notifications, gists, org members, and users/<own login>),
shell metacharacters and private repositories. Base64 file contents are decoded. Saves
workspace/sources/LANE_ID/SOURCE_ID.md under the campaign root and appends index.jsonl.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ALLOWED_PREFIX = re.compile(r"^(repos|search|orgs|users)/")
REFUSED = re.compile(r"(^user\b|^notifications|^gists|^orgs/[^/]+/members|[;|`$<>\\\s])", re.I)


def campaign_root() -> Path:
    return Path(__file__).resolve().parent.parent


def own_login() -> str | None:
    """The gh CLI's configured login, from local config only (no network)."""
    try:
        proc = subprocess.run(["gh", "config", "get", "-h", "github.com", "user"], capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    login = proc.stdout.strip()
    return login or None


def path_problem(path: str, login: str | None) -> str | None:
    if not ALLOWED_PREFIX.match(path) or REFUSED.search(path):
        return "path not allowed; public read-only repos/search/orgs/users paths only"
    if login and re.search(rf"(^|/){re.escape(login)}(/|$|\?)", path, re.I):
        return "path names the authenticated account; refused"
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        print(json.dumps({"ok": False, "error": "usage: gh_get.py LANE_ID SOURCE_ID 'API_PATH'"}))
        return 2
    lane, sid, path = args[0], args[1], args[2].lstrip("/")
    if not ID_RE.match(lane) or not ID_RE.match(sid):
        print(json.dumps({"ok": False, "error": "lane and source ids are [A-Za-z0-9._-]"}))
        return 2
    problem = path_problem(path, own_login())
    if problem:
        print(json.dumps({"ok": False, "error": problem}))
        return 2
    proc = subprocess.run(["gh", "api", "--method", "GET", "-H", "Accept: application/vnd.github+json", path],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(json.dumps({"ok": False, "error": proc.stderr.strip()[:300]}))
        return 1
    body = proc.stdout
    data = None
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("private") is True:
        print(json.dumps({"ok": False, "error": "private repository refused"}))
        return 2
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        data["items"] = [i for i in data["items"] if not (isinstance(i, dict) and
                         ((i.get("repository") or {}).get("private") or i.get("private")))]
        body = json.dumps(data, indent=1)
    if isinstance(data, dict) and data.get("encoding") == "base64" and data.get("content"):
        body = "decoded content of " + str(data.get("html_url")) + "\n\n" + \
            base64.b64decode(data["content"]).decode("utf-8", "replace")
    folder = campaign_root() / "workspace" / "sources" / lane
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{sid}.md"
    if out.exists():
        print(json.dumps({"ok": False, "error": f"{lane}/{sid}.md already exists; use a new source id"}))
        return 2
    url = "https://api.github.com/" + path
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    out.write_text(f"<!-- url: {url} | method: gh-api-get | fetched: {stamp} -->\n" + body, encoding="utf-8")
    row = {"file": f"{sid}.md", "source_id": sid, "url": url, "method": "gh-api-get",
           "sha256": hashlib.sha256(out.read_bytes()).hexdigest(), "chars": len(body), "fetched_at": stamp}
    with open(folder / "index.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    summary = {}
    if isinstance(data, list):
        summary = {"array_len": len(data)}
    elif isinstance(data, dict) and isinstance(data.get("items"), list):
        summary = {"total_count": data.get("total_count"), "items_len": len(data["items"])}
    print(json.dumps({"ok": True, **row, **summary}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
