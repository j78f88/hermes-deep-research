#!/usr/bin/env python3
"""Was web search working during a deep-research run, and does the brief say so if not?

The 2026-10-17 incident ran a whole campaign with web search broken (``web.search_backend`` named a
provider that did not exist) and never told the owner. This check reads what the Hermes runtime recorded,
not what the writer claims:

1. **Session evidence.** Every Hermes session (all profiles except excluded-domain) whose tool calls name the
   campaign root, plus the sessions the manifest names, plus their child sessions. Each ``web_search``
   tool result is a success or a failure; provider errors printed by ``execute_code`` or ``terminal``
   count as failures too. A session export (``.jsonl``) can be given for replays.
2. **Live probe** (optional): one real ``web_search`` call through the Hermes runtime for a profile.

Status: ``ok``, ``degraded`` (at least a quarter of searches failed), ``unavailable`` (searches failed
and none worked), ``not_observed`` (no search activity found) or ``unknown`` (no session found and no
probe). When the status is ``degraded`` or ``unavailable``, the brief the owner reads must say that web
search was limited; otherwise the verdict is FAIL ``web_search_missing_undeclared``.

Usage: python3 web_search_status.py --campaign-root ROOT [--probe] [--session-export FILE.jsonl ...]
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

HERMES_ROOT = Path(os.path.expanduser("~/.hermes"))
EXCLUDED_PROFILES = {value.strip() for value in os.environ.get("RESEARCH_EXCLUDED_PROFILES", "excluded-domain-lead").split(",") if value.strip()}  # Never read excluded profile sessions.
# The probe must run on the interpreter Hermes itself runs on. The old ``hermes-agent/venv`` is a
# stale 0.21-era venv: during the ISSUE-772 outage it reported search up while the runtime had none.
# The published launcher resolves the live interpreter (``--print-runtime-command``).
HERMES_LAUNCHER = HERMES_ROOT / "hermes-agent" / ".hermes" / "bin" / "hermes"
# A failed search result printed by code (execute_code / terminal): a result dict with success false
# and a search-provider error.
FAILURE_PATTERNS = [
    re.compile(r"success\W{1,4}:\s*(?:False|false)\W{1,6}error\W{1,6}:\s*\W{0,3}[^\n]{0,120}?"
               r"(?:web search provider|web is configured to use|searching web)", re.I),
]
INLINE_SUCCESS = re.compile(r"""['"]success['"]\s*:\s*(?:True|true)\s*,\s*['"]data['"]\s*:\s*\{\s*['"]web['"]\s*:\s*\[\s*\{""")
DEGRADED_SHARE = 0.25
SEARCH_WORDS = re.compile(r"\b(?:web search(?:es)?|search engines?|searching the web|general search|internet search)\b", re.I)
LIMIT_WORDS = re.compile(r"\b(?:unavailable|not available|unusable|failed|fail(?:ing)?|broken|down|degraded|limited|blocked|"
                         r"did not work|didn't work|could not|couldn't|no working|without|fallbacks?|workarounds?|bot challenges?)\b", re.I)


def _tool_json(content: str) -> dict | None:
    start = content.find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(content[start:])
        return value if isinstance(value, dict) else None
    except Exception:  # noqa: BLE001
        return None


# Tool output Hermes shrank later: "[web_search] query=... (3,445 chars result)" or
# "[web_search output demoted at compaction — 33,177 chars preserved ...]". Long ones were real results.
PRUNED_RESULT = re.compile(r"^\[web_search[^\n]*?([\d,]+) chars (?:result|preserved)")


def _calls_of(row: dict) -> list[tuple[str, str, str]]:
    """(call id, function name, arguments) for each tool call an assistant message made."""
    raw = row.get("tool_calls")
    try:
        calls = json.loads(raw) if isinstance(raw, str) else (raw or [])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for call in calls if isinstance(calls, list) else []:
        fn = (call or {}).get("function") or {}
        args = fn.get("arguments")
        out.append((str(call.get("id") or call.get("call_id") or ""), str(fn.get("name") or ""),
                    args if isinstance(args, str) else json.dumps(args or {})))
    return out


def scan_messages(rows: Iterable[dict]) -> dict:
    """Count web-search successes and failures in one session's messages, in order.

    ``web_search`` tool results count directly. For ``execute_code`` and ``terminal`` results, provider
    errors count only when the call that produced the result ran a web search itself, so a session that
    merely prints an old log is not mistaken for a failed search.
    """
    out = {"web_search_calls": 0, "successes": 0, "empty": 0, "failures": 0, "examples": []}
    pending: list[tuple[str, str, str]] = []
    for row in rows:
        if row.get("role") == "assistant":
            pending = _calls_of(row)
            continue
        if row.get("role") != "tool":
            continue
        content = str(row.get("content") or "")
        name = str(row.get("tool_name") or "")
        call_id = str(row.get("tool_call_id") or "")
        match = next((c for c in pending if call_id and c[0] == call_id), None) or next((c for c in pending if c[1] == name), None)
        if match in pending:
            pending.remove(match)
        args = match[2] if match else ""
        if name == "web_search":
            out["web_search_calls"] += 1
            if content.lstrip().startswith("Blocked web_search"):
                continue  # Hermes' own loop guard stopped the call; the provider was not asked
            pruned = PRUNED_RESULT.match(content.strip())
            if pruned:
                if int(pruned.group(1).replace(",", "")) >= 500:
                    out["successes"] += 1
                continue
            data = _tool_json(content) or {}
            if data.get("success") is True and (data.get("data") or {}).get("web"):
                out["successes"] += 1
            elif data.get("success") is True:
                out["empty"] += 1  # the search ran and found nothing: not a provider failure
            else:
                out["failures"] += 1
                if len(out["examples"]) < 3:
                    out["examples"].append(str(data.get("error") or content[-200:])[:200])
            continue
        if "web_search" not in args:
            continue
        for pattern in FAILURE_PATTERNS:
            hits = pattern.findall(content)
            if hits:
                out["failures"] += len(hits)
                if len(out["examples"]) < 3:
                    found = pattern.search(content)
                    out["examples"].append(content[max(0, found.start() - 80):found.end() + 80].replace("\n", " "))
        out["successes"] += len(INLINE_SUCCESS.findall(content))
    return out


def profile_homes() -> list[Path]:
    homes = [HERMES_ROOT]
    profiles = HERMES_ROOT / "profiles"
    if profiles.is_dir():
        homes += sorted(p for p in profiles.iterdir() if p.is_dir() and p.name not in EXCLUDED_PROFILES)
    return [h for h in homes if (h / "state.db").is_file()]


def _connect(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def find_sessions(tokens: list[str], named: list[str], since: float, homes: list[Path] | None = None) -> list[dict]:
    """Sessions that mention any token (campaign root path or folder name), plus named ids and children."""
    found: list[dict] = []
    for home in homes if homes is not None else profile_homes():
        try:
            conn = _connect(home / "state.db")
            ids: set[str] = set(named)
            for token in tokens:
                # Sessions that acted on the campaign (a tool call names its root), not ones that only read about it.
                rows = conn.execute(
                    "SELECT DISTINCT m.session_id FROM messages m JOIN sessions s ON s.id = m.session_id "
                    "WHERE s.started_at >= ? AND m.role = 'assistant' AND instr(m.tool_calls, ?) > 0", (since, token)).fetchall()
                ids |= {r[0] for r in rows}
            if ids:
                marks = ",".join("?" * len(ids))
                children = conn.execute(f"SELECT id FROM sessions WHERE parent_session_id IN ({marks})", tuple(ids)).fetchall()
                ids |= {r[0] for r in children}
            existing = {r[0] for r in conn.execute(f"SELECT id FROM sessions WHERE id IN ({','.join('?' * len(ids))})", tuple(ids)).fetchall()} if ids else set()
            for sid in sorted(existing):
                rows = conn.execute("SELECT role, tool_name, tool_call_id, tool_calls, content FROM messages WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
                found.append({"profile_home": str(home), "session_id": sid, **scan_messages(dict(r) for r in rows)})
            conn.close()
        except sqlite3.Error as exc:
            found.append({"profile_home": str(home), "error": f"{type(exc).__name__}: {exc}"})
    return found


def runtime_python(launcher: Path = HERMES_LAUNCHER) -> Path | None:
    """The interpreter the installed Hermes launches with, or None. Never a guessed venv."""
    try:
        proc = subprocess.run([str(launcher), "--print-runtime-command"], capture_output=True, text=True, timeout=60)
        python = Path(json.loads(proc.stdout)[0])
    except Exception:  # noqa: BLE001
        return None
    return python if proc.returncode == 0 and python.is_file() else None


def probe(hermes_home: Path = HERMES_ROOT, query: str = "deep research web search probe",
          python: Path | None = None) -> dict:
    """One real web_search call through the Hermes runtime for a profile."""
    python = python or runtime_python()
    if python is None:
        return {"available": False, "error": f"Hermes runtime interpreter not found via {HERMES_LAUNCHER}"}
    agent_dir = hermes_home / "hermes-agent"
    root = str(agent_dir)
    code = (f"import sys;sys.path.insert(0,{root!r});import hermes_bootstrap;"
            "from tools.web_tools import web_search_tool;"
            "print('PROBE'+web_search_tool(sys.argv[1], 1).replace(chr(10),' '))")
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")}
    try:
        proc = subprocess.run([str(python), "-I", "-c", code, query], cwd=root if agent_dir.is_dir() else None,
                              env={**env, "HERMES_HOME": str(hermes_home)}, capture_output=True, text=True, timeout=120)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "error": f"{type(exc).__name__}: {exc}"}
    line = next((l for l in proc.stdout.splitlines() if l.startswith("PROBE")), "")
    data = _tool_json(line[5:]) or {}
    ok = data.get("success") is True and bool((data.get("data") or {}).get("web"))
    return {"available": ok, "error": None if ok else str(data.get("error") or proc.stderr[-300:] or "no result")[:300]}


def declared_in(text: str) -> bool:
    """True when a sentence says web search was limited, unavailable or worked around."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text):
        if SEARCH_WORDS.search(sentence) and LIMIT_WORDS.search(sentence):
            return True
    return False


def classify(evidence: list[dict], probe_result: dict | None) -> str:
    sessions = [e for e in evidence if "session_id" in e]
    successes = sum(e["successes"] for e in sessions)
    failures = sum(e["failures"] for e in sessions)
    if failures and not successes:
        return "unavailable"
    if failures and failures / (failures + successes) >= DEGRADED_SHARE:
        return "degraded"
    if successes:
        return "ok"
    if probe_result is not None:
        return "not_observed" if probe_result.get("available") else "unavailable"
    return "not_observed" if sessions else "unknown"


def brief_text(root: Path, manifest: dict) -> str:
    pb = manifest.get("plain_brief") if isinstance(manifest.get("plain_brief"), dict) else {}
    rels = [pb.get("path") or "internal/plain-brief.md"]
    if manifest.get("output_layout") != "brief-and-reference-v2":
        rels.append("internal/executive-brief.md")  # legacy layout only; v2 has no executive brief
    for rel in rels:
        path = root / rel
        if path.is_file() and not path.is_symlink():
            return path.read_text(encoding="utf-8", errors="ignore")
    return ""


def evaluate(root: Path, *, run_probe: bool = False, session_exports: list[Path] | None = None,
             homes: list[Path] | None = None) -> dict:
    root = Path(root).resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    created = str(manifest.get("generated_at") or manifest.get("created_at") or manifest.get("run_date") or "")
    try:
        since = datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp() - 2 * 86400
    except ValueError:
        since = (datetime.now(timezone.utc) - timedelta(days=60)).timestamp()
    ws = manifest.get("web_search") if isinstance(manifest.get("web_search"), dict) else {}
    named = [str(s) for s in ws.get("sessions") or [] if s]
    tokens = [str(root)] + ([root.name] if len(root.name) >= 12 else [])
    if manifest.get("campaign_root") and str(manifest["campaign_root"]) not in tokens:
        tokens.append(str(manifest["campaign_root"]))
    evidence = find_sessions(tokens, named, since, homes)
    # The session running this check may quote old errors while investigating; it is not run evidence.
    current = os.environ.get("HERMES_SESSION_ID")
    evidence = [e for e in evidence if not current or e.get("session_id") != current or e.get("session_id") in named]
    for export in session_exports or []:
        rows = [json.loads(l) for l in Path(export).read_text(encoding="utf-8").splitlines() if l.strip()]
        evidence.append({"session_export": str(export), "session_id": Path(export).stem, **scan_messages(rows)})
    probe_result = probe() if run_probe else None
    status = classify(evidence, probe_result)
    declared = declared_in(brief_text(root, manifest))
    if status in {"unavailable", "degraded"} and not declared:
        verdict, klass = "FAIL", "web_search_missing_undeclared"
        reasons = [f"web search was {status} during the run and the brief does not say so"]
    elif status == "unknown":
        verdict, klass, reasons = "HOLD", "web_search_status_unknown", ["no Hermes session for this campaign was found; run with --probe or name sessions in manifest.web_search.sessions"]
    else:
        verdict, klass, reasons = "PASS", "ok", []
    sessions = [e for e in evidence if "session_id" in e]
    return {"verdict": verdict, "class": klass, "status": status, "declared_in_brief": declared, "reasons": reasons,
            "sessions_checked": len(sessions), "successes": sum(e["successes"] for e in sessions),
            "failures": sum(e["failures"] for e in sessions), "probe": probe_result,
            "failure_examples": [x for e in sessions for x in e.get("examples", [])][:5],
            "errors": [e["error"] for e in evidence if "error" in e]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--campaign-root", required=True)
    ap.add_argument("--probe", action="store_true", help="also make one live web_search call")
    ap.add_argument("--session-export", action="append", default=[], help="a session .jsonl export to include")
    args = ap.parse_args()
    result = evaluate(Path(args.campaign_root), run_probe=args.probe, session_exports=[Path(p) for p in args.session_export])
    print(json.dumps(result, indent=2))
    return {"PASS": 0, "HOLD": 2}.get(result["verdict"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
