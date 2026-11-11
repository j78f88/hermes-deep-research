#!/usr/bin/env python3
"""Launch phase processes and wait on them once (deep-research kit).

launch_unit() starts one process either as a transient systemd user unit (production) or as a
detached child (tests, launcher "direct"). systemd-run starts units without the login PATH, so the
unit always gets --setenv=PATH=$PATH --setenv=HOME=$HOME. API keys that are set in the driver's environment
are passed by name only (--setenv=NAME), so their values never appear on a command line. wait_markers() is one
blocking wait over every marker file, through wait_for_jobs.wait_for_jobs.

session_tool_calls() reads a session's tool-call count (and its child sessions') from the Hermes
session store, read-only and body-free.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


# Secrets the phase sessions need. systemd-run copies a name-only --setenv value from its own environment.
FORWARDED_SECRETS = ("OPENROUTER_API_KEY", "APIFY_TOKEN")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def systemd_command(unit: str, argv: list[str], env: dict[str, str] | None = None) -> list[str]:
    cmd = ["systemd-run", "--user", f"--unit={unit}", "--collect",
           f"--setenv=PATH={os.environ.get('PATH', '')}", f"--setenv=HOME={os.environ.get('HOME', '')}"]
    cmd += [f"--setenv={k}" for k in FORWARDED_SECRETS if os.environ.get(k)]
    cmd += [f"--setenv={k}={v}" for k, v in sorted((env or {}).items())]
    return cmd + list(argv)


def launch_unit(*, unit: str, argv: list[str], launcher: str, env: dict[str, str] | None = None,
                journal: Path | None = None) -> dict:
    """Start argv detached. Returns a launch record (unit, command, pid or systemd output, rc)."""
    record = {"unit": unit, "launcher": launcher, "argv": list(argv), "launched_at": now()}
    if launcher == "systemd":
        cmd = systemd_command(unit, argv, env)
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True)
            record.update(command=cmd, rc=proc.returncode, output=(proc.stdout + proc.stderr).strip())
        except OSError as exc:  # systemd-run missing: use --launcher direct
            record.update(command=cmd, rc=127, output=f"{exc}; use --launcher direct")
    elif launcher == "direct":
        child_env = {**os.environ, **(env or {})}
        try:
            proc = subprocess.Popen(argv, env=child_env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
            record.update(command=list(argv), rc=0, pid=proc.pid, output="")
        except OSError as exc:
            record.update(command=list(argv), rc=127, output=str(exc))
    else:
        raise ValueError(f"unknown launcher: {launcher}")
    if journal is not None:
        journal.parent.mkdir(parents=True, exist_ok=True)
        with journal.open("a", encoding="utf-8") as fh:
            fh.write(f"{record['launched_at']} launch unit={unit} launcher={launcher} rc={record['rc']} "
                     f"out={record.get('output', '')!r}\n")
    return record


def wait_markers(markers: list[Path], timeout: float, interval: float = 5.0) -> dict:
    """One blocking wait for every marker file. Returns the wait_for_jobs summary."""
    import wait_for_jobs

    return wait_for_jobs.wait_for_jobs(markers=[str(m) for m in markers], timeout=timeout, interval=interval)


def default_state_db() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "state.db"


def session_tool_calls(session_id: str | None, db: Path | None = None) -> dict:
    """Tool calls recorded for a session and its descendants; None values when unknown."""
    out = {"session_id": session_id, "tool_calls": None, "sessions": [], "model": None, "cost_usd": None}
    db = db or default_state_db()
    if not session_id or not db.is_file():
        return out
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return out
    try:
        ids, frontier = [session_id], [session_id]
        while frontier:
            marks = ",".join("?" * len(frontier))
            frontier = [r[0] for r in con.execute(f"select id from sessions where parent_session_id in ({marks})",
                                                    frontier)]
            ids += frontier
        marks = ",".join("?" * len(ids))
        rows = con.execute(f"select id, model, tool_call_count, coalesce(actual_cost_usd, estimated_cost_usd) "
                           f"from sessions where id in ({marks})", ids).fetchall()
        if rows:
            out["tool_calls"] = sum(r[2] or 0 for r in rows)
            out["cost_usd"] = round(sum(r[3] or 0 for r in rows), 4)
            out["model"] = next((r[1] for r in rows if r[0] == session_id), None)
            out["sessions"] = [{"id": r[0], "tool_calls": r[2] or 0} for r in rows]
    except sqlite3.Error:
        pass
    finally:
        con.close()
    return out


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
