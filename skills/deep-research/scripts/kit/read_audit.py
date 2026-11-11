#!/usr/bin/env python3
"""Body-free read audit of phase sessions from the Hermes session store (deep-research kit, DR-897).

For each named session (and its child sessions) it counts tool names and pattern hits in tool-call
arguments: context brief, history, orchestration, vault, Linear, session store, skills tools, Hermes
home outside the campaign, private directories, installs, and other lanes' files. Message bodies are
never read or written. Context-handoff QA adjudicates any hit; this script only records them.

Usage: python3 read_audit.py --campaign-root ROOT --session NAME=SESSION_ID [--public NAME ...]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SKILL_TOOLS = {"skill_view", "skills_list", "skill_manage"}


def patterns(root: Path) -> dict[str, re.Pattern]:
    return {
        "context_brief": re.compile(r"context-brief"),
        "history_dir": re.compile(r"/history/|history/relevant"),
        "orchestration_dir": re.compile(r"orchestration/"),
        "vault": re.compile(r"private-knowledge-base"),
        "linear": re.compile(r"linear\.app|LINEAR_API", re.I),
        "session_db": re.compile(r"state\.db"),
        "hermes_home": re.compile(rf"/\.hermes/(?!{re.escape(str(root).split('/.hermes/', 1)[-1])}|cache/scratch)")
        if "/.hermes/" in str(root) else re.compile(r"/\.hermes/(?!cache/scratch)"),
        "private_dirs": re.compile(r"/home/[^/]+/(work|apps)/|/mnt/shared-storage|\.claude/"),
        "install_or_exec": re.compile(r"pip install|uv pip|uvx |git clone|npm (i|install)|npx |docker |podman "),
    }


def audit(root: Path, sessions: dict[str, str], public: set[str], db: Path, lanes: list[str]) -> dict:
    pats = patterns(root)
    rows = []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        return {"schema_version": "worker-read-audit-v1", "error": f"state db unavailable: {exc}", "sessions": []}
    try:
        for name, sid in sorted(sessions.items()):
            ids, frontier = [sid], [sid]
            while frontier:
                marks = ",".join("?" * len(frontier))
                frontier = [r[0] for r in con.execute(
                    f"select id from sessions where parent_session_id in ({marks})", frontier)]
                ids += frontier
            tools: dict[str, int] = {}
            hits = {key: 0 for key in pats} | {"skills_tool": 0, "other_lane_files": 0}
            marks = ",".join("?" * len(ids))
            for (raw,) in con.execute(f"select tool_calls from messages where session_id in ({marks}) "
                                      f"and tool_calls is not null", ids):
                try:
                    calls = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                for call in calls if isinstance(calls, list) else []:
                    fn = call.get("function") or {}
                    tool = fn.get("name") or call.get("name") or "?"
                    tools[tool] = tools.get(tool, 0) + 1
                    args = fn.get("arguments") or ""
                    args = args if isinstance(args, str) else json.dumps(args)
                    if tool in SKILL_TOOLS:
                        hits["skills_tool"] += 1
                    for key, pat in pats.items():
                        if pat.search(args):
                            hits[key] += 1
                    for other in lanes:
                        if other != name and (f"lanes/{other}" in args or f"sources/{other}" in args):
                            hits["other_lane_files"] += 1
            rows.append({"worker": name, "session_id": sid, "sessions_scanned": ids, "tool_counts": tools,
                         "pattern_hits": hits, "public_lane": name in public})
    finally:
        con.close()
    return {
        "schema_version": "worker-read-audit-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "method": "Body-free scan of messages.tool_calls in the Hermes session store for each phase session and "
                  "its child sessions. Only tool names and argument pattern hits are recorded.",
        "notes": {"public_lanes": "Public lanes must show no context_brief, history_dir, orchestration_dir, vault, "
                                  "linear, session_db, skills_tool, hermes_home, private_dirs or other_lane_files "
                                  "hits. Context-handoff QA adjudicates any hit."},
        "sessions": rows,
    }


def write_audit(root: Path, sessions: dict[str, str], public: set[str], lanes: list[str], db: Path | None = None) -> Path:
    from units import default_state_db

    data = audit(root, sessions, public, db or default_state_db(), lanes)
    out = root / "receipts" / "worker-read-audit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--campaign-root", required=True)
    p.add_argument("--session", action="append", default=[], help="NAME=SESSION_ID (repeatable)")
    p.add_argument("--public", action="append", default=[])
    p.add_argument("--db")
    args = p.parse_args(argv)
    sessions = dict(item.split("=", 1) for item in args.session)
    out = write_audit(Path(args.campaign_root).resolve(), sessions, set(args.public), sorted(sessions),
                      Path(args.db) if args.db else None)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
