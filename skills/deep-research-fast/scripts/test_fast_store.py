import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

from fast_store import Store, norm_url, sha256_text  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "fast.db")
    s.create_run("r1", "What is X?")
    return s


def test_schema_and_wal(store, tmp_path):
    assert store.journal_mode() == "wal"
    names = {r[0] for r in sqlite3.connect(tmp_path / "fast.db").execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "phases", "jobs", "pages", "claims", "calls"} <= names


def test_create_run_is_idempotent_and_rejects_other_question(store):
    assert store.create_run("r1", "What is X?")["id"] == "r1"
    with pytest.raises(ValueError):
        store.create_run("r1", "Something else?")


def test_norm_url_dedups_common_variants():
    a = norm_url("https://www.Example.com/docs/?utm_source=x#part")
    assert a == norm_url("http://example.com/docs") == "https://example.com/docs"
    assert norm_url("https://example.com/") == "https://example.com/"


def test_page_saved_once_with_hash_and_file(store, tmp_path):
    p = store.save_page("https://example.com/a", "first text", title="A", run_id="r1", job_id="j1")
    assert p["sha256"] == sha256_text("first text")
    f = tmp_path / p["path"]
    assert f.read_text() == "first text"
    again = store.save_page("https://www.example.com/a/", "changed text")
    assert again["sha256"] == p["sha256"]
    assert store.page_count() == 1


def test_job_limits_enforced(store):
    limits = {"search": 2, "fetch": 3, "total": 4}
    assert store.add_job_within("r1", "s1", "search", {}, None, limits) == "added"
    assert store.add_job_within("r1", "s1", "search", {}, None, limits) == "exists"
    assert store.add_job_within("r1", "s2", "search", {}, None, limits) == "added"
    assert store.add_job_within("r1", "s3", "search", {}, None, limits) == "over_limit"
    assert store.add_job_within("r1", "f1", "fetch", {}, None, limits) == "added"
    assert store.add_job_within("r1", "f2", "fetch", {}, None, limits) == "added"
    assert store.add_job_within("r1", "f3", "fetch", {}, None, limits) == "over_limit"  # total cap of 4
    assert store.add_job_within("r1", "rd1", "read", {}, None, limits) == "added"  # reads are not searches or fetches
    assert store.count_jobs("r1", ("search", "fetch")) == 4


def test_claim_next_never_hands_out_a_job_twice(store):
    for i in range(40):
        store.add_job("r1", f"j{i}", "fetch")
    got, lock = [], threading.Lock()

    def take():
        while True:
            job = store.claim_next("r1")
            if job is None:
                return
            with lock:
                got.append(job["id"])

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(f"j{i}" for i in range(40))
    assert all(j["attempts"] == 1 and j["status"] == "running" for j in store.jobs("r1"))


def test_parallel_writers_do_not_lose_rows(store):
    errors = []

    def write(n):
        try:
            for i in range(25):
                # "read" jobs: 200 search or fetch jobs would pass the hard ceiling of 40 per run.
                store.add_job("r1", f"w{n}-{i}", "read")
                page = store.save_page(f"https://example.com/{n}/{i}", f"text {n} {i}")
                store.add_claim("r1", f"w{n}-{i}", f"claim {n} {i}", f"text {n} {i}", page["url"], page["sha256"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(store.jobs("r1")) == 200
    assert store.page_count() == 200
    assert len(store.claims("r1")) == 200


def test_attempts_retry_then_fail(store):
    store.add_job("r1", "j", "fetch", max_attempts=2)
    store.claim_next("r1")
    assert store.fail_attempt("r1", "j", "boom") == "pending"
    store.claim_next("r1")
    assert store.fail_attempt("r1", "j", "boom") == "failed"
    assert store.claim_next("r1") is None


def test_land_finish_and_recover(store):
    store.add_job("r1", "a", "fetch")
    store.add_job("r1", "b", "fetch")
    a = store.claim_next("r1")
    store.claim_next("r1")
    store.land_job("r1", a["id"], {"x": 1})
    assert store.job("r1", "a")["status"] == "landed"
    assert store.recover("r1") == 1  # only the running one
    b = store.job("r1", "b")
    assert b["status"] == "pending" and b["attempts"] == 0
    store.finish_job("r1", "a")
    assert store.job("r1", "a")["status"] == "done"
    assert store.job("r1", "a")["result"] == {"x": 1}


def test_claim_dedup_and_phases(store):
    c1 = store.add_claim("r1", "j", "t", "q q q q q", None, None)
    c2 = store.add_claim("r1", "j", "t", "q q q q q", None, None)
    assert c1 == c2
    store.start_phase("r1", "plan")
    store.end_phase("r1", "plan", {"topics": 3})
    assert store.phase("r1", "plan")["status"] == "done"
    assert store.phase("r1", "plan")["data"] == {"topics": 3}
