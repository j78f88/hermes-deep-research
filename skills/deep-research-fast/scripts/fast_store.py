#!/usr/bin/env python3
"""SQLite store for deep research fast mode.

One file, ``<root>/fast.db``, in WAL mode so parallel worker threads can write. It holds:

* ``runs``: one row per research question. ``parent_run_id`` lets a later "deepest research"
  controller chain follow-up questions on the same database.
* ``phases``: start/end times and a small JSON note per controller phase, used for resume.
* ``jobs``: the job list. Status moves ``pending -> running -> landed -> done``; a failed attempt
  goes back to ``pending`` until ``max_attempts``, then ``failed``. ``skipped`` is for budget or
  deadline cuts. ``landed`` means the worker result is stored but its check has not finished.
* ``pages``: every saved source page, keyed by normalised URL, with its SHA-256. Pages are shared
  by all runs in the database, so no page is fetched twice and later runs reuse them.
* ``claims``: each finding with its verbatim quote, the page it came from and its check status.
  ``quote_ok`` means the quote is in the page. ``pass`` means the meaning check also found that
  the quote supports the claim. Only ``pass`` findings reach the writer.
* ``claim_support``: the meaning check verdict per finding, with the narrowed claim text when the
  quote supports only part of it. Saved verdicts are reused on resume, never asked again.
* ``calls``: every Hermes session launched (model, role, job) so cost can be read back later.
* ``fetch_leases``: one row per page URL being fetched right now, by any run on this root. A run
  takes the lease before it fetches; other runs wait and then read the saved page. A lease whose
  holder died, or that ran past its expiry, can be taken over once its recorded worker has ended.
  The worker stays on the lease so the new holder can harvest what it fetched.

Standard library only.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    parent_run_id TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    record_json TEXT
);
CREATE TABLE IF NOT EXISTS phases (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at REAL,
    ended_at REAL,
    data_json TEXT,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS jobs (
    run_id TEXT NOT NULL,
    id TEXT NOT NULL,
    kind TEXT NOT NULL,
    topic TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    pid INTEGER,
    worker_dir TEXT,
    session_id TEXT,
    result_json TEXT,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY (run_id, id)
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (run_id, status);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY,
    final_url TEXT,
    title TEXT,
    sha256 TEXT NOT NULL,
    text TEXT NOT NULL,
    path TEXT,
    chars INTEGER NOT NULL,
    origin TEXT,
    fetched_at REAL NOT NULL,
    run_id TEXT,
    job_id TEXT
);
CREATE INDEX IF NOT EXISTS pages_sha ON pages (sha256);
CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    topic TEXT,
    text TEXT NOT NULL,
    quote TEXT NOT NULL,
    page_url TEXT,
    page_sha256 TEXT,
    check_status TEXT NOT NULL DEFAULT 'pending',
    check_reason TEXT,
    checked_at REAL,
    UNIQUE (run_id, job_id, text, quote)
);
CREATE INDEX IF NOT EXISTS claims_run ON claims (run_id, check_status);
CREATE TABLE IF NOT EXISTS claim_support (
    claim_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    supported_text TEXT,
    reason TEXT,
    model TEXT,
    session_id TEXT,
    checked_at REAL NOT NULL,
    source_kind TEXT
);
CREATE TABLE IF NOT EXISTS fetch_leases (
    url TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    host TEXT,
    pid INTEGER,
    run_id TEXT,
    job_id TEXT,
    worker_pid INTEGER,
    worker_dir TEXT,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS calls (
    run_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    job_id TEXT,
    model TEXT,
    started_at REAL,
    ended_at REAL,
    exit_code INTEGER,
    PRIMARY KEY (run_id, session_id)
);
"""

TERMINAL = {"done", "failed", "skipped"}
HARD_MAX_SEARCH_FETCH = 40  # search plus fetch jobs per run, whatever the config or caller says
CAPPED_KINDS = ("search", "fetch")


