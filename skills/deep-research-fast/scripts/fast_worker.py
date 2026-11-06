#!/usr/bin/env python3
"""Hermes one-shot worker launcher and session-record readers for fast mode.

Every model call in fast mode is a ``hermes chat --oneshot`` process, so Hermes loads its own
credentials and records the session in ``~/.hermes/state.db``. This module:

* launches one worker with its query in a file (absolute path), stdout and stderr in files, and
  ``HERMES_DISABLE_LAZY_INSTALLS=1``; the launch line follows deep-research pitfall 31;
* adopts a worker that outlived a crashed controller, or reads its finished output;
* reads back, read-only, the ``web_extract`` and ``web_search`` tool results the worker received,
  so saved pages are the exact text the tool returned, never text a model retyped;
* reads model and cost rows per session from ``session_model_usage``.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE_DB = Path(os.environ.get("HERMES_STATE_DB", str(Path.home() / ".hermes" / "state.db")))
WEB_CACHE = Path(os.environ.get("HERMES_WEB_CACHE", str(Path.home() / ".hermes" / "cache" / "web")))
SESSION_RE = re.compile(r"session_id:\s*(\S+)")
FULL_TEXT_RE = re.compile(r"Full text saved to:\s*(\S+?\.md)")
SLOT_DIR = Path.home() / ".hermes" / "work" / ".research-slots"
HOST_SLOTS = 24  # set once by the top-level controller, never by child runs


@contextlib.contextmanager
def worker_slot(deadline: float, log_path: Path | None = None):
    """Hold one host-wide process slot. Kernel locks cannot leak after a crash."""
    SLOT_DIR.mkdir(parents=True, exist_ok=True)
    start, waited = time.monotonic(), False
    while True:
        for n in range(HOST_SLOTS):
            lock = open(SLOT_DIR / f"slot-{n}.lock", "a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                continue
            try:
                yield {"fd": lock.fileno(), "wait_seconds": time.monotonic() - start if waited else 0.0}
            finally:
                lock.close()
            return
        waited = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if log_path is not None:
                with open(log_path, "a", encoding="utf-8") as log:
                    log.write(f"waited {time.monotonic() - start:.3f}s for a research worker slot, timed out\n")
            raise TimeoutError("timed out waiting for a research worker slot")
        time.sleep(min(0.02, remaining))


@dataclass
class OneshotResult:
    stdout: str
    stderr: str
    session_id: str | None
    exit_code: int | None
    pid: int | None
    started_at: float
    ended_at: float
    workdir: str
    adopted: bool = False
    extra: dict = field(default_factory=dict)


def hermes_bin() -> str:
    found = shutil.which("hermes")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / "hermes"
    return str(local)


def build_command(model: str, query_file: Path, workdir: Path, toolsets: str, run_budget: int,
                  provider: str = "openrouter", reasoning: str | None = None) -> list[str]:
    cmd = [hermes_bin(), "chat", "--oneshot", "-Q", "--ignore-rules", "-t", toolsets,
           "--provider", provider, "-m", model, "--query-file", str(Path(query_file).resolve()),
           "--in", str(Path(workdir).resolve()), "--run-budget", str(int(run_budget))]
    if reasoning:
        cmd += ["--reasoning", reasoning]
    return cmd


def _proc_state(pid: int) -> str | None:
    """The one-letter state from /proc/<pid>/stat, such as R, S or Z, or None if unreadable."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # The command name sits in brackets and may hold spaces, so read the state after the last ')'.
    rest = stat.rpartition(")")[2].split()
    return rest[0] if rest else None


CMDLINE_SETTLE_SECONDS = 2.0  # how long an empty command line is re-read before it counts as not ours


