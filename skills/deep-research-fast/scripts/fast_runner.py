#!/usr/bin/env python3
"""Parallel job runner for fast mode.

The runner pulls pending jobs from the store and runs them on a thread pool with a hard
concurrency ceiling. ``execute(job)`` does the work (in production a Hermes one-shot worker) and
returns a JSON-able result. The result is stored first (``landed``), then ``on_done(job, result)``
runs straight away in the same thread. That is where findings are checked as they arrive, so
checks for different jobs run side by side. ``on_done`` may add new jobs (a search adds fetches),
and the runner keeps going until nothing is pending or running.

Crash safety: ``landed`` jobs whose handler did not finish are handled again on start, and
``Store.recover`` puts interrupted ``running`` jobs back to pending without using an attempt.
Handlers must be safe to repeat.
"""
from __future__ import annotations

import time
import traceback
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Callable

from fast_store import CAPPED_KINDS, HARD_MAX_SEARCH_FETCH, Store

HARD_MAX_CONCURRENCY = 8


class Runner:
    def __init__(self, store: Store, run_id: str, execute: Callable[[dict], dict],
                 on_done: Callable[[dict, dict], None] | None = None, *, concurrency: int = 8,
                 deadline: float | None = None, clock: Callable[[], float] = time.monotonic, poll: float = 0.2):
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self.store = store
        self.run_id = run_id
        self.execute = execute
        self.on_done = on_done or (lambda job, result: None)
        self.concurrency = min(concurrency, HARD_MAX_CONCURRENCY)
        self.deadline = deadline
        self.clock = clock
        self.poll = poll
        self.peak_running = 0
        self.deadline_hit = False

    def _handle(self, job: dict, result: dict):
        try:
            self.on_done(job, result)
        except Exception as exc:  # noqa: BLE001
            self.store.fail_attempt(self.run_id, job["id"], f"handler: {exc!r}\n{traceback.format_exc()[-1200:]}")
            return
        self.store.finish_job(self.run_id, job["id"])

    def _run_one(self, job: dict):
        try:
            result = self.execute(job)
            if not isinstance(result, dict):
                raise TypeError("worker result must be a dict")
        except Exception as exc:  # noqa: BLE001
            self.store.fail_attempt(self.run_id, job["id"], f"worker: {exc!r}")
            return
        self.store.land_job(self.run_id, job["id"], result)
        self._handle(job, result)

    def run(self) -> dict:
        started = self.clock()
        # Results that were stored before a crash but never handled.
        for job in self.store.jobs(self.run_id, status="landed"):
            self._handle(job, job["result"] or {})
        futures = {}
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            while True:
                if self.deadline is not None and self.clock() >= self.deadline and not self.deadline_hit:
                    self.deadline_hit = True
                    self.store.skip_pending(self.run_id, "deadline")
                while len(futures) < self.concurrency and not self.deadline_hit:
                    job = self.store.claim_next(self.run_id)
                    if job is None:
                        break
                    if job["kind"] in CAPPED_KINDS and self.store.count_started(self.run_id) > HARD_MAX_SEARCH_FETCH:
                        # Independent of how the job was admitted: never start more than the hard ceiling.
                        self.store.skip_job(self.run_id, job["id"], "over_hard_limit")
                        continue
                    futures[pool.submit(self._run_one, job)] = job["id"]
                self.peak_running = max(self.peak_running, len(futures))
                if not futures:
                    if self.deadline_hit or not self.store.jobs(self.run_id, status="pending"):
                        break
                    continue
                done, _ = wait(list(futures), timeout=self.poll, return_when=FIRST_COMPLETED)
                for f in done:
                    futures.pop(f)
                    f.result()
        return {"elapsed_seconds": round(self.clock() - started, 3), "peak_running": self.peak_running,
                "deadline_hit": self.deadline_hit, "counts": self.store.job_counts(self.run_id)}
