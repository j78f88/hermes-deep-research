import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from fast_runner import Runner  # noqa: E402
from fast_store import Store  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "fast.db")
    s.create_run("r1", "Q?")
    return s


class Gauge:
    def __init__(self):
        self.now = self.peak = 0
        self.lock = threading.Lock()

    def __enter__(self):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)

    def __exit__(self, *a):
        with self.lock:
            self.now -= 1


def test_runs_all_jobs_in_parallel_within_the_limit(store):
    for i in range(20):
        store.add_job("r1", f"j{i}", "fetch")
    gauge = Gauge()

    def execute(job):
        with gauge:
            time.sleep(0.05)
        return {"id": job["id"]}

    out = Runner(store, "r1", execute, concurrency=4, poll=0.01).run()
    assert out["counts"] == {"fetch": {"done": 20}}
    assert 2 <= gauge.peak <= 4


def test_concurrency_is_capped_at_eight(store):
    for i in range(30):
        store.add_job("r1", f"j{i}", "fetch")
    gauge = Gauge()

    def execute(job):
        with gauge:
            time.sleep(0.05)
        return {}

    runner = Runner(store, "r1", execute, concurrency=50, poll=0.01)
    assert runner.concurrency == 8
    runner.run()
    assert gauge.peak <= 8


def test_handler_can_add_follow_up_jobs(store):
    store.add_job("r1", "search-1", "search")
    store.add_job("r1", "search-2", "search")
    ran = []

    def execute(job):
        ran.append(job["id"])
        return {"urls": [f"{job['id']}-a", f"{job['id']}-b"]} if job["kind"] == "search" else {}

    def on_done(job, result):
        for u in result.get("urls", []):
            store.add_job("r1", f"fetch-{u}", "fetch")

    Runner(store, "r1", execute, on_done, concurrency=3, poll=0.01).run()
    assert sorted(ran) == sorted(["search-1", "search-2", "fetch-search-1-a", "fetch-search-1-b",
                                  "fetch-search-2-a", "fetch-search-2-b"])
    assert store.job_counts("r1") == {"search": {"done": 2}, "fetch": {"done": 4}}


def test_failed_worker_is_retried_once_then_marked_failed(store):
    store.add_job("r1", "flaky", "fetch", max_attempts=2)
    store.add_job("r1", "broken", "fetch", max_attempts=2)
    calls = {"flaky": 0, "broken": 0}

    def execute(job):
        calls[job["id"]] += 1
        if job["id"] == "broken" or calls["flaky"] == 1:
            raise RuntimeError("worker died")
        return {}

    Runner(store, "r1", execute, concurrency=2, poll=0.01).run()
    assert calls == {"flaky": 2, "broken": 2}
    assert store.job("r1", "flaky")["status"] == "done"
    assert store.job("r1", "broken")["status"] == "failed"
    assert "worker died" in store.job("r1", "broken")["error"]


def test_handler_error_counts_as_a_failed_attempt(store):
    store.add_job("r1", "j", "fetch", max_attempts=1)

    def on_done(job, result):
        raise ValueError("bad reply")

    Runner(store, "r1", lambda job: {}, on_done, concurrency=1, poll=0.01).run()
    job = store.job("r1", "j")
    assert job["status"] == "failed" and "bad reply" in job["error"]


def test_checks_in_handlers_run_side_by_side(store):
    for i in range(4):
        store.add_job("r1", f"j{i}", "fetch")
    barrier = threading.Barrier(4, timeout=5)

    def on_done(job, result):
        barrier.wait()  # only returns if four handlers are running at the same time

    Runner(store, "r1", lambda job: {}, on_done, concurrency=4, poll=0.01).run()
    assert store.job_counts("r1") == {"fetch": {"done": 4}}


def test_deadline_skips_jobs_not_started(store):
    for i in range(10):
        store.add_job("r1", f"j{i}", "fetch")
    clock = {"t": 0.0}

    def execute(job):
        clock["t"] += 10
        return {}

    out = Runner(store, "r1", execute, concurrency=1, deadline=25, clock=lambda: clock["t"], poll=0.01).run()
    counts = store.job_counts("r1")["fetch"]
    assert out["deadline_hit"]
    assert counts["done"] == 3 and counts["skipped"] == 7