class JobLimitError(ValueError):
    pass
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "ref_src"}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def norm_url(url: str) -> str:
    """Normalise a URL for de-duplication: lower-case scheme and host, no fragment, no tracking
    parameters, no trailing slash on a non-root path."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return url
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in _TRACKING])
    return urlunsplit(("https", host, path, query, ""))


RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def check_run_id(run_id) -> str:
    """A run id is one plain path component: letters, digits, '.', '_' and '-', at most 128
    characters, starting with a letter or digit. No separators, no '..', no absolute paths."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"bad run id {run_id!r}: use one path component of letters, digits, '.', '_' or '-'")
    return run_id


def contained(base: str | Path, path: str | Path) -> Path:
    """Resolve ``path``, following any symlinks, and require it to stay beneath ``base``."""
    root = Path(base).resolve()
    target = Path(path).resolve()
    if target != root and root not in target.parents:
        raise ValueError(f"path {path} resolves to {target}, outside {root}")
    return target


def run_dir(root: str | Path, run_id: str) -> Path:
    """``<root>/runs/<run_id>``, checked to be a plain id that stays inside the root."""
    root = Path(root).expanduser().resolve()
    runs = contained(root, root / "runs")
    target = contained(runs, runs / check_run_id(run_id))
    if target == runs:
        raise ValueError(f"run id {run_id!r} resolves to the runs folder itself")
    return target


def _now() -> float:
    return time.time()


