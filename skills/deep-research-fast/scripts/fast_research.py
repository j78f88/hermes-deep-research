#!/usr/bin/env python3
"""Deep research fast mode controller.

    python3 fast_research.py run --question "..." --root DIR [--run-id ID] [--parent-run-id ID]
    python3 fast_research.py status --root DIR [--run-id ID]
    python3 fast_research.py record --root DIR --run-id ID     # refresh cost records later

Phases, each recorded in ``DIR/fast.db`` and skipped on resume once done:

1. preflight  every model answers "Reply with exactly: OK" through ``hermes chat --oneshot``.
2. plan       one strong-model call splits the question into topics and search queries.
3. research   small search jobs, then one fetch job per new page, run in parallel (at most 8).
              Each page is saved once from the tool's own output. Findings are quote-checked
              as each job lands, then one meaning check per page asks a model from another vendor
              whether each quote supports its claim. Only supported findings reach the writer.
4. answer     one strong-model call writes the answer from checked findings only. Citations are
              renumbered and every one is checked against its saved page.
5. review     one review by a model from a different vendor than the writer.
6. record     wall time, job counts, models and cost from Hermes session records.

Rerunning the same command after a crash resumes the unfinished run for that question.
A later controller can call ``FastResearch(...).run()`` many times on one root to share pages.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fast_prompts as P  # noqa: E402
import fast_worker as fw  # noqa: E402
import fact_grades as fg  # noqa: E402
from fast_quote_check import MIN_QUOTE_CHARS, _norm_page, norm, check_answer, check_claim  # noqa: E402
from fast_runner import HARD_MAX_CONCURRENCY, Runner  # noqa: E402
from fast_store import HARD_MAX_SEARCH_FETCH, Store, check_run_id, contained, norm_url, run_dir  # noqa: E402

CONFIG_PATH = HERE.parent / "config" / "fast-mode.json"
LEASE_GRACE_SECONDS = 60  # a fetch lease outlives the worker timeout by this much before it expires
PHASES = ("preflight", "plan", "research", "answer", "check", "review", "finish", "record")
FINDING_RE = re.compile(r"\[(C\d+(?:\s*[,;]\s*C\d+)*)\]")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
SUPPORT_STATUS = {"supported": "pass", "partly": "pass", "unsupported": "unsupported", "unchecked": "meaning_unchecked"}


def claim_counts(claims: list[dict]) -> dict:
    out = {"total": len(claims)}
    for c in claims:
        out[c["check_status"]] = out.get(c["check_status"], 0) + 1
    out.setdefault("pass", 0)
    out.setdefault("fail", 0)
    return out


def parse_checks(parsed, claims: dict[int, dict], pending: list[int]) -> list[tuple[str, str | None, str]]:
    """One (verdict, narrowed text, reason) per pending finding, in order, from a checker reply.

    Fails closed: a row with a non-integer or out-of-range number is ignored, an unknown verdict
    counts as unchecked, and a partly verdict needs a non-empty string narrowed claim that adds
    no number its quote lacks.
    """
    by_n: dict[int, dict] = {}
    rows = parsed.get("checks") if isinstance(parsed, dict) else None
    for row in rows if isinstance(rows, list) else []:
        n = row.get("n") if isinstance(row, dict) else None
        if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(pending):
            by_n.setdefault(n, row)
    out = []
    for i, cid in enumerate(pending, 1):
        row = by_n.get(i) or {}
        verdict = row.get("verdict") if row.get("verdict") in ("supported", "partly", "unsupported") else "unchecked"
        raw_reason = row.get("reason")
        reason = (raw_reason if isinstance(raw_reason, str) else "")[:500] or (
            "no verdict from the checker" if verdict == "unchecked" else verdict)
        narrowed = None
        if verdict == "partly":
            text = row.get("supported_claim")
            narrowed = text.strip() if isinstance(text, str) else ""
            if not narrowed_ok(narrowed, claims[cid]["quote"]):
                verdict, reason, narrowed = "unsupported", "narrowed claim was empty, not text, or added a number", None
        out.append((verdict, narrowed, reason))
    return out


def narrowed_ok(text: str, quote: str) -> bool:
    """A narrowed claim may not carry a number that its quote lacks."""
    quote_numbers = set(NUMBER_RE.findall(quote))
    return bool(text.strip()) and all(n in quote_numbers for n in NUMBER_RE.findall(text))


class PhaseError(RuntimeError):
    pass


def validate_config(cfg: dict) -> dict:
    """A checked copy of a config: concurrency and job counts held to the hard ceilings in code.

    Every ``FastResearch`` runs its config through here, whether it came from a file or a caller."""
    cfg = copy.deepcopy(cfg)
    lim = cfg["limits"]
    lim["concurrency"] = max(1, min(int(lim["concurrency"]), HARD_MAX_CONCURRENCY))
    lim["host_slots"] = max(1, int(lim.get("host_slots", 24)))
    for key in ("max_search_fetch_jobs", "max_search_jobs", "max_fetch_jobs"):
        lim[key] = max(0, min(int(lim[key]), HARD_MAX_SEARCH_FETCH))
    return cfg


def load_config(path: Path | None = None) -> dict:
    return validate_config(json.loads(Path(path or CONFIG_PATH).read_text(encoding="utf-8")))


def vendor(model: str | None) -> str | None:
    return model.split("/", 1)[0].lower() if model and "/" in model else None


def default_run_id(question: str, prefix: str = "fast") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{hashlib.sha1(question.encode()).hexdigest()[:8]}"


def find_open_run(store: Store, question: str, parent_run_id: str | None = None, prefix: str = "fast") -> str | None:
    rows = store.conn().execute("SELECT id,status,record_json FROM runs WHERE question=? AND parent_run_id IS ? "
                                "AND id LIKE ? ORDER BY created_at DESC",
                                (question, parent_run_id, prefix + "-%")).fetchall()
    for row in rows:
        if row["status"] not in ("completed", "stopped"):
            return row["id"]
        record = json.loads(row["record_json"] or "{}")
        branches = record.get("branches", []) + (record.get("review_round") or {}).get("branches", [])
        if any(branch.get("status") == "failed" for branch in branches):
            return row["id"]
    return None


def _url_ok(url: str) -> bool:
    u = (url or "").lower()
    if not u.startswith(("http://", "https://")):
        return False
    return not any(bad in u for bad in ("google.com/goto", "google.com/url", "youtube.com/watch", "youtu.be/"))


class FastResearch:
    ID_PREFIX = "fast"

    def __init__(self, root: str | Path, question: str, *, run_id: str | None = None, parent_run_id: str | None = None,
                 config: dict | None = None, oneshot=None, harvest_pages=None, harvest_search=None, usage=None,
                 log=None, skip_preflight: bool = False):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.cfg = validate_config(config if config is not None else load_config())
        self.show_grades = bool((self.cfg.get("grades") or {}).get("show_in_answer", False))
        self.store = Store(self.root / "fast.db")
        self.question = question.strip()
        self.skip_preflight = skip_preflight
        if run_id is None:
            run_id = (find_open_run(self.store, self.question, parent_run_id, self.ID_PREFIX)
                      or default_run_id(self.question, self.ID_PREFIX))
        self.run_id = check_run_id(run_id)
        self.dir = run_dir(self.root, self.run_id)  # checked before any record or folder is made
        self.store.create_run(self.run_id, self.question, parent_run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir = run_dir(self.root, self.run_id)
        self.oneshot = oneshot or fw.oneshot
        self.harvest_pages = harvest_pages or fw.harvest_pages
        self.harvest_search = harvest_search or fw.harvest_search_urls
        self.usage = usage or fw.session_usage
        self._log_fn = log
        self._lock = threading.Lock()
        self.host, self.pid = socket.gethostname(), os.getpid()
        self.lease_owner = f"{self.host}:{self.pid}:{uuid.uuid4().hex[:12]}"
        self.lease_poll = 1.0

    # -- helpers --------------------------------------------------------------------------
    def path(self, *parts: str) -> Path:
        """A path inside this run's folder. Names or symlinks that lead outside it are refused."""
        target = self.dir.joinpath(*parts)
        contained(self.dir, target)
        return target

    def log(self, msg: str):
        line = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {msg}"
        with self._lock:
            with open(self.path("events.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self._log_fn:
            self._log_fn(line)

    def model(self, role: str) -> str:
        return self.cfg["models"][role]["id"]

    def write_json(self, name: str, data) -> Path:
        path = self.path(name)
        tmp = self.path(name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def call(self, role: str, prompt: str, workdir: Path, *, model: str | None = None, toolsets: str | None = None,
             budget: int, job_id: str | None = None, reasoning_role: str | None = None,
             lease_url: str | None = None) -> fw.OneshotResult:
        model = model or self.model(role)
        toolsets = toolsets or self.cfg["toolsets"]["thinker"]
        reasoning = self.cfg.get("reasoning", {}).get(reasoning_role or role)

        def on_start(pid, wd):
            if job_id:
                self.store.set_worker(self.run_id, job_id, pid=pid, worker_dir=wd)
            if lease_url:
                self.store.set_lease_worker(lease_url, self.lease_owner, pid, wd)

        if lease_url:
            # Name the worker folder on the lease before launching, so a crash right after the
            # launch still leaves the worker findable (its pid is in the folder's meta.json).
            self.store.set_lease_worker(lease_url, self.lease_owner, None, str(Path(workdir).resolve()))
        res = self.oneshot(model, prompt, workdir=workdir, toolsets=toolsets, run_budget=budget, timeout=budget + 90,
                           reasoning=reasoning, on_start=on_start)
        if res.session_id:
            self.store.add_call(self.run_id, res.session_id, role, model, job_id, res.started_at, res.ended_at, res.exit_code)
            if job_id:
                self.store.set_worker(self.run_id, job_id, session_id=res.session_id)
        return res

    def _phase(self, name: str, fn):
        ph = self.store.phase(self.run_id, name)
        if ph and ph["status"] == "done":
            self.log(f"phase {name}: already done, skipped")
            return ph["data"]
        self.store.start_phase(self.run_id, name)
        self.log(f"phase {name}: start")
        try:
            data = fn() or {}
        except Exception as exc:
            self.store.end_phase(self.run_id, name, {"error": repr(exc)}, status="failed")
            self.log(f"phase {name}: failed {exc!r}")
            raise
        self.store.end_phase(self.run_id, name, data)
        self.log(f"phase {name}: done")
        return data

    # -- run --------------------------------------------------------------------------------
    def research_steps(self):
        return [("plan", self.plan), ("research", self.research)]

    def source_run_ids(self) -> list[str]:
        return [self.run_id]

    def run(self, stop_after: str | None = None) -> dict:
        names = ["preflight", *[name for name, _ in self.research_steps()], "answer", "check", "review", "finish", "record"]
        if stop_after is not None and stop_after not in names:
            raise ValueError(f"stop_after must be one of {names}")
        with open(self.path(".controller.lock"), "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError(f"another controller is running run {self.run_id}") from None
            if not self.skip_preflight:
                fw.HOST_SLOTS = self.cfg["limits"]["host_slots"]
            return self._run(stop_after)

    def _run(self, stop_after: str | None) -> dict:
        self.store.set_run(self.run_id, status="running")
        self.log(f"run {self.run_id} root {self.root}")
        status, error = "completed", None
        try:
            steps = [("preflight", (lambda: {"skipped": "covered by parent run"}) if self.skip_preflight else self.preflight),
                     *self.research_steps()]
            for name, fn in steps:
                self._phase(name, fn)
                if name == stop_after:
                    record = self.record()
                    self.store.set_run(self.run_id, status="completed", record=record)
                    return record
            self.answer_stages(stop_after)
        except PhaseError as exc:
            status, error = "stopped", str(exc)
        except Exception as exc:  # unexpected: keep the run open for resume
            status, error = "failed", repr(exc)
        self.store.start_phase(self.run_id, "record")
        self.store.end_phase(self.run_id, "record", {})
        record = self.record(status=status, error=error)
        self.store.set_run(self.run_id, status=status, record=record)
        return record

    def answer_stages(self, stop_after=None, *, prefix=""):
        """Run or resume the shared answer, check, review and finish stages."""
        self._phase(prefix + "answer", self.answer)
        # Revalidate even saved answers, including those written by older controllers.
        self._checker_independent()
        if not self._answer_meaning_checked():
            self.log("answer: saved answer cites findings without a meaning verdict, checking and rewriting it")
            self._check_legacy_passes()
            stamp = int(time.time())
            for name in ("answer", "answer.md", "citations.json", "quote_check.json", "answer.pre-check.md",
                         "citations.pre-check.json", "sentence_check.json", "grades.json", "not_verified.json", "review.json"):
                if (self.dir / name).exists():
                    (self.dir / name).replace(self.path(f"{name}.legacy-{stamp}"))
            for name in ("answer", "check", "review", "finish"):
                self.store.start_phase(self.run_id, prefix + name)
            self._phase(prefix + "answer", self.answer)
        if stop_after != "answer":
            # Display-only changes reuse the cached sentence check.
            saved_grades = self._read_json("grades.json")
            if saved_grades is not None and saved_grades.get("shown_in_answer", True) != self.show_grades:
                self.log("check: grade display setting changed, rebuilding the checked answer")
                for name in ("check", "finish"):
                    self.store.start_phase(self.run_id, prefix + name)
            for name, fn in [("check", self.check), ("review", self.review), ("finish", self.finish)]:
                self._phase(prefix + name, fn)
                if name == "check":
                    self._sentence_checker_independent()
                    self._final_quote_check()
                if name == stop_after:
                    break

    # -- phases -----------------------------------------------------------------------------
    def preflight(self) -> dict:
        roles = ("worker", "checker", "planner", "writer", "reviewer")
        models = {}
        for role in roles:
            models.setdefault(self.model(role), role)

        def probe(item):
            model, role = item
            toolsets = self.cfg["toolsets"]["worker" if role == "worker" else "thinker"]
            slug = re.sub(r"[^a-z0-9]+", "-", model.lower())
            res = self.call("preflight", P.PREFLIGHT, self.path("preflight", slug), model=model, toolsets=toolsets,
                            budget=self.cfg["budgets_seconds"]["preflight"], reasoning_role="preflight")
            reply = res.stdout.strip()
            return {"model": model, "toolsets": toolsets, "reply": reply[:200], "ok": reply == "OK",
                    "exit_code": res.exit_code, "session_id": res.session_id}

        if vendor(self.model("checker")) == vendor(self.model("worker")):
            raise PhaseError("the meaning checker must come from a different vendor than the worker")
        with ThreadPoolExecutor(max_workers=len(models)) as pool:
            checks = list(pool.map(probe, models.items()))
        self.write_json("preflight.json", checks)
        bad = [c["model"] for c in checks if not c["ok"]]
        if bad:
            raise PhaseError(f"preflight failed for {', '.join(bad)}")
        return {"models": [c["model"] for c in checks]}

    def plan(self) -> dict:
        lim = self.cfg["limits"]
        prompt = P.PLAN.format(question=self.question, min_topics=lim["min_topics"], max_topics=lim["max_topics"],
                               queries_per_topic=lim["queries_per_topic"])
        plan = None
        for attempt in range(1, 3):
            res = self.call("planner", prompt, self.path("plan", f"attempt-{attempt}"),
                            budget=self.cfg["budgets_seconds"]["plan"])
            plan = fw.parse_json_reply(res.stdout)
            if plan and isinstance(plan.get("topics"), list) and plan["topics"]:
                break
            plan = None
        if not plan:
            raise PhaseError("planner gave no usable topics")
        topics = []
        for i, t in enumerate(plan["topics"][: lim["max_topics"]], 1):
            if not isinstance(t, dict):
                continue
            queries = [q.strip() for q in t.get("queries") or [] if isinstance(q, str) and q.strip()]
            topics.append({"id": f"t{i}", "title": str(t.get("title") or f"Topic {i}").strip(),
                           "need": str(t.get("need") or "").strip(), "queries": queries[: lim["queries_per_topic"]]})
        self.write_json("plan.json", {"question": self.question, "topics": topics})
        # Round-taxonomy across topics so a search cap cuts evenly.
        added, depth = 0, max((len(t["queries"]) for t in topics), default=0)
        for qi in range(depth):
            for t in topics:
                if qi >= len(t["queries"]):
                    continue
                outcome = self.store.add_job_within(
                    self.run_id, f"search-{t['id']}-{qi + 1}", "search",
                    {"query": t["queries"][qi], "topic": t}, t["id"], self._limits(), lim["max_attempts"])
                added += outcome == "added"
        if not added and not self.store.jobs(self.run_id, kind="search"):
            raise PhaseError("plan produced no search jobs")
        return {"topics": len(topics), "search_jobs": added}

    def _limits(self) -> dict:
        lim = self.cfg["limits"]
        return {"search": lim["max_search_jobs"], "fetch": lim["max_fetch_jobs"], "total": lim["max_search_fetch_jobs"]}

    def research(self) -> dict:
        recovered = self.store.recover(self.run_id)
        if recovered:
            self.log(f"research: {recovered} interrupted job(s) put back to pending")
        lim = self.cfg["limits"]
        deadline = time.monotonic() + lim["research_deadline_seconds"]
        runner = Runner(self.store, self.run_id, self.execute_job, self.on_job_done,
                        concurrency=lim["concurrency"], deadline=deadline)
        out = runner.run()
        claims = self.store.claims(self.run_id)
        out["claims"] = claim_counts(claims)
        self.log(f"research: {json.dumps(out)}")
        return out

    # -- jobs -------------------------------------------------------------------------------
    def _job_dir(self, job: dict) -> Path:
        return self.path("jobs", job["id"], f"attempt-{job['attempts']}")

    def execute_job(self, job: dict) -> dict:
        p = job["payload"]
        topic = p["topic"]
        budgets = self.cfg["budgets_seconds"]
        common = {"guard": P.GUARD, "question": self.question, "topic_title": topic["title"], "topic_need": topic["need"]}
        if job["kind"] == "search":
            prompt = P.SEARCH.format(query=p["query"], picks=self.cfg["limits"]["picks_per_search"], **common)
            res = self.call("search", prompt, self._job_dir(job), model=self.model("worker"),
                            toolsets=self.cfg["toolsets"]["worker"], budget=budgets["search"], job_id=job["id"],
                            reasoning_role="worker")
            reply = fw.parse_json_reply(res.stdout)
            if reply is None:
                raise ValueError(f"no JSON reply (exit {res.exit_code})")
            # The search results the tool really returned are the only evidence a pick is real.
            # Without them the job lands with no usable picks; a paid search is not repeated.
            results, evidence = [], "ok"
            if not res.session_id:
                evidence = "no_session"
            else:
                try:
                    results = self.harvest_search(res.session_id)
                except Exception as exc:  # noqa: BLE001
                    evidence = f"harvest_failed: {exc!r}"[:300]
                if evidence == "ok" and not results:
                    evidence = "no_search_results"
            return {"session_id": res.session_id, "exit_code": res.exit_code, "reply": reply,
                    "search_results": results[:60], "search_evidence": evidence}
        url = p["url"]
        saved = self.store.get_page(url)
        lease = None
        if saved is None and job["kind"] == "fetch":
            saved, lease = self._fetch_lease_or_page(job, url)
        if saved is None and job["kind"] == "fetch":
            prompt = P.FETCH.format(url=url, claims=self.cfg["limits"]["claims_per_page"], **common)
            budget, mode = budgets["fetch"], "fetch"
        elif saved is not None:
            prompt = P.READ.format(url=url, path=str(self.root / saved["path"]),
                                   claims=self.cfg["limits"]["claims_per_page"], **common)
            budget, mode = budgets["read"], "read"
        else:
            raise ValueError("read job has no saved page")
        pages = []
        try:
            res = self.call(job["kind"], prompt, self._job_dir(job), model=self.model("worker"),
                            toolsets=self.cfg["toolsets"]["worker"], budget=budget, job_id=job["id"],
                            reasoning_role="worker", lease_url=lease)
            if mode == "fetch" and res.session_id:
                # Save what the tool returned before anything else, so a retry never fetches again.
                pages = self._save_fetched(res.session_id, url, self.run_id, job["id"])
        except Exception:
            if lease:
                self.store.release_fetch_lease(lease, self.lease_owner)
            raise
        # A controller that is killed here keeps its lease; other runs take it over once it is dead.
        if lease:
            self.store.release_fetch_lease(lease, self.lease_owner)
        if saved is not None:
            pages.append({"url": saved["url"], "sha256": saved["sha256"]})
        reply = fw.parse_json_reply(res.stdout)
        if reply is None:
            raise ValueError(f"no JSON reply (exit {res.exit_code})")
        return {"session_id": res.session_id, "exit_code": res.exit_code, "mode": mode, "reply": reply, "pages": pages}

    def _save_fetched(self, session_id: str, url: str, run_id: str, job_id: str) -> list[dict]:
        """Save the pages a fetch worker's session received from the tool. Returns their urls and hashes."""
        pages = []
        for page in self.harvest_pages(session_id):
            row = self.store.save_page(page["url"], page["text"], title=page.get("title"), origin=page.get("origin", "web_extract"),
                                       run_id=run_id, job_id=job_id)
            pages.append({"url": row["url"], "sha256": row["sha256"]})
        if pages and self.store.get_page(url) is None:
            # The tool reports the final URL after redirects: also index the page under the asked URL.
            first = self.store.get_page(pages[0]["url"])
            self.store.save_page(url, first["text"], title=first["title"], final_url=first["final_url"],
                                 origin=first["origin"], run_id=run_id, job_id=job_id)
        return pages

    def _fetch_lease_or_page(self, job: dict, url: str) -> tuple[dict | None, str | None]:
        """Reserve ``url`` for this fetch across all runs on the root, or wait for the run fetching it.

        Returns ``(saved_page, None)`` once the page is saved, or ``(None, key)`` when this job holds
        the lease and should fetch. A lease is taken over only when its recorded worker has ended
        and either its controller is gone or the lease is past its expiry. Before fetching, the new
        holder harvests the earlier worker's finished session. It fetches only if that left no page.
        """
        key = norm_url(url)
        ttl = self.cfg["budgets_seconds"]["fetch"] + 90 + LEASE_GRACE_SECONDS
        ids = {"run_id": self.run_id, "job_id": job["id"], "ttl": ttl, "host": self.host, "pid": self.pid}
        waited = False
        while True:
            state, holder = self.store.acquire_fetch_lease(key, self.lease_owner, **ids)
            prev = None
            if state == "held" and self._may_take_over(holder):
                state = self.store.take_over_fetch_lease(key, holder["owner"], self.lease_owner, **ids,
                                                         expect_worker=(holder["worker_pid"], holder["worker_dir"]))
                if state == "acquired":
                    prev = holder
                    self.log(f"{job['id']}: took over fetch lease on {url} from {holder['run_id']}/{holder['job_id']} "
                             f"(controller {holder['host']}:{holder['pid']} gone or lease expired, worker ended)")
            if state == "acquired":
                page = self._adopt_previous_fetch(job, key, url, prev)
                if page is not None:
                    self.store.release_fetch_lease(key, self.lease_owner)
                    return page, None
                return None, key
            if state == "saved":
                if waited:
                    self.log(f"{job['id']}: {url} was saved by another run, reading the saved copy")
                return self.store.get_page(key), None
            if not waited and holder:
                waited = True
                self.log(f"{job['id']}: waiting for fetch lease on {url} held by {holder['run_id']}/{holder['job_id']}")
            time.sleep(self.lease_poll)

    def _may_take_over(self, holder: dict | None) -> bool:
        """True when another holder's lease may be taken: its worker has ended, and its controller is gone
        or the lease is past its expiry. Expiry alone is never enough while the recorded worker runs."""
        if not holder:
            return False
        expired = holder["expires_at"] <= time.time()
        if holder.get("host") != self.host:
            return expired  # that host's processes cannot be seen from here: only the expiry frees it
        if self._lease_worker_active(holder):
            return False
        if expired:
            return True
        if not holder.get("pid") or holder["pid"] == self.pid:
            return False  # this process holds it, so its controller is alive
        try:
            os.kill(holder["pid"], 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    def _worker_meta(self, worker_dir: str | None) -> dict:
        """The meta.json of a worker folder inside this root's runs, or {}."""
        if not worker_dir:
            return {}
        try:
            path = contained(self.root / "runs", Path(worker_dir) / "meta.json")
            return json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            return {}

    def _lease_worker_active(self, lease: dict) -> bool:
        """True when the lease's recorded worker is a live process on this host with its folder on its command line."""
        wd = lease.get("worker_dir")
        if not wd:
            return False
        pid = lease.get("worker_pid") or self._worker_meta(wd).get("pid")
        return fw._pid_alive(pid, wd)

    def _adopt_previous_fetch(self, job: dict, key: str, url: str, prev: dict | None) -> dict | None:
        """Harvest a fetch worker recorded on a lease this job now holds, before any new fetch.

        This includes this job's own attempt folder. Harvest its session before ``fw.oneshot``
        can archive empty stdout and launch another fetch. Wait for a live worker to end first.
        Returns the saved page when the earlier worker's session gave it, else None.
        """
        lease = self.store.fetch_lease(key) or {}
        wd = lease.get("worker_dir")
        if not wd:
            return None
        while self._lease_worker_active(lease):
            time.sleep(self.lease_poll)
        meta = self._worker_meta(wd)
        try:
            done = fw._finished(contained(self.root / "runs", wd)) if meta else None
        except (OSError, ValueError):
            done = None
        credit_run, credit_job = (prev["run_id"], prev["job_id"]) if prev else (self.run_id, job["id"])
        if done and done.session_id:
            try:
                pages = self._save_fetched(done.session_id, url, credit_run, credit_job)
            except Exception as exc:  # noqa: BLE001  an unreadable session counts as no usable page
                pages = []
                self.log(f"{job['id']}: could not harvest earlier worker session {done.session_id}: {exc!r}")
            if credit_run and self.store.get_run(credit_run):
                self.store.add_call(credit_run, done.session_id, "fetch", meta.get("model"), credit_job,
                                    done.started_at, done.ended_at, done.exit_code)
            page = self.store.get_page(key)
            if page is not None:
                self.log(f"{job['id']}: adopted the finished fetch of {url} by {credit_run}/{credit_job} "
                         f"(session {done.session_id}, {len(pages)} page(s)), reading the saved copy")
                return page
        self.log(f"{job['id']}: earlier worker for {url} in {wd} left no usable page, fetching")
        return None

    def on_job_done(self, job: dict, result: dict):
        if job["kind"] == "search":
            self._enqueue_pages(job, result)
        else:
            self._check_findings(job, result)

    def _enqueue_pages(self, job: dict, result: dict):
        rows = result.get("search_results")
        seen = {norm_url(r["url"]) for r in (rows if isinstance(rows, list) else [])
                if isinstance(r, dict) and isinstance(r.get("url"), str) and r["url"].strip()}
        picks = (result.get("reply") or {}).get("picks") or []
        if not seen:
            # No search evidence (never searched, harvest failed, nothing parseable): no pick is usable.
            self.log(f"{job['id']}: no_search_evidence ({result.get('search_evidence') or 'no search results'}), "
                     f"{len(picks) if isinstance(picks, list) else 0} pick(s) dropped")
            return
        outcomes = []
        for pick in picks[: self.cfg["limits"]["picks_per_search"]]:
            url = (pick or {}).get("url") if isinstance(pick, dict) else None
            if not url or not _url_ok(url):
                outcomes.append({"url": url, "outcome": "bad_url"})
                continue
            key = norm_url(url)
            if key not in seen:
                outcomes.append({"url": url, "outcome": "not_in_search_results"})
                continue
            job_id = "page-" + hashlib.sha1(key.encode()).hexdigest()[:16]
            with self._lock:
                kind = "read" if self.store.get_page(key) else "fetch"
                outcome = self.store.add_job_within(self.run_id, job_id, kind, {"url": url, "topic": job["payload"]["topic"]},
                                                    job["topic"], self._limits(), self.cfg["limits"]["max_attempts"])
            outcomes.append({"url": url, "outcome": outcome, "kind": kind})
        self.log(f"{job['id']}: picks {json.dumps(outcomes)}")

    def _check_findings(self, job: dict, result: dict):
        reply = result.get("reply") or {}
        pages = [self.store.get_page(p["url"]) for p in result.get("pages") or []]
        primary = self.store.get_page(job["payload"]["url"])
        candidates, seen = [], set()
        for p in [primary, *pages]:
            if p and p["url"] not in seen:
                seen.add(p["url"])
                candidates.append(p)
        n_pass = n_fail = 0
        quote_ok: list[int] = []
        for item in (reply.get("claims") or [])[: self.cfg["limits"]["claims_per_page"]]:
            if not isinstance(item, dict):
                continue
            text, quote = str(item.get("claim") or "").strip(), str(item.get("quote") or "").strip()
            if not text or not quote:
                continue
            cid = self.store.add_claim(self.run_id, job["id"], text, quote, candidates[0]["url"] if candidates else None,
                                       candidates[0]["sha256"] if candidates else None, job["topic"])
            # A finding with a saved meaning verdict goes straight to its final status when its quote
            # still checks out, so handling a job again never shows a passed finding as unchecked.
            status, reason = self._status_after_quote(cid)
            ok, _ = check_claim(self.store, cid, candidates, pass_status=status, pass_reason=reason)
            n_pass += ok
            n_fail += not ok
            if ok and cid not in quote_ok:
                quote_ok.append(cid)
        self.log(f"{job['id']}: {len(candidates)} page(s), quotes found {n_pass} not found {n_fail}")
        if quote_ok:
            self._meaning_check(job, quote_ok)

    def _meaning_check(self, job: dict, claim_ids: list[int]):
        """Ask the checker model whether each quote supports its claim, and set each finding's status.

        One call per page job. Saved verdicts are reused, so a resumed run never asks twice. The
        worker folder is named by the findings it covers, so a resumed call adopts only its own reply.
        A finding with no usable verdict is kept out of the answer.
        """
        pending = [cid for cid in claim_ids if self.store.support(cid) is None]
        if pending:
            claims = {cid: self.store.claim(cid) for cid in pending}
            def source(c):
                page = self.store.get_page(c["page_url"]) if c["page_url"] else None
                title = " ".join(str((page or {}).get("title") or "untitled").split())[:200]
                return f"{title} <{(page or {}).get('final_url') or c['page_url']}>"

            listing = "\n".join(f"{i}. Claim: {claims[cid]['text']}\n   Quote: \"{claims[cid]['quote']}\"\n"
                                 f"   Page: {source(claims[cid])}" for i, cid in enumerate(pending, 1))
            prompt = P.CHECK.format(findings=listing)
            tag = hashlib.sha1(",".join(map(str, pending)).encode()).hexdigest()[:10]
            parsed, res = None, None
            for attempt in range(1, 3):
                try:
                    res = self.call("checker", prompt, self.path("jobs", job["id"], f"check-{tag}", f"attempt-{attempt}"),
                                    budget=self.cfg["budgets_seconds"]["check"], job_id=None)
                except Exception as exc:  # noqa: BLE001  a broken checker call leaves findings unchecked
                    self.log(f"{job['id']}: meaning check call failed {exc!r}")
                    continue
                parsed = fw.parse_json_reply(res.stdout)
                if parsed and isinstance(parsed.get("checks"), list):
                    break
                parsed = None
            model, sid = self.model("checker"), (res.session_id if res else None)
            kinds = {}
            for row in (parsed or {}).get("checks", []):
                n = row.get("n") if isinstance(row, dict) else None
                if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= len(pending):
                    kinds.setdefault(n, fg.normalise_kind(row.get("source_kind")))
            rows = []
            for i, (cid, (verdict, narrowed, reason)) in enumerate(zip(pending, parse_checks(parsed, claims, pending)), 1):
                rows.append({"claim_id": cid, "run_id": claims[cid]["run_id"], "verdict": verdict, "supported_text": narrowed,
                             "reason": reason, "model": model, "session_id": sid, "source_kind": kinds.get(i, "unknown")})
            self.store.set_supports(rows)
        tally: dict[str, int] = {}
        for cid in claim_ids:
            sup = self.store.support(cid)
            status, reason = self._status_after_quote(cid)
            self.store.set_claim_check(cid, status, reason)
            tally[sup["verdict"]] = tally.get(sup["verdict"], 0) + 1
        self.log(f"{job['id']}: meaning check {json.dumps(tally, sort_keys=True)}")

    def _status_after_quote(self, claim_id: int) -> tuple[str, str]:
        """The status a finding takes once its quote is found: ``quote_ok`` until it has a meaning verdict."""
        sup = self.store.support(claim_id)
        if sup is None:
            return "quote_ok", "ok"
        status = SUPPORT_STATUS[sup["verdict"]]
        if status != "pass":
            return status, sup["reason"] or sup["verdict"]
        return status, "narrowed" if sup["verdict"] == "partly" else "ok"

    def _admitted(self) -> list[dict]:
        """Findings the writer may use: status pass and a saved supported or valid partly verdict."""
        out = []
        for c in [c for rid in self.source_run_ids() for c in self.store.claims(rid, status="pass")]:
            sup = self.store.support(c["id"])
            if sup and (sup["verdict"] == "supported" or (sup["verdict"] == "partly" and sup["supported_text"])):
                out.append(c)
        return out

    def _check_legacy_passes(self):
        """Meaning-check findings that passed only the quote check under older code, before any answer."""
        by_job: dict[str, list[int]] = {}
        for c in [c for rid in self.source_run_ids() for c in self.store.claims(rid, status="pass")]:
            if self.store.support(c["id"]) is None:
                by_job.setdefault(c["job_id"], []).append(c["id"])
        for job_id, ids in by_job.items():
            self.log(f"answer: {len(ids)} finding(s) from {job_id} passed only the quote check, meaning-checking them now")
            self._meaning_check({"id": job_id}, ids)

    def _answer_meaning_checked(self) -> bool:
        """True when every citation in the saved answer points at a finding the writer may use,
        with the same claim text the writer may use now.

        A narrowed verdict saved after the answer was written leaves a citation with the older,
        broader text, so the answer is rewritten. This holds after a crash at any point, because
        it is worked out from saved state on every pass.
        """
        path = self.dir / "citations.pre-check.json"
        if not path.exists():
            path = self.dir / "citations.json"
        if not path.exists():
            return False
        admitted = {c["id"]: c for c in self._admitted()}
        citations = json.loads(path.read_text(encoding="utf-8"))
        for cit in citations:
            claim = admitted.get(int(cit.get("claim_id") or 0))
            if claim is None or cit.get("claim") != self._finding_text(claim):
                return False
        return bool(citations)

    def _checker_independent(self):
        """Stop if the meaning checks and the reading workers were served by the same vendor.

        Both sides use the served model from the session records, and the requested model only
        where no served model is recorded.
        """
        calls = [c for rid in dict.fromkeys([self.run_id, *self.source_run_ids()]) for c in self.store.calls(rid)]
        served = self._served_models([c["session_id"] for c in calls])

        def vendors(roles):
            return {vendor(served.get(c["session_id"]) or c["model"]) for c in calls if c["role"] in roles} - {None}

        checker, workers = vendors({"checker"}), vendors({"fetch", "read"})
        same = sorted(checker & workers)
        if same:
            raise PhaseError(f"meaning checks and reading workers were served by the same vendor: {', '.join(same)}")

    def _support_counts(self) -> dict:
        out: dict[str, int] = {}
        for row in [s for rid in self.source_run_ids() for s in self.store.supports(rid)]:
            out[row["verdict"]] = out.get(row["verdict"], 0) + 1
        return out

    def _finding_text(self, claim: dict) -> str:
        """The claim text the writer may use: the narrowed text when the quote supports only part."""
        sup = self.store.support(claim["id"])
        return sup["supported_text"] if sup and sup["verdict"] == "partly" and sup["supported_text"] else claim["text"]

    # -- answer -----------------------------------------------------------------------------
    def _topics(self) -> list[dict]:
        path = self.dir / "plan.json"
        return json.loads(path.read_text(encoding="utf-8"))["topics"] if path.exists() else []

    def _findings(self, claims):
        lines = []
        for c in claims:
            page = self.store.get_page(c["page_url"])
            lines.append(f"[C{c['id']}] (topic {c['topic']}) {self._finding_text(c)}\n  Quote: \"{c['quote']}\"\n"
                         f"  Source: {page['title'] or ''} {page['final_url'] or page['url']}")
        return "\n".join(lines)

    def answer(self) -> dict:
        self._check_legacy_passes()
        self._checker_independent()
        claims = self._admitted()
        if not claims:
            raise PhaseError("no finding passed the quote and meaning checks, so there is nothing to cite")
        topics = self._topics()
        covered = {c["topic"] for c in claims}
        gaps = [t["title"] for t in topics if t["id"] not in covered] or ["none"]
        prompt = P.ANSWER.format(question=self.question,
                                 topics="\n".join(f"- {t['id']}: {t['title']}. {t['need']}" for t in topics),
                                 findings=self._findings(claims), gaps="; ".join(gaps))
        return self._write_answer(prompt, claims)

    def _write_answer(self, prompt, claims):
        body = ""
        for attempt in range(1, 3):
            res = self.call("writer", prompt, self.path("answer", f"attempt-{attempt}"), budget=self.cfg["budgets_seconds"]["answer"])
            body = res.stdout.strip()
            if FINDING_RE.search(body):
                break
        if not FINDING_RE.search(body):
            raise PhaseError("writer gave no answer with finding ids")
        return self._save_answer(body, claims)

    def _save_answer(self, body: str, claims: list[dict]) -> dict:
        """Use the same renderer and fail-closed id rule for fast and combined answers."""
        rendered, citations, dropped = self.render(body, {c["id"]: c for c in claims})
        if dropped:
            # Fail closed: the sentence around an id that is not a checked finding is unsupported.
            # Keep the writer's draft and say which ids were wrong; deliver no answer.
            draft = self.path("answer_draft.md")
            draft.write_text(body + "\n", encoding="utf-8")
            self.write_json("quote_check.json", {
                "verdict": "fail", "citations": 0, "passed": 0, "dropped_markers": dropped, "draft_path": str(draft),
                "problems": [{"n": None, "kind": "unknown_finding_id", "id": t} for t in dict.fromkeys(dropped)]})
            self.log(f"answer: writer cited ids that are not checked findings {dropped}, draft kept in {draft.name}")
            raise PhaseError(f"writer cited finding ids that are not checked findings of this run: "
                             f"{', '.join(dict.fromkeys(dropped))}. No answer delivered, draft kept in {draft.name}")
        self.path("answer.md").write_text(rendered, encoding="utf-8")
        self.write_json("citations.json", citations)
        self._save_evidence(citations)
        qc = check_answer(self.store, self.run_id, rendered, citations, run_ids=self.source_run_ids())
        qc["dropped_markers"] = dropped
        self.write_json("quote_check.json", qc)
        self.log(f"answer: {len(citations)} citations, quote check {qc['verdict']}, dropped markers {dropped}")
        return {"citations": len(citations), "quote_check": qc["verdict"], "dropped_markers": dropped,
                "answer_path": str(self.dir / "answer.md")}

    def render(self, body: str, passed: dict[int, dict], grade_info: dict | None = None) -> tuple[str, list[dict], list[str]]:
        """Swap finding ids for numbers in order of first use and add the sources list.

        Ids that are not checked findings of this run are never rendered. They are returned in the
        third value, and ``answer`` then stops the run instead of delivering the text.
        """
        numbers: dict[int, int] = {}
        dropped: list[str] = []

        def swap(m):
            out = []
            for token in re.split(r"\s*[,;]\s*", m.group(1)):
                cid = int(token[1:])
                if cid not in passed:
                    dropped.append(token)
                    continue
                numbers.setdefault(cid, len(numbers) + 1)
                if f"[{numbers[cid]}]" not in out:
                    out.append(f"[{numbers[cid]}]")
            return "".join(out)

        text = FINDING_RE.sub(swap, body)
        if dropped:
            text = re.sub(r"[ \t]+([.,])", r"\1", text)
        citations, pages = [], {}
        for cid, n in sorted(numbers.items(), key=lambda kv: kv[1]):
            c = passed[cid]
            page = self.store.get_page(c["page_url"])
            citations.append({"n": n, "claim_id": cid, "claim": self._finding_text(c), "original_claim": c["text"],
                              "quote": c["quote"], "page_url": c["page_url"],
                              "source_url": page["final_url"] or page["url"], "title": page["title"],
                              "page_sha256": c["page_sha256"], "page_path": page["path"]})
            if grade_info is not None:
                citations[-1]["grade"] = fg.grade([grade_info[cid]])
            url = page["final_url"] or page["url"]
            entry = pages.setdefault(url, {"title": (page["title"] or page["url"]).strip(), "numbers": []})
            entry["numbers"].append(n)
        sources = ["- " + "".join(f"[{n}]" for n in entry["numbers"]) + f" {entry['title']}. {url}"
                   for url, entry in pages.items()]
        head = f"# {self.question}\n\n"
        return head + text.strip() + "\n\n## Sources\n\n" + "\n".join(sources) + "\n", citations, dropped

    def _save_evidence(self, citations, removed=()):
        entries = []
        for c in citations:
            grade = c.get("grade")
            grade_line = f"\n\nSource grade: {grade['code']}, {grade['words']}." if grade and self.show_grades else ""
            entries.append(f"## [{c['n']}]\n\n{c['title'] or c['source_url']}. {c['source_url']}\n\n"
                           f"Quote: \"{c['quote']}\"\n\nSaved page: {c['page_path']}" + grade_line)
        if removed:
            entries.append("## Removed because the sources did not support them\n\n" +
                           "\n".join(f"- {s}" for s in removed))
        self.path("evidence.md").write_text("# Evidence\n\n" + "\n\n".join(entries) + "\n", encoding="utf-8")

    # -- sentence attribution and grades ---------------------------------------------------
    def _sentence_checker_independent(self):
        calls = self.store.calls(self.run_id)
        served = self._served_models([c["session_id"] for c in calls])
        writers = {vendor(served.get(c["session_id"]) or c["model"]) for c in calls if c["role"] == "writer"}
        writers = writers or {vendor(self.model("writer"))}
        checkers = {vendor(served.get(c["session_id"]) or c["model"]) for c in calls if c["role"] == "sentence_checker"}
        checkers = checkers or {vendor(self.model("checker"))}
        if None in checkers or checkers & writers:
            raise PhaseError("sentence checker must be served by a different vendor than the writer")

    def _answer_sentences(self, body, by_n):
        # A paragraph or a list item supplies context for an inference, never its neighbour.
        groups, group, blank, bullet_indent = {}, 0, True, None
        for i, line in enumerate(body.split("\n")):
            if not line.strip():
                blank = True
                continue
            bullet = re.match(r"(\s*)(?:[-*+]|\d+[.)])\s+", line)
            indent = len(line) - len(line.lstrip())
            if bullet or line.lstrip().startswith(("#", "```")):
                group += 1
                bullet_indent = len(bullet.group(1)) if bullet else None
            elif blank and not (bullet_indent is not None and indent > bullet_indent):
                group += 1
                bullet_indent = None
            groups[i], blank = group, False
        rows = fg.split_sentences(body)
        nearby = {}
        for s in rows:
            nearby.setdefault(groups[s["line"]], []).extend(n for n in s["cites"] if n in by_n)
        out = []
        for s in rows:
            inference = bool(re.match(r"^(?:\*\*[^*]+\*\*\s+)?\**my inference:", s["text"], re.I))
            cites = list(dict.fromkeys(nearby[groups[s["line"]]])) if inference else [n for n in s["cites"] if n in by_n]
            if cites or inference:
                out.append({**s, "cites": cites, "inference": inference})
        return out

    def check(self) -> dict:
        # Immutable inputs let an interrupted phase retry without applying marks twice.
        for source, backup in (("answer.md", "answer.pre-check.md"), ("citations.json", "citations.pre-check.json")):
            if not self.path(backup).exists():
                self.path(backup).write_bytes(self.path(source).read_bytes())
        original = self.path("answer.pre-check.md").read_text(encoding="utf-8")
        citations = json.loads(self.path("citations.pre-check.json").read_text(encoding="utf-8"))
        body = original.split("\n## Sources", 1)[0].split("\n", 1)[1].strip()
        by_n = {c["n"]: c for c in citations}
        sentences = self._answer_sentences(body, by_n)
        self._sentence_checker_independent()
        signature = hashlib.sha256((body + json.dumps(citations, sort_keys=True)
                                    + json.dumps(sentences, sort_keys=True)).encode()).hexdigest()
        saved = self._read_json("sentence_check.json") or {}
        checked = saved.get("sentences") if saved.get("input_sha256") == signature else None
        if checked is None:
            checked = []
            cited = [s for s in sentences if s["cites"]]
            for offset in range(0, len(cited), 20):
                batch = cited[offset:offset + 20]
                inputs, listing = {}, []
                for i, s in enumerate(batch, 1):
                    sources = [by_n[n] for n in s["cites"] if n in by_n]
                    inputs[i] = {"quote": "\n".join(c["quote"] for c in sources)}
                    listing.append(f"{i}. Claim: {fg.sub_cites('', s['text']).strip()}\n" + "\n".join(
                        f"   Quote: \"{c['quote']}\"\n   Page: {c['title']} <{c['source_url']}>" for c in sources))
                pending = list(inputs)
                results = parse_checks(None, inputs, pending)
                for attempt in range(1, 3):
                    try:
                        res = self.call("sentence_checker", P.SENTENCE_CHECK.format(findings="\n".join(listing)),
                                        self.path("sentence-check", signature[:12], str(offset), f"attempt-{attempt}"),
                                        model=self.model("checker"), reasoning_role="checker",
                                        budget=self.cfg["budgets_seconds"]["check"])
                        results = parse_checks(fw.parse_json_reply(res.stdout), inputs, pending)
                    except Exception as exc:
                        self.log(f"sentence check: call failed {exc!r}")
                    self._sentence_checker_independent()
                    if any(verdict != "unchecked" for verdict, _, _ in results):
                        break
                for s, (verdict, narrowed, reason) in zip(batch, results):
                    checked.append({**s, "verdict": verdict, "narrowed": narrowed, "reason": reason})
            checked.extend({**s, "verdict": "unsupported", "narrowed": None,
                            "reason": "no citations in the same paragraph or bullet"}
                           for s in sentences if s["inference"] and not s["cites"])
            checked.sort(key=lambda s: (s["line"], s["start"]))
        facts = [s for s in checked if not s["inference"]]
        counts = {"checked": sum(s["verdict"] != "unchecked" for s in facts),
                  "narrowed": sum(s["verdict"] == "partly" for s in facts),
                  "removed": sum(s["verdict"] == "unsupported" for s in facts),
                  "unchecked": sum(s["verdict"] == "unchecked" for s in facts)}
        inferences = [s for s in checked if s["inference"]]
        counts.update(inference_checked=sum(s["verdict"] != "unchecked" for s in inferences),
                      inference_narrowed=sum(s["verdict"] == "partly" for s in inferences),
                      inference_removed=sum(s["verdict"] == "unsupported" for s in inferences),
                      inference_unchecked=sum(s["verdict"] == "unchecked" for s in inferences))
        saved = {"input_sha256": signature, "sentences": checked, "counts": counts}
        self.write_json("sentence_check.json", saved)
        # Origin lookup uses cached normalised row text. The final check still verifies file hashes.
        pages = {c["page_url"]: self.store.get_page(c["page_url"]) for c in citations}
        page_text = {url: _norm_page(p["sha256"], p["text"]) for url, p in pages.items() if p}
        quotes = {c["quote"]: norm(c["quote"]) for c in citations}
        origins = fg.origins(citations, lambda quote, url: len(quotes[quote]) >= MIN_QUOTE_CHARS
                             and quotes[quote] in page_text.get(url, ""))
        kinds = self._page_kinds({c["page_url"] for c in citations})
        info = {}
        for c in citations:
            support = self.store.support(c["claim_id"]) or {}
            info[c["n"]] = {"verdict": support.get("verdict"), "kind": kinds.get(c["page_url"], "unknown"),
                            "origin": origins[c["n"]]}
        lines, removed, graded = body.split("\n"), [], []
        # Use offsets in the original line so repeated sentences are changed once each.
        replacements = {}
        for s in checked:
            markers = "" if s["inference"] else "".join(f"[{n}]" for n in s["cites"])
            plain = fg.sub_cites("", s["text"]).strip()
            replacement = plain if s["inference"] else s["text"]
            if s["verdict"] == "partly":
                narrowed = fg.sub_cites("", FINDING_RE.sub("", s["narrowed"])).strip()
                if s["inference"]:
                    narrowed = "My inference: " + re.sub(r"^\**my inference:\**\s*", "", narrowed, flags=re.I)
                replacement = narrowed + ("" if re.search(r"[.!?][\"'”’)]*$", narrowed) else ".") + markers
            elif s["verdict"] == "unsupported":
                replacement = ""
                removed.append(plain)
            elif s["verdict"] == "unchecked":
                replacement += " (Not checked)"
            if s["verdict"] != "unsupported":
                supports = [info[n] for n in s["cites"] if n in info]
                if s["verdict"] == "partly":
                    supports = [{**support, "verdict": "partly"} for support in supports]
                # Unchecked keeps its meaning-check grade. The separate mark reports the missing check.
                g = fg.grade(supports)
                graded.append({**s, "text": replacement, **g})
                if self.show_grades and fg.is_low(g):
                    replacement += f" (grade {g['code']}: {g['words']})"
            replacements.setdefault(s["line"], []).append((s["text"], replacement))
        for line, changes in replacements.items():
            start, parts = 0, []
            for old, new in changes:
                pos = lines[line].find(old, start)
                before = lines[line][start:pos]
                start = pos + len(old)
                if not new:
                    # Remove the redundant separator, not spaces inside code or list indentation.
                    while start < len(lines[line]) and lines[line][start] in " \t":
                        start += 1
                    if start == len(lines[line]):
                        before = before.rstrip(" \t")
                parts.extend((before, new))
            changed = "".join(parts) + lines[line][start:]
            lines[line] = "" if re.fullmatch(r"\s*(?:[-*+]|\d+[.)])\s*", changed) else changed
        fixed = "\n".join(lines)
        # The renderer accepts finding ids, not the old numbered citations.
        fixed = fg.sub_cites(lambda m: f"[C{by_n[n]['claim_id']}]" if (n := int(m.group(1))) in by_n else m.group(0), fixed)
        rendered, citations, dropped = self.render(fixed, {c["id"]: c for c in self._admitted()},
                                                   {c["claim_id"]: info[c["n"]] for c in by_n.values()})
        if dropped:
            raise PhaseError("sentence check referenced a finding that is no longer admitted")
        body, sources = rendered.split("\n## Sources", 1)
        new_numbers = {c["claim_id"]: c["n"] for c in citations}
        for g in graded:
            remap = {n: new_numbers[by_n[n]["claim_id"]] for n in g["cites"]
                     if n in by_n and by_n[n]["claim_id"] in new_numbers}
            g["text"] = fg.sub_cites(lambda m: f"[{remap[n]}]" if (n := int(m.group(1))) in remap else m.group(0), g["text"])
            g["cites"] = [remap[n] for n in g["cites"] if n in remap]
        body, not_verified = fg.mark_not_verified(body)
        self._save_evidence(citations, removed)
        text = body + "\n## Sources" + sources
        grades = {"scheme": fg.SCHEME, "shown_in_answer": self.show_grades, "counts": fg.grade_counts(graded), "sentences": graded,
                  "not_verified": not_verified, "citation_recall": fg.citation_recall([s["verdict"] for s in facts if s["cites"]]),
                  "citation_precision": None, "precision_note": "not measured", "sentence_check_counts": saved["counts"]}
        self.write_json("grades.json", grades)
        self.write_json("not_verified.json", not_verified)
        self.path("answer.md").write_text(text, encoding="utf-8")
        self.write_json("citations.json", citations)
        self._final_quote_check()
        return saved["counts"]

    def _page_kinds(self, urls) -> dict:
        """One source kind per page: the kind the checker gave most often across its findings.

        Only this run's own findings vote, so other questions on the same root never change its grades.
        The checker judges each finding on its own, so one page can get two kinds. A tie goes to the
        kind with the lower grade letter, so a page never grades higher than its checks agree on.
        """
        votes: dict = {}
        run_ids = list(self.source_run_ids())
        marks = ",".join("?" * len(run_ids))
        for url in urls:
            rows = self.store.conn().execute(
                "SELECT s.source_kind FROM claims c JOIN claim_support s ON s.claim_id=c.id "
                f"WHERE c.page_url=? AND c.run_id IN ({marks})", (url, *run_ids)).fetchall()
            for (kind,) in rows:
                kind = fg.normalise_kind(kind)
                votes.setdefault(url, {}).setdefault(kind, 0)
                votes[url][kind] += 1
        # A missing or unreadable kind is not a vote while any real kind was given.
        votes = {url: ({k: n for k, n in v.items() if k != "unknown"} or v) for url, v in votes.items()}
        rank = lambda k: fg.LETTER_ORDER.index(fg.KIND_LETTER.get(k, "F"))  # noqa: E731
        return {url: max(v, key=lambda k: (v[k], rank(k))) for url, v in votes.items()}

    def _final_quote_check(self):
        qc = check_answer(self.store, self.run_id, self.path("answer.md").read_text(encoding="utf-8"),
                          self._read_json("citations.json"), run_ids=self.source_run_ids())
        qc["dropped_markers"] = []
        self.write_json("quote_check.json", qc)
        return qc

    def _read_json(self, name):
        path = self.path(name)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def _review_issues(self):
        review = (self._read_json("review.json") or {}).get("review") or {}
        return {key: len(review.get(key)) if isinstance(review.get(key), list) else 0 for key in
                ("unsupported_citations", "uncited_claims", "missing_parts", "other_issues")}

    def _usable_review(self):
        saved = self._read_json("review.json") or {}
        review = saved.get("review")
        if (saved.get("different_vendor") is True and isinstance(review, dict)
                and isinstance(review.get("verdict"), str) and saved.get("verdict") == review.get("verdict")):
            return review
        return {}

    NOT_REVIEWED = "Not reviewed. Check the sources before relying on it."
    ANSWERED = "Answers the question. Notes are at the end."
    PART_ANSWERED = "Answers part of the question. Still missing:"

    def _missing_parts(self) -> list[str]:
        """Every non-empty missing part from a usable review, whitespace collapsed."""
        missing = self._usable_review().get("missing_parts")
        return [" ".join(item.split()) for item in (missing if isinstance(missing, list) else [])
                if isinstance(item, str) and item.strip()]

    def readiness(self):
        # The reviewer's verdict does not set this line. It rejected good answers too, so a
        # verdict-based line misled. The line says only whether a usable review ran and
        # whether it named missing parts. The verdict stays in review.json and run.json.
        if not self._usable_review():
            return self.NOT_REVIEWED
        return self.PART_ANSWERED if self._missing_parts() else self.ANSWERED

    def finish(self) -> dict:
        grades = self._read_json("grades.json") or {}
        counts = grades.get("sentence_check_counts", {})
        issues = self._review_issues()
        review = (self._read_json("review.json") or {}).get("review") or {}
        items = review.get("unsupported_citations")
        questioned = [str(c["n"]) for c in items if isinstance(c, dict) and "n" in c] if isinstance(items, list) else []
        header = (f"Sentence check: {counts.get('checked', 0)} checked, {counts.get('narrowed', 0)} narrowed, {counts.get('removed', 0)} removed.\n\n"
                  + (f"Fact grades (NATO Admiralty Code): {fg.summary_line(grades.get('counts', {}))}.\n\n"
                     if self.show_grades else "")
                  + f"Citation recall: {grades.get('citation_recall')}.\n\n"
                  f"Not verified: {len(grades.get('not_verified', []))}.\n\n"
                  f"Review: questioned citations {issues['unsupported_citations']} ({', '.join(questioned) or 'none'}), "
                  f"uncited claims {issues['uncited_claims']}, missing parts {issues['missing_parts']}, other issues {issues['other_issues']}.\n\n")
        text = self.path("answer.md").read_text(encoding="utf-8")
        text = re.sub(r"<!-- research-check -->.*?<!-- /research-check -->\n*", "", text, flags=re.S)
        text = re.sub(r"\n[^\n]*\n<!-- research-readiness -->.*?<!-- /research-readiness -->\n*", "\n", text, flags=re.S)
        title, body = text.split("\n", 1)
        body, sources = body.split("\n## Sources", 1)
        # Every missing part is listed. Review references are text, not extra answer citations.
        bullets = ["- " + item.replace("[", r"\[").replace("]", r"\]") for item in self._missing_parts()]
        lead = self.readiness() + "\n<!-- research-readiness -->\n" + "\n".join(bullets) + "\n<!-- /research-readiness -->\n\n"
        checks = "<!-- research-check -->\n## Checks\n\n" + header + "Evidence: [evidence.md](evidence.md). Review notes: [review.json](review.json).\n\n<!-- /research-check -->\n"
        self.path("answer.md").write_text(title + "\n\n" + lead + body.strip() + "\n\n" + checks + "\n## Sources" + sources, encoding="utf-8")
        return {"review_issue_counts": issues}

    # -- review -----------------------------------------------------------------------------
    def review(self) -> dict:
        answer = (self.dir / "answer.md").read_text(encoding="utf-8")
        citations = json.loads((self.dir / "citations.json").read_text(encoding="utf-8"))
        body = answer.split("\n## Sources", 1)[0]
        sources = "\n".join(f"[{c['n']}] {c['source_url']}\n    Quote: \"{c['quote']}\"" for c in citations)
        prompt = P.REVIEW.format(question=self.question, answer=body, sources=sources)
        parsed, res = None, None
        for attempt in range(1, 3):
            res = self.call("reviewer", prompt, self.path("review", f"attempt-{attempt}"), budget=self.cfg["budgets_seconds"]["review"])
            parsed = fw.parse_json_reply(res.stdout)
            if parsed and parsed.get("verdict"):
                break
        writer_calls = [c for c in self.store.calls(self.run_id) if c["role"] == "writer"]
        served = self._served_models([c["session_id"] for c in writer_calls] + ([res.session_id] if res and res.session_id else []))
        writer_vendors = {vendor(served.get(c["session_id"]) or c["model"]) for c in writer_calls}
        reviewer_model = served.get(res.session_id) if res and res.session_id else None
        reviewer_vendor = vendor(reviewer_model or self.model("reviewer"))
        independent = bool(reviewer_vendor and reviewer_vendor not in writer_vendors)
        review = {"verdict": (parsed or {}).get("verdict") or "no_verdict", "review": parsed, "raw_reply": res.stdout if res else "",
                  "reviewer_model_requested": self.model("reviewer"), "reviewer_model_served": reviewer_model,
                  "writer_vendors": sorted(v for v in writer_vendors if v), "reviewer_vendor": reviewer_vendor,
                  "different_vendor": independent, "session_id": res.session_id if res else None}
        self.write_json("review.json", review)
        if not independent:
            raise PhaseError("reviewer vendor is not different from the writer vendor")
        return {"verdict": review["verdict"], "different_vendor": independent}

    def _served_models(self, session_ids: list[str]) -> dict[str, str]:
        out = {}
        for row in self.usage([s for s in session_ids if s]):
            if (row.get("task") or "") == "" and row.get("model"):
                out.setdefault(row["session_id"], row["model"])
        return out

    # -- record -----------------------------------------------------------------------------
    def record(self, status: str = "completed", error: str | None = None, end_at: float | None = None) -> dict:
        run = self.store.get_run(self.run_id)
        end = end_at or time.time()
        run_ids = list(dict.fromkeys([self.run_id, *self.source_run_ids()]))
        calls = [c for rid in run_ids for c in self.store.calls(rid)]
        usage = self.usage([c["session_id"] for c in calls])
        by_session: dict[str, list[dict]] = {}
        for row in usage:
            by_session.setdefault(row["session_id"], []).append(row)
        call_rows, total, missing = [], 0.0, 0
        by_role: dict[str, float] = {}
        by_model: dict[str, float] = {}
        for c in calls:
            rows = by_session.get(c["session_id"], [])
            cost = 0.0
            for r in rows:
                value = r.get("actual_cost_usd") or r.get("estimated_cost_usd")
                if value is None:
                    missing += 1
                    continue
                cost += value
                by_model[r.get("model") or "unknown"] = by_model.get(r.get("model") or "unknown", 0.0) + value
            if not rows:
                missing += 1
            total += cost
            by_role[c["role"]] = by_role.get(c["role"], 0.0) + cost
            call_rows.append({**c, "usage": rows, "cost_usd": round(cost, 6)})
        phases = {p["name"]: {"status": p["status"], "seconds": round((p["ended_at"] or time.time()) - p["started_at"], 1)}
                  for p in self.store.phases(self.run_id) if p and p["started_at"]}
        qc_path, review_path = self.dir / "quote_check.json", self.dir / "review.json"
        qc = json.loads(qc_path.read_text()) if qc_path.exists() else None
        review = json.loads(review_path.read_text()) if review_path.exists() else None
        counts = {}
        for rid in run_ids:
            for kind, statuses in self.store.job_counts(rid).items():
                for state, n in statuses.items():
                    group = counts.setdefault(kind, {})
                    group[state] = group.get(state, 0) + n
        jobs_total = sum(sum(v.values()) for v in counts.values())
        claims = [c for rid in run_ids for c in self.store.claims(rid)]
        pages_this_run = sum(self.store.conn().execute("SELECT COUNT(*) FROM pages WHERE run_id=?", (rid,)).fetchone()[0]
                             for rid in run_ids)
        reads = sum(counts.get("read", {}).values())
        record = {
            "run_id": self.run_id, "question": self.question, "status": status, "error": error,
            "root": str(self.root), "parent_run_id": run["parent_run_id"],
            "started_at": datetime.fromtimestamp(run["created_at"], timezone.utc).isoformat(),
            "finished_at": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "wall_seconds": round(end - run["created_at"], 1),
            "phases": phases,
            "jobs": {"total": jobs_total, "search_fetch": sum(self.store.count_jobs(rid, ("search", "fetch")) for rid in run_ids), "by_kind": counts},
            "limits": self.cfg["limits"],
            "pages": {"saved_this_run": pages_this_run, "reused_from_earlier_runs": reads, "in_database": self.store.page_count()},
            "claims": claim_counts(claims),
            "meaning_check": self._support_counts(),
            "quote_check": (qc or {}).get("verdict"),
            "review_verdict": (review or {}).get("verdict"),
            "review_different_vendor": (review or {}).get("different_vendor"),
            "readiness": self.readiness(), "review_path": str(self.path("review.json")),
            "models": {role: m["id"] for role, m in self.cfg["models"].items()},
            "models_served": sorted({r["model"] for r in usage if r.get("model")}),
            "cost_usd": round(total, 6), "cost_by_role": {k: round(v, 6) for k, v in by_role.items()},
            "cost_by_model": {k: round(v, 6) for k, v in by_model.items()},
            "cost_note": "Model cost from Hermes session records (session_model_usage, estimated where the provider gave no actual). "
                         "Metered web_search and web_extract tool charges are not in these records.",
            "cost_rows_missing": missing,
            "calls": call_rows,
            "answer_path": str(self.dir / "answer.md") if (self.dir / "answer.md").exists() else None,
        }
        grades = self._read_json("grades.json") or {}
        record.update(grade_counts=grades.get("counts", {}), citation_recall=grades.get("citation_recall"),
                      sentence_check_counts=grades.get("sentence_check_counts", {}), review_issue_counts=self._review_issues())
        self.write_json("run.json", record)
        return record


def cmd_run(args) -> int:
    question = args.question or Path(args.question_file).read_text(encoding="utf-8")
    fr = FastResearch(args.root, question, run_id=args.run_id, parent_run_id=args.parent_run_id,
                      config=load_config(Path(args.config)) if args.config else None, log=print)
    rec = fr.run()
    summary = {k: rec[k] for k in ("run_id", "status", "error", "wall_seconds", "jobs", "claims", "quote_check",
                                   "review_verdict", "cost_usd", "answer_path", "readiness", "review_path")}
    print(json.dumps(summary, indent=2))
    return 0 if rec["status"] == "completed" and rec["quote_check"] == "pass" else 2


def cmd_status(args) -> int:
    if args.run_id is not None:
        check_run_id(args.run_id)
    store = Store(Path(args.root).expanduser().resolve() / "fast.db")
    runs = [store.get_run(args.run_id)] if args.run_id else [dict(r) for r in store.conn().execute("SELECT * FROM runs ORDER BY created_at")]
    for run in runs:
        if not run:
            continue
        print(json.dumps({"run_id": run["id"], "status": run["status"], "question": run["question"],
                          "phases": {p["name"]: p["status"] for p in store.phases(run["id"])},
                          "jobs": store.job_counts(run["id"])}, indent=2))
    return 0


def refresh_record(fr: FastResearch) -> dict:
    """Rebuild the run record with fresh cost rows. Wall time still ends when the run ended."""
    run = fr.store.get_run(fr.run_id)
    prev = json.loads(run["record_json"] or "{}")
    end_at = run["created_at"] + prev["wall_seconds"] if prev.get("wall_seconds") is not None else None
    rec = fr.record(status=run["status"], error=prev.get("error"), end_at=end_at)
    fr.store.set_run(fr.run_id, record=rec)
    return rec


def cmd_record(args) -> int:
    check_run_id(args.run_id)
    store = Store(Path(args.root).expanduser().resolve() / "fast.db")
    run = store.get_run(args.run_id)
    if not run:
        print("unknown run", file=sys.stderr)
        return 2
    controller = FastResearch
    if store.phase(args.run_id, "base"):
        from deepest_research import DeepestResearch
        controller = DeepestResearch
    fr = controller(args.root, run["question"], run_id=args.run_id)
    rec = refresh_record(fr)
    print(json.dumps({k: rec[k] for k in ("run_id", "wall_seconds", "cost_usd", "cost_rows_missing")}, indent=2))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deep research fast mode")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run or resume one fast research question")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--question")
    g.add_argument("--question-file")
    r.add_argument("--root", required=True, help="folder for fast.db, saved pages and runs/")
    r.add_argument("--run-id")
    r.add_argument("--parent-run-id", help="link a follow-up run to an earlier one on the same root")
    r.add_argument("--config", help="other config file (default config/fast-mode.json)")
    r.set_defaults(fn=cmd_run)
    s = sub.add_parser("status")
    s.add_argument("--root", required=True)
    s.add_argument("--run-id")
    s.set_defaults(fn=cmd_status)
    rc = sub.add_parser("record", help="rebuild run.json, for example once late cost rows exist")
    rc.add_argument("--root", required=True)
    rc.add_argument("--run-id", required=True)
    rc.set_defaults(fn=cmd_record)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as exc:  # bad run id, a path outside the root, or a run id reused for another question
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