def _pid_alive(pid: int | None, marker: str, *, settle: float = CMDLINE_SETTLE_SECONDS) -> bool:
    """True when ``pid`` is a live, non-zombie process with ``marker`` on its command line.

    A process that has just been started can show an empty command line for a moment, and
    reading it then made a live worker look dead under load. An empty command line on a
    live process is therefore re-read for up to ``settle`` seconds. A zombie has exited and
    counts as dead straight away.
    """
    if not pid:
        return False
    deadline = time.monotonic() + settle
    while True:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        if _proc_state(pid) == "Z":
            return False
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        if raw:
            return marker in raw.replace(b"\0", b" ").decode("utf-8", "replace")
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _finished(workdir: Path) -> OneshotResult | None:
    meta_path = workdir / "meta.json"
    if not meta_path.exists():
        return None
    meta = json.loads(_read(meta_path) or "{}")
    stdout, stderr = _read(workdir / "stdout.txt"), _read(workdir / "stderr.txt")
    m = SESSION_RE.search(stderr)
    if meta.get("exit_code") is None and not m:
        return None
    return OneshotResult(stdout, stderr, m.group(1) if m else None, meta.get("exit_code"), meta.get("pid"),
                         meta.get("started_at", 0.0), meta.get("ended_at") or time.time(), str(workdir), adopted=True)


def oneshot(model: str, prompt: str, *, workdir: Path, toolsets: str, run_budget: int, timeout: int,
            reasoning: str | None = None, on_start=None, poll: float = 1.0) -> OneshotResult:
    """Run one Hermes one-shot worker in ``workdir``, or adopt the one already there.

    If ``workdir/meta.json`` shows a worker that is still running (same pid, our workdir on its
    command line), wait for it. If it shows a worker that finished with a session id, return its
    saved output. Otherwise launch a fresh worker.
    """
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    meta_path = workdir / "meta.json"
    marker = str(workdir)
    if meta_path.exists():
        meta = json.loads(_read(meta_path) or "{}")
        if _pid_alive(meta.get("pid"), marker):
            deadline = time.monotonic() + timeout
            while _pid_alive(meta.get("pid"), marker) and time.monotonic() < deadline:
                time.sleep(poll)
            if _pid_alive(meta.get("pid"), marker):
                _kill(meta["pid"])
            meta["ended_at"] = time.time()
            meta.setdefault("exit_code", None)
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            done = _finished(workdir)
            if done and done.stdout.strip():
                return done
        else:
            done = _finished(workdir)
            if done and done.session_id and done.stdout.strip():
                return done
        for name in ("stdout.txt", "stderr.txt", "meta.json"):
            if (workdir / name).exists():
                (workdir / name).replace(workdir / f"{name}.stale-{int(time.time())}")
    query_file = workdir / "query.md"
    query_file.write_text(prompt, encoding="utf-8")
    cmd = build_command(model, query_file, workdir, toolsets, run_budget, reasoning=reasoning)
    env = dict(os.environ, HERMES_DISABLE_LAZY_INSTALLS="1")
    deadline = time.monotonic() + timeout
    with worker_slot(deadline, workdir / "slots.log") as slot:
        if slot["wait_seconds"]:
            with open(workdir / "slots.log", "a", encoding="utf-8") as log:
                log.write(f"waited {slot['wait_seconds']:.3f}s for a research worker slot\n")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out waiting for a research worker slot")
        started = time.time()
        with open(workdir / "stdout.txt", "w", encoding="utf-8") as out, open(workdir / "stderr.txt", "w", encoding="utf-8") as err:
            proc = subprocess.Popen(cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=env,
                                    cwd=str(workdir), start_new_session=True, pass_fds=(slot["fd"],))
            meta = {"pid": proc.pid, "started_at": started, "model": model, "toolsets": toolsets, "cmd": cmd, "exit_code": None}
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            try:
                if on_start:
                    on_start(proc.pid, str(workdir))
                code = proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                _kill(proc.pid)
                code = proc.wait()
                meta["timed_out"] = True
            except Exception:
                _kill(proc.pid)
                proc.wait()
                raise
    meta.update(exit_code=code, ended_at=time.time())
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    stdout, stderr = _read(workdir / "stdout.txt"), _read(workdir / "stderr.txt")
    m = SESSION_RE.search(stderr)
    return OneshotResult(stdout, stderr, m.group(1) if m else None, code, proc.pid, started, meta["ended_at"],
                         str(workdir), extra={"timed_out": bool(meta.get("timed_out")), "slot_wait_seconds": slot["wait_seconds"]})


def _kill(pid: int):
    try:
        os.killpg(pid, signal.SIGTERM)
        time.sleep(3)
        os.killpg(pid, signal.SIGKILL)
    except OSError:
        pass


