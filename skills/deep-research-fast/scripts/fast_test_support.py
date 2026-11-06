"""Stub Hermes for fast mode tests: no network, no models, no state.db."""
from __future__ import annotations

import itertools
import json
import re
import threading
import time
from pathlib import Path

import fast_prompts as P
import fast_worker as fw
from fast_research import load_config


def small_config(**limits) -> dict:
    cfg = load_config()
    cfg["limits"].update({"concurrency": 4, "min_topics": 2, "max_topics": 2, "queries_per_topic": 2,
                          "max_search_jobs": 4, "max_fetch_jobs": 6, "max_search_fetch_jobs": 10,
                          "picks_per_search": 2, "claims_per_page": 3, "research_deadline_seconds": 60})
    cfg["limits"].update(limits)
    cfg["grades"] = {"show_in_answer": True}  # most tests check how grades read; test_grades_hidden covers the default
    return cfg


def page_text(url: str) -> str:
    return (f"# Page for {url}\n\nThe deep research mode explores sub-questions in a tree with breadth and depth settings.\n\n"
            f"This page lives at {url} and says the tool runs on a Linux server with Python 3.11.\n")


class FakeHermes:
    """Answers each prompt the way a well-behaved worker would."""

    def __init__(self, search: dict[str, list[str]], bad_quote_urls=(), reviewer_crash=False, crash_on_fetch=None,
                 invent_finding=False, verdicts=None, checker_garbage=False):
        self.search = search
        # claim text -> ("supported" | "partly" | "unsupported", narrowed text or None); default supported
        self.verdicts = dict(verdicts or {})
        self.checker_garbage = checker_garbage  # the checker replies with no JSON
        self.followups = []
        self.last_prompt: dict[str, str] = {}
        self.invent_finding = invent_finding  # the writer also cites [C9999], an id it was never given
        self.bad_quote_urls = set(bad_quote_urls)
        self.crash_on_fetch = crash_on_fetch
        self.sessions: dict[str, dict] = {}
        self.log: list[tuple[str, str]] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def count(self, kind: str, key: str | None = None) -> int:
        return sum(1 for k, v in self.log if k == kind and (key is None or v == key))

    def oneshot(self, model, prompt, *, workdir, toolsets, run_budget, timeout, reasoning=None, on_start=None, poll=1.0):
        with self._lock:
            sid = f"sess-{next(self._ids)}"
        Path(workdir).mkdir(parents=True, exist_ok=True)
        session = {"model": model, "pages": [], "search": []}
        if prompt == P.PREFLIGHT:
            kind, key, out = "preflight", model, "OK"
        elif prompt.startswith("You plan"):
            kind, key = "plan", ""
            out = json.dumps({"topics": [
                {"id": "a", "title": "How deep research branches", "need": "tree", "queries": ["q1", "q2"]},
                {"id": "b", "title": "Running it on Linux", "need": "setup", "queries": ["q3", "q4"]}]})
        elif "search worker" in prompt:
            query = re.search(r"Search query: (.*)", prompt).group(1).strip()
            kind, key = "search", query
            urls = self.search.get(query, [])
            session["search"] = [{"url": u, "title": f"T {u}"} for u in urls]
            out = "Here you go:\n```json\n" + json.dumps({"picks": [{"url": u, "title": f"T {u}", "why": "x"} for u in urls]}) + "\n```"
        elif "Page to read:" in prompt or "Saved copy:" in prompt:
            url = re.search(r"Page(?: to read)?: (\S+)", prompt).group(1)
            kind, key = ("fetch" if "Page to read:" in prompt else "read"), url
            if kind == "fetch" and self.crash_on_fetch == url:
                self.crash_on_fetch = None
                self.log.append(("crash", url))
                raise KeyboardInterrupt("controller killed")
            if kind == "fetch":
                session["pages"] = [{"url": url, "title": f"T {url}", "text": page_text(url), "origin": "web_extract"}]
            quote = ("this quote is not anywhere on the page at all" if url in self.bad_quote_urls
                     else "explores sub-questions in a tree with breadth and depth settings")
            out = json.dumps({"url": url, "title": f"T {url}", "claims": [
                {"claim": "Deep research explores a tree of sub-questions.", "quote": quote},
                {"claim": "It runs on Linux with Python 3.11.", "quote": "runs on a Linux server with Python 3.11"}]})
        elif prompt.startswith(("You check findings", "You check sentences")):
            kind, key = ("sentence_check" if prompt.startswith("You check sentences") else "check"), ""
            checks = []
            for n, claim in re.findall(r"^(\d+)\. Claim: (.*)$", prompt, flags=re.M):
                verdict, narrowed = self.verdicts.get(claim.strip(), ("supported", None))
                row = {"n": int(n), "verdict": verdict, "reason": "fixture", "source_kind": "self"}
                if narrowed:
                    row["supported_claim"] = narrowed
                checks.append(row)
            out = "I could not decide." if self.checker_garbage else json.dumps({"checks": checks})
        elif prompt.startswith("You suggest follow-up"):
            kind, key, out = "followups", "", json.dumps({"questions": self.followups})
        elif "final answer" in prompt or prompt.startswith("You write one combined research answer"):
            kind, key = ("synthesis" if prompt.startswith("You write one combined") else "writer"), ""
            ids = re.findall(r"^\[C(\d+)\] \(topic", prompt, flags=re.M)  # the findings list, not the prompt's example ids
            out = ("Deep research branches into sub-questions. " +
                   " ".join(f"Finding {i} holds.[C{i}]" for i in ids[:4]) +
                   (" A made-up point.[C9999]" if self.invent_finding else "") + "\n\nMy inference: it needs an API key.\n\n## What is still missing\n\nCost figures.")
        elif "independent reviewer" in prompt:
            kind, key = "review", ""
            out = json.dumps({"verdict": "accept", "summary": "Fine.", "unsupported_citations": [],
                              "uncited_claims": [], "missing_parts": [], "other_issues": []})
        else:
            raise AssertionError("unexpected prompt: " + prompt[:80])
        with self._lock:
            self.sessions[sid] = session
            self.log.append((kind, key))
            self.last_prompt[kind] = prompt
        now = time.time()
        return fw.OneshotResult(out, f"\nsession_id: {sid}\n", sid, 0, None, now, now, str(workdir))

    def harvest_pages(self, sid):
        return self.sessions[sid]["pages"]

    def harvest_search(self, sid):
        return self.sessions[sid]["search"]

    def usage(self, sids):
        return [{"session_id": s, "model": self.sessions[s]["model"], "task": "", "estimated_cost_usd": 0.01,
                 "actual_cost_usd": 0.0} for s in sids if s in self.sessions]

    def hooks(self) -> dict:
        return {"oneshot": self.oneshot, "harvest_pages": self.harvest_pages, "harvest_search": self.harvest_search,
                "usage": self.usage}