class Store:
    """Thread-safe handle: each thread gets its own SQLite connection."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)  # executescript commits on its own, so not inside tx()
        cols = {r[1] for r in self.conn().execute("PRAGMA table_info(claim_support)")}
        if "source_kind" not in cols:  # databases made before fact grades
            try:
                self.conn().execute("ALTER TABLE claim_support ADD COLUMN source_kind TEXT")
            except sqlite3.OperationalError as exc:  # another thread added it first
                if "duplicate column" not in str(exc):
                    raise
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    # -- connection -------------------------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA busy_timeout=30000")
            deadline = time.monotonic() + 30
            while True:
                try:
                    c.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    # Concurrent first opens can conflict while switching an old database to WAL.
                    if "locked" not in str(exc) or time.monotonic() >= deadline:
                        c.close()
                        raise
                    time.sleep(0.01)
            c.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = c
        return c

    @contextlib.contextmanager
    def tx(self):
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        c.execute("COMMIT")

    def journal_mode(self) -> str:
        return self.conn().execute("PRAGMA journal_mode").fetchone()[0]

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # -- runs and phases --------------------------------------------------------------------
    def create_run(self, run_id: str, question: str, parent_run_id: str | None = None) -> dict:
        now = _now()
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO runs (id, question, parent_run_id, created_at, updated_at) VALUES (?,?,?,?,?)",
                      (run_id, question, parent_run_id, now, now))
        run = self.get_run(run_id)
        if run["question"] != question:
            raise ValueError(f"run {run_id} already exists with a different question")
        return run

    def get_run(self, run_id: str) -> dict | None:
        row = self.conn().execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return dict(row) if row else None

    def set_run(self, run_id: str, status: str | None = None, record: dict | None = None):
        with self.tx() as c:
            if status is not None:
                c.execute("UPDATE runs SET status=?, updated_at=? WHERE id=?", (status, _now(), run_id))
            if record is not None:
                c.execute("UPDATE runs SET record_json=?, updated_at=? WHERE id=?", (json.dumps(record), _now(), run_id))

    def phase(self, run_id: str, name: str) -> dict | None:
        row = self.conn().execute("SELECT * FROM phases WHERE run_id=? AND name=?", (run_id, name)).fetchone()
        if not row:
            return None
        out = dict(row)
        out["data"] = json.loads(out.pop("data_json") or "{}")
        return out

    def phases(self, run_id: str) -> list[dict]:
        rows = self.conn().execute("SELECT name FROM phases WHERE run_id=? ORDER BY started_at", (run_id,)).fetchall()
        return [self.phase(run_id, r["name"]) for r in rows]

    def start_phase(self, run_id: str, name: str):
        with self.tx() as c:
            c.execute("INSERT INTO phases (run_id, name, status, started_at) VALUES (?,?, 'running', ?) "
                      "ON CONFLICT (run_id, name) DO UPDATE SET status='running'", (run_id, name, _now()))

    def end_phase(self, run_id: str, name: str, data: dict | None = None, status: str = "done"):
        with self.tx() as c:
            c.execute("UPDATE phases SET status=?, ended_at=?, data_json=? WHERE run_id=? AND name=?",
                      (status, _now(), json.dumps(data or {}), run_id, name))

    # -- jobs -------------------------------------------------------------------------------
    def add_job(self, run_id: str, job_id: str, kind: str, payload: dict | None = None, topic: str | None = None,
                max_attempts: int = 2) -> bool:
        """Insert a job once. Returns True when the job is new.

        Raises ``JobLimitError`` for a new search or fetch job past ``HARD_MAX_SEARCH_FETCH``."""
        with self.tx() as c:
            if kind in CAPPED_KINDS and not c.execute("SELECT 1 FROM jobs WHERE run_id=? AND id=?", (run_id, job_id)).fetchone() \
                    and self._capped_count(c, run_id) >= HARD_MAX_SEARCH_FETCH:
                raise JobLimitError(f"run {run_id} already has {HARD_MAX_SEARCH_FETCH} search and fetch jobs")
            cur = c.execute("INSERT OR IGNORE INTO jobs (run_id, id, kind, topic, payload_json, max_attempts, created_at) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (run_id, job_id, kind, topic, json.dumps(payload or {}), max_attempts, _now()))
            return cur.rowcount == 1

    def add_job_within(self, run_id: str, job_id: str, kind: str, payload: dict, topic: str | None,
                       limits: dict[str, int], max_attempts: int = 2) -> str:
        """Insert a job only if the per-kind and total limits allow it, in one transaction.

        ``limits`` maps a kind (or ``"total"`` for search plus fetch) to its ceiling. The total
        never goes above ``HARD_MAX_SEARCH_FETCH``, whatever ``limits`` says.
        Returns ``"added"``, ``"exists"`` or ``"over_limit"``.
        """
        limits = dict(limits)
        limits["total"] = min(int(limits.get("total", HARD_MAX_SEARCH_FETCH)), HARD_MAX_SEARCH_FETCH)
        with self.tx() as c:
            if c.execute("SELECT 1 FROM jobs WHERE run_id=? AND id=?", (run_id, job_id)).fetchone():
                return "exists"
            if kind in limits:
                n = c.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND kind=?", (run_id, kind)).fetchone()[0]
                if n >= limits[kind]:
                    return "over_limit"
            if kind in CAPPED_KINDS and self._capped_count(c, run_id) >= limits["total"]:
                return "over_limit"
            c.execute("INSERT INTO jobs (run_id, id, kind, topic, payload_json, max_attempts, created_at) VALUES (?,?,?,?,?,?,?)",
                      (run_id, job_id, kind, topic, json.dumps(payload or {}), max_attempts, _now()))
            return "added"

    @staticmethod
    def _capped_count(c, run_id: str) -> int:
        return c.execute("SELECT COUNT(*) FROM jobs WHERE run_id=? AND kind IN ('search','fetch')", (run_id,)).fetchone()[0]

    def count_started(self, run_id: str, kinds: tuple[str, ...] = CAPPED_KINDS) -> int:
        """Distinct jobs of these kinds that have started at least once (and were not skipped)."""
        marks = ",".join("?" * len(kinds))
        return self.conn().execute(f"SELECT COUNT(*) FROM jobs WHERE run_id=? AND kind IN ({marks}) AND attempts>0 "
                                   "AND status!='skipped'", (run_id, *kinds)).fetchone()[0]

    def skip_job(self, run_id: str, job_id: str, reason: str):
        with self.tx() as c:
            c.execute("UPDATE jobs SET status='skipped', error=?, finished_at=? WHERE run_id=? AND id=?",
                      (reason, _now(), run_id, job_id))

    @staticmethod
    def _job(row) -> dict:
        job = dict(row)
        job["payload"] = json.loads(job.pop("payload_json") or "{}")
        raw = job.pop("result_json")
        job["result"] = json.loads(raw) if raw else None
        return job

    def job(self, run_id: str, job_id: str) -> dict | None:
        row = self.conn().execute("SELECT * FROM jobs WHERE run_id=? AND id=?", (run_id, job_id)).fetchone()
        return self._job(row) if row else None

    def jobs(self, run_id: str, status: str | None = None, kind: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM jobs WHERE run_id=?", [run_id]
        if status:
            q += " AND status=?"
            args.append(status)
        if kind:
            q += " AND kind=?"
            args.append(kind)
        return [self._job(r) for r in self.conn().execute(q + " ORDER BY rowid", args).fetchall()]

    def claim_next(self, run_id: str) -> dict | None:
        """Atomically move the oldest pending job to running and count the attempt."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM jobs WHERE run_id=? AND status='pending' ORDER BY rowid LIMIT 1",
                            (run_id,)).fetchone()
            if not row:
                return None
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, error=NULL "
                      "WHERE run_id=? AND id=?", (_now(), run_id, row["id"]))
        return self.job(run_id, row["id"])

    def set_worker(self, run_id: str, job_id: str, pid: int | None = None, worker_dir: str | None = None,
                   session_id: str | None = None):
        with self.tx() as c:
            c.execute("UPDATE jobs SET pid=COALESCE(?, pid), worker_dir=COALESCE(?, worker_dir), "
                      "session_id=COALESCE(?, session_id) WHERE run_id=? AND id=?",
                      (pid, worker_dir, session_id, run_id, job_id))

    def land_job(self, run_id: str, job_id: str, result: dict):
        with self.tx() as c:
            c.execute("UPDATE jobs SET status='landed', result_json=?, session_id=COALESCE(?, session_id) "
                      "WHERE run_id=? AND id=? AND status='running'",
                      (json.dumps(result), result.get("session_id"), run_id, job_id))

    def finish_job(self, run_id: str, job_id: str):
        with self.tx() as c:
            c.execute("UPDATE jobs SET status='done', finished_at=? WHERE run_id=? AND id=? AND status='landed'",
                      (_now(), run_id, job_id))

    def fail_attempt(self, run_id: str, job_id: str, error: str) -> str:
        """Record a failed attempt. Returns the new status (``pending`` to retry, or ``failed``)."""
        with self.tx() as c:
            row = c.execute("SELECT attempts, max_attempts FROM jobs WHERE run_id=? AND id=?", (run_id, job_id)).fetchone()
            status = "pending" if row and row["attempts"] < row["max_attempts"] else "failed"
            c.execute("UPDATE jobs SET status=?, error=?, finished_at=CASE WHEN ?='failed' THEN ? ELSE NULL END "
                      "WHERE run_id=? AND id=?", (status, error[:2000], status, _now(), run_id, job_id))
        return status

    def skip_pending(self, run_id: str, reason: str) -> int:
        with self.tx() as c:
            return c.execute("UPDATE jobs SET status='skipped', error=?, finished_at=? WHERE run_id=? AND status='pending'",
                             (reason, _now(), run_id)).rowcount

    def recover(self, run_id: str) -> int:
        """After a controller crash: running jobs go back to pending without using up an attempt.

        The executor then finds the earlier attempt's folder: it waits for a worker that is still
        alive, or reads a worker's finished output, before launching anything new.
        """
        with self.tx() as c:
            return c.execute("UPDATE jobs SET status='pending', attempts=MAX(attempts-1, 0) WHERE run_id=? AND status='running'",
                             (run_id,)).rowcount

    def job_counts(self, run_id: str) -> dict:
        out: dict[str, dict[str, int]] = {}
        for r in self.conn().execute("SELECT kind, status, COUNT(*) n FROM jobs WHERE run_id=? GROUP BY kind, status", (run_id,)):
            out.setdefault(r["kind"], {})[r["status"]] = r["n"]
        return out

    def count_jobs(self, run_id: str, kinds: tuple[str, ...]) -> int:
        marks = ",".join("?" * len(kinds))
        return self.conn().execute(f"SELECT COUNT(*) FROM jobs WHERE run_id=? AND kind IN ({marks})",
                                   (run_id, *kinds)).fetchone()[0]

    # -- pages ------------------------------------------------------------------------------
    def save_page(self, url: str, text: str, *, title: str | None = None, final_url: str | None = None,
                  origin: str = "web_extract", run_id: str | None = None, job_id: str | None = None) -> dict:
        """Save a page once. A URL already saved keeps its first copy (saved pages never change)."""
        key = norm_url(url)
        existing = self.get_page(key)
        if existing:
            return existing
        digest = sha256_text(text)
        rel = f"pages/{digest[:2]}/{digest}.txt"
        target = contained(self.path.parent, self.path.parent / rel)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(f".tmp{threading.get_ident()}")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(target)
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO pages (url, final_url, title, sha256, text, path, chars, origin, fetched_at, run_id, job_id) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (key, final_url or url, title, digest, text, rel, len(text), origin, _now(), run_id, job_id))
        return self.get_page(key)

    # -- fetch leases -----------------------------------------------------------------------
    def acquire_fetch_lease(self, url: str, owner: str, *, run_id: str, job_id: str, ttl: float,
                            host: str | None = None, pid: int | None = None) -> tuple[str, dict | None]:
        """Reserve one page URL for fetching, across every run on this database, in one transaction.

        Returns ``("saved", None)`` when the page is already saved, ``("acquired", None)`` when the
        caller now holds the lease, or ``("held", lease_row)`` when someone else holds it. A lease
        held by the same owner or the same run and job (a resume) is taken. Expiry alone never
        frees a lease here: the caller checks the recorded worker and uses ``take_over_fetch_lease``.
        The recorded worker is kept, so a later take-over can still find that worker's output.
        """
        key, now = norm_url(url), _now()
        with self.tx() as c:
            if c.execute("SELECT 1 FROM pages WHERE url=?", (key,)).fetchone():
                return "saved", None
            row = c.execute("SELECT * FROM fetch_leases WHERE url=?", (key,)).fetchone()
            if row and row["owner"] != owner and (row["run_id"], row["job_id"]) != (run_id, job_id):
                return "held", dict(row)
            c.execute("INSERT INTO fetch_leases (url, owner, host, pid, run_id, job_id, acquired_at, expires_at) "
                      "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (url) DO UPDATE SET owner=excluded.owner, "
                      "host=excluded.host, pid=excluded.pid, run_id=excluded.run_id, job_id=excluded.job_id, "
                      "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
                      (key, owner, host, pid, run_id, job_id, now, now + ttl))
            return "acquired", None

    def take_over_fetch_lease(self, url: str, old_owner: str, owner: str, *, run_id: str, job_id: str, ttl: float,
                              host: str | None = None, pid: int | None = None,
                              expect_worker: tuple[int | None, str | None] | None = None) -> str:
        """Take a lease whose holder and worker are known to be gone. Only succeeds if ``old_owner``
        still holds it and, when ``expect_worker`` is given, its recorded ``(worker_pid, worker_dir)``
        is still the one the caller checked.

        The recorded worker stays on the lease, so the new holder (or whoever takes it next, after
        another crash) can harvest that worker's finished output before fetching again.
        Returns ``"saved"``, ``"acquired"`` or ``"lost"`` (someone else changed the lease first)."""
        key, now = norm_url(url), _now()
        q = "UPDATE fetch_leases SET owner=?, host=?, pid=?, run_id=?, job_id=?, acquired_at=?, expires_at=? WHERE url=? AND owner=?"
        args = [owner, host, pid, run_id, job_id, now, now + ttl, key, old_owner]
        if expect_worker is not None:
            q += " AND worker_pid IS ? AND worker_dir IS ?"
            args += list(expect_worker)
        with self.tx() as c:
            if c.execute("SELECT 1 FROM pages WHERE url=?", (key,)).fetchone():
                return "saved"
            return "acquired" if c.execute(q, args).rowcount == 1 else "lost"

    def set_lease_worker(self, url: str, owner: str, worker_pid: int | None, worker_dir: str | None):
        with self.tx() as c:
            c.execute("UPDATE fetch_leases SET worker_pid=?, worker_dir=? WHERE url=? AND owner=?",
                      (worker_pid, worker_dir, norm_url(url), owner))

    def release_fetch_lease(self, url: str, owner: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM fetch_leases WHERE url=? AND owner=?", (norm_url(url), owner)).rowcount == 1

    def fetch_lease(self, url: str) -> dict | None:
        row = self.conn().execute("SELECT * FROM fetch_leases WHERE url=?", (norm_url(url),)).fetchone()
        return dict(row) if row else None

    def get_page(self, url: str) -> dict | None:
        row = self.conn().execute("SELECT * FROM pages WHERE url=?", (norm_url(url),)).fetchone()
        return dict(row) if row else None

    def page_count(self) -> int:
        return self.conn().execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    # -- claims -----------------------------------------------------------------------------
    def add_claim(self, run_id: str, job_id: str, text: str, quote: str, page_url: str | None,
                  page_sha256: str | None, topic: str | None = None) -> int:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO claims (run_id, job_id, topic, text, quote, page_url, page_sha256) VALUES (?,?,?,?,?,?,?)",
                      (run_id, job_id, topic, text, quote, norm_url(page_url) if page_url else None, page_sha256))
            row = c.execute("SELECT id FROM claims WHERE run_id=? AND job_id=? AND text=? AND quote=?",
                            (run_id, job_id, text, quote)).fetchone()
        return row["id"]

    def claim(self, claim_id: int) -> dict | None:
        row = self.conn().execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        return dict(row) if row else None

    def claims(self, run_id: str, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM claims WHERE run_id=?", [run_id]
        if status:
            q += " AND check_status=?"
            args.append(status)
        return [dict(r) for r in self.conn().execute(q + " ORDER BY id", args).fetchall()]

    def set_claim_check(self, claim_id: int, status: str, reason: str | None = None):
        with self.tx() as c:
            c.execute("UPDATE claims SET check_status=?, check_reason=?, checked_at=? WHERE id=?",
                      (status, reason, _now(), claim_id))

    def set_support(self, claim_id: int, run_id: str, verdict: str, supported_text: str | None = None,
                    reason: str | None = None, model: str | None = None, session_id: str | None = None):
        """Record the meaning check verdict for one finding. The first saved verdict is kept."""
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO claim_support (claim_id, run_id, verdict, supported_text, reason, model, "
                      "session_id, checked_at) VALUES (?,?,?,?,?,?,?,?)",
                      (claim_id, run_id, verdict, supported_text, reason, model, session_id, _now()))

    def set_supports(self, rows: list[dict]):
        """Record a whole checker batch in one transaction, so a crash saves all of it or none."""
        with self.tx() as c:
            for r in rows:
                c.execute("INSERT OR IGNORE INTO claim_support (claim_id, run_id, verdict, supported_text, reason, model, "
                          "session_id, checked_at, source_kind) VALUES (?,?,?,?,?,?,?,?,?)",
                          (r["claim_id"], r["run_id"], r["verdict"], r.get("supported_text"), r.get("reason"),
                           r.get("model"), r.get("session_id"), _now(), r.get("source_kind")))

    def child_runs(self, parent_run_id: str) -> list[dict]:
        rows = self.conn().execute("SELECT * FROM runs WHERE parent_run_id=? ORDER BY created_at, id",
                                   (parent_run_id,)).fetchall()
        return [dict(r) for r in rows]

    def support(self, claim_id: int) -> dict | None:
        row = self.conn().execute("SELECT * FROM claim_support WHERE claim_id=?", (claim_id,)).fetchone()
        return dict(row) if row else None

    def supports(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.conn().execute(
            "SELECT * FROM claim_support WHERE run_id=? ORDER BY claim_id", (run_id,)).fetchall()]

    # -- calls ------------------------------------------------------------------------------
    def add_call(self, run_id: str, session_id: str, role: str, model: str | None, job_id: str | None = None,
                 started_at: float | None = None, ended_at: float | None = None, exit_code: int | None = None):
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO calls (run_id, session_id, role, job_id, model, started_at, ended_at, exit_code) "
                      "VALUES (?,?,?,?,?,?,?,?)", (run_id, session_id, role, job_id, model, started_at, ended_at, exit_code))

    def calls(self, run_id: str) -> list[dict]:
        return [dict(r) for r in self.conn().execute("SELECT * FROM calls WHERE run_id=? ORDER BY started_at", (run_id,))]