def _without_trailing_commas(text: str) -> str:
    # Match strings first so commas and escaped quotes inside them stay byte-for-byte intact.
    # A blank in place of the comma preserves offsets for raw_decode's caller.
    return re.sub(r'"(?:\\.|[^"\\])*"|,\s*(?=[}\]])',
                  lambda m: " " + m.group(0)[1:] if m.group(0).startswith(",") else m.group(0), text)


def parse_json_reply(text: str) -> dict | None:
    """The last JSON object in a model reply. Retry failed parses once without trailing commas."""
    text = (text or "").strip()
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    for chunk in reversed(fenced):
        try:
            value = json.loads(chunk)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            try:
                value = json.loads(_without_trailing_commas(chunk))
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                pass
    decoder = json.JSONDecoder()
    best = None
    i = text.find("{")
    while i >= 0:
        try:
            value, end = decoder.raw_decode(text[i:])
            if isinstance(value, dict):
                best = value
            i = text.find("{", i + max(end, 1))
        except json.JSONDecodeError:
            try:
                value, end = decoder.raw_decode(_without_trailing_commas(text[i:]))
                if isinstance(value, dict):
                    best = value
                i = text.find("{", i + max(end, 1))
            except json.JSONDecodeError:
                i = text.find("{", i + 1)
    return best


# -- session records (read-only) -------------------------------------------------------------
@contextlib.contextmanager
def _ro(db: Path | None = None):
    path = Path(db or STATE_DB)
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
    finally:
        c.close()


def _tool_payload(content: str) -> dict | None:
    start = (content or "").find("{")
    if start < 0:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(content[start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _full_text(content: str) -> str | None:
    """When Hermes cut the middle of a long page, it saved the whole page under the web cache."""
    m = FULL_TEXT_RE.search(content or "")
    if not m:
        return None
    path = Path(m.group(1))
    try:
        path.resolve().relative_to(WEB_CACHE.resolve())
    except ValueError:
        return None
    if path.is_symlink() or not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def session_tool_results(session_id: str, tool_name: str, db: Path | None = None) -> list[str]:
    if not session_id:
        return []
    with _ro(db) as c:
        rows = c.execute("SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_name=? ORDER BY id",
                         (session_id, tool_name)).fetchall()
    return [r["content"] or "" for r in rows]


def harvest_pages(session_id: str, db: Path | None = None) -> list[dict]:
    """Pages a worker received from ``web_extract``, as returned by the tool."""
    pages = []
    for content in session_tool_results(session_id, "web_extract", db):
        data = _tool_payload(content) or {}
        for row in data.get("results") or []:
            if not isinstance(row, dict) or not row.get("url"):
                continue
            text = row.get("content") or ""
            if row.get("error") or not text.strip():
                continue
            full = _full_text(text)
            pages.append({"url": row["url"], "title": row.get("title"), "text": full if full else text,
                          "origin": "web_cache_full_text" if full else "web_extract"})
    return pages


def harvest_search_urls(session_id: str, db: Path | None = None) -> list[dict]:
    """Result rows a worker received from ``web_search``."""
    out = []
    for content in session_tool_results(session_id, "web_search", db):
        data = _tool_payload(content) or {}
        for row in ((data.get("data") or {}).get("web") or []):
            if isinstance(row, dict) and row.get("url"):
                out.append({"url": row["url"], "title": row.get("title"), "description": row.get("description")})
    return out


def session_usage(session_ids: list[str], db: Path | None = None) -> list[dict]:
    """Model and cost rows per session. Auxiliary calls (for example title generation) are kept."""
    if not session_ids:
        return []
    out = []
    with _ro(db) as c:
        for sid in session_ids:
            rows = c.execute("SELECT session_id, model, task, api_call_count, input_tokens, output_tokens, cache_read_tokens, "
                             "cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status, cost_source "
                             "FROM session_model_usage WHERE session_id=?", (sid,)).fetchall()
            if rows:
                out.extend(dict(r) for r in rows)
                continue
            s = c.execute("SELECT id session_id, model, '' task, api_call_count, input_tokens, output_tokens, cache_read_tokens, "
                          "cache_write_tokens, reasoning_tokens, estimated_cost_usd, actual_cost_usd, cost_status, cost_source "
                          "FROM sessions WHERE id=?", (sid,)).fetchone()
            out.append(dict(s) if s else {"session_id": sid, "model": None, "task": None, "estimated_cost_usd": None,
                                          "actual_cost_usd": None, "cost_status": "missing"})
    return out
