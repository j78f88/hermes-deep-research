"""Regression tests for the review round 1 fixes.

The defect cases come from the independent reviewer's sandbox tests. Here they assert the fixed
behaviour. No network, no models, no state.db.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import fast_quote_check as qc  # noqa: E402
from fast_research import FastResearch  # noqa: E402
from fast_store import Store  # noqa: E402
from fast_test_support import FakeHermes, small_config  # noqa: E402


# -- fix 1: run ids stay inside the run root -------------------------------------------------
ABS = "<absolute path under tmp_path>"
BAD_RUN_IDS = ["../../escaped", "../escaped", ABS, "a/b", "a\\b", "", ".", "..",
               "-leading-dash", "x" * 200, "with space", "nul\x00byte"]


@pytest.mark.parametrize("run_id", BAD_RUN_IDS)
def test_bad_run_ids_are_rejected_before_anything_is_written(tmp_path, run_id):
    root = tmp_path / "intended-root"
    if run_id == ABS:
        run_id = str(tmp_path / "absolute-escape")
    fake = FakeHermes({})
    with pytest.raises(ValueError, match="run id"):
        FastResearch(root, "Path confinement check", run_id=run_id, config=small_config(), **fake.hooks())
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "absolute-escape").exists()
    assert fake.log == []
    # No run row was created for the bad id.
    assert Store(root / "fast.db").conn().execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_symlinked_run_folder_that_leaves_the_root_is_rejected(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "runs").mkdir(parents=True)
    outside.mkdir()
    (root / "runs" / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        FastResearch(root, "Q?", run_id="linked", config=small_config(), **FakeHermes({}).hooks())
    assert list(outside.iterdir()) == []


def test_symlinked_runs_folder_that_leaves_the_root_is_rejected(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "runs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        FastResearch(root, "Q?", run_id="ok-id", config=small_config(), **FakeHermes({}).hooks())
    assert list(outside.iterdir()) == []


def test_run_files_cannot_be_written_through_a_name_or_symlink_that_escapes(tmp_path):
    fr = FastResearch(tmp_path / "root", "Q?", run_id="r1", config=small_config(), **FakeHermes({}).hooks())
    with pytest.raises(ValueError):
        fr.write_json("../../proof.json", {"x": 1})
    outside = tmp_path / "outside.json"
    (fr.dir / "linked.json").symlink_to(outside)
    with pytest.raises(ValueError):
        fr.write_json("linked.json", {"x": 1})
    assert not outside.exists() and not (tmp_path / "proof.json").exists()
    assert fr.write_json("fine.json", {"x": 1}) == fr.dir / "fine.json"


@pytest.mark.parametrize("run_id", ["../../escaped", ABS, "a/b", ".."])
def test_quote_check_entry_point_rejects_bad_run_ids(tmp_path, run_id, capsys):
    root = tmp_path / "root"
    root.mkdir()
    if run_id == ABS:
        run_id = str(tmp_path / "absolute-escape")
    assert qc.main(["--root", str(root), "--run-id", run_id, "--write"]) == 2
    assert "run id" in capsys.readouterr().err
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "absolute-escape").exists()
    assert not (root / "fast.db").exists()  # rejected before the store was opened
    with pytest.raises(ValueError):
        qc.check_answer_files(root, run_id)


def test_quote_check_write_does_not_follow_a_symlink_out_of_the_run(tmp_path, capsys):
    root = tmp_path / "root"
    store = Store(root / "fast.db")
    store.create_run("r1", "Q?")
    page = store.save_page("https://example.com/p", "The page says the queue is bounded at eight.")
    cid = store.add_claim("r1", "j1", "The queue is bounded.", "the queue is bounded at eight", page["url"], page["sha256"])
    qc.check_claim(store, cid)
    run_dir = root / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "answer.md").write_text("# Q?\n\nThe queue is bounded.[1]\n\n## Sources\n\n1. P\n")
    claim = store.claim(cid)
    (run_dir / "citations.json").write_text(json.dumps([{"n": 1, "claim_id": cid, "quote": claim["quote"],
                                                         "page_url": claim["page_url"], "page_sha256": claim["page_sha256"]}]))
    outside = tmp_path / "outside.json"
    (run_dir / "quote_check.json").symlink_to(outside)
    assert qc.main(["--root", str(root), "--run-id", "r1", "--write"]) == 2
    assert not outside.exists()
    os.unlink(run_dir / "quote_check.json")
    assert qc.main(["--root", str(root), "--run-id", "r1", "--write"]) == 0
    assert json.loads((run_dir / "quote_check.json").read_text())["verdict"] == "pass"


# -- fix 2: a pick must be in the worker's own search results, even when there are none -------
TOPIC = {"id": "t1", "title": "Topic", "need": "Evidence"}
SEARCH_JOB = {"id": "search1", "topic": "t1", "payload": {"topic": TOPIC, "query": "q1"}}
INVENTED = "https://example.invalid/not-in-any-search-result"


def _fr(tmp_path, fake=None, run_id="search-check", **limits):
    return FastResearch(tmp_path, "Search provenance check", run_id=run_id, config=small_config(**limits),
                        **(fake or FakeHermes({})).hooks())


def test_empty_search_results_allow_no_pick(tmp_path):
    fr = _fr(tmp_path)
    fr._enqueue_pages(SEARCH_JOB, {"reply": {"picks": [{"url": INVENTED}]}, "search_results": []})
    assert fr.store.jobs(fr.run_id) == []
    assert "no_search_evidence" in (fr.dir / "events.log").read_text()


@pytest.mark.parametrize("results", [None, "not a list", [None, 3, {"title": "no url"}, {"url": None}]])
def test_missing_or_unparseable_search_evidence_allows_no_pick(tmp_path, results):
    fr = _fr(tmp_path)
    result = {"reply": {"picks": [{"url": INVENTED}]}}
    if results is not None:
        result["search_results"] = results
    fr._enqueue_pages(SEARCH_JOB, result)
    assert fr.store.jobs(fr.run_id) == []


def test_fabricated_pick_is_dropped_and_a_real_pick_kept(tmp_path):
    real = "https://example.com/real"
    fr = _fr(tmp_path)
    fr._enqueue_pages(SEARCH_JOB, {"reply": {"picks": [{"url": INVENTED}, {"url": real}]},
                                   "search_results": [{"url": real + "/"}]})
    jobs = fr.store.jobs(fr.run_id)
    assert [j["payload"]["url"] for j in jobs] == [real]
    assert "not_in_search_results" in (fr.dir / "events.log").read_text()


class _SearchHermes(FakeHermes):
    """Search worker that names a page, but its search evidence is missing."""

    def __init__(self, mode):
        super().__init__({"q1": [INVENTED]})
        self.mode = mode

    def oneshot(self, model, prompt, **kwargs):
        res = super().oneshot(model, prompt, **kwargs)
        if self.mode == "no_session":
            res.session_id = None
        return res

    def harvest_search(self, sid):
        if self.mode == "harvest_fails":
            raise RuntimeError("state.db is locked")
        if self.mode == "never_searched":
            return []
        return super().harvest_search(sid)


@pytest.mark.parametrize("mode", ["harvest_fails", "never_searched", "no_session"])
def test_search_job_without_search_evidence_queues_no_fetch(tmp_path, mode):
    fake = _SearchHermes(mode)
    fr = _fr(tmp_path, fake, run_id=f"search-{mode.replace('_', '-')}")
    fr.store.add_job(fr.run_id, "search1", "search", SEARCH_JOB["payload"], "t1")
    job = fr.store.claim_next(fr.run_id)
    result = fr.execute_job(job)
    assert result["search_evidence"] != "ok"
    fr.on_job_done(job, result)
    assert fr.store.jobs(fr.run_id, kind="fetch") == []
    assert fake.count("search") == 1 and fake.count("fetch") == 0
    assert "no_search_evidence" in (fr.dir / "events.log").read_text()


# -- fix 3: one fetch per page URL across every run on a root ---------------------------------
SHARED = "https://example.com/shared"


def _fetch_job(fr, url=SHARED):
    import hashlib
    jid = "page-" + hashlib.sha1(url.encode()).hexdigest()[:16]
    fr.store.add_job(fr.run_id, jid, "fetch", {"url": url, "topic": TOPIC}, "t1")
    return fr.store.claim_next(fr.run_id)


class _SlowFetchHermes(FakeHermes):
    """A fetch worker that only returns once the other run is seen waiting for the page."""

    def __init__(self, waiting):
        super().__init__({})
        self.waiting = waiting

    def oneshot(self, model, prompt, **kwargs):
        if "Page to read:" in prompt:
            self.waiting.wait(3)
        return super().oneshot(model, prompt, **kwargs)


def test_two_runs_on_one_root_fetch_an_unsaved_page_once(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    waiting = threading.Event()
    fake = _SlowFetchHermes(waiting)

    def log(line):
        if "waiting for fetch lease" in line:
            waiting.set()

    controllers = [FastResearch(tmp_path, f"Question {i}", run_id=f"run-{i}", config=small_config(), log=log,
                                **fake.hooks()) for i in range(2)]
    for fr in controllers:
        fr.lease_poll = 0.02
    jobs = [_fetch_job(fr) for fr in controllers]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in [pool.submit(fr.execute_job, job) for fr, job in zip(controllers, jobs)]]
    assert waiting.is_set(), "the second run should have waited on the first run's fetch"
    assert fake.count("fetch", SHARED) == 1 and fake.count("read", SHARED) == 1
    assert sorted(r["mode"] for r in results) == ["fetch", "read"]
    assert controllers[0].store.page_count() == 1
    assert controllers[0].store.fetch_lease(SHARED) is None  # released once the page was saved


def test_lease_store_basics(tmp_path):
    store = Store(tmp_path / "fast.db")
    kw = {"run_id": "r1", "job_id": "j1", "ttl": 60}
    assert store.acquire_fetch_lease(SHARED, "a", **kw) == ("acquired", None)
    state, holder = store.acquire_fetch_lease(SHARED + "/", "b", run_id="r2", job_id="j1", ttl=60)
    assert state == "held" and holder["owner"] == "a"  # same page after URL normalising
    assert store.acquire_fetch_lease(SHARED, "a2", **kw)[0] == "acquired"  # same run and job: a resume
    assert store.take_over_fetch_lease(SHARED, "a", "b", run_id="r2", job_id="j1", ttl=60) == "lost"
    assert store.take_over_fetch_lease(SHARED, "a2", "b", run_id="r2", job_id="j1", ttl=60) == "acquired"
    assert not store.release_fetch_lease(SHARED, "a2") and store.release_fetch_lease(SHARED, "b")
    store.save_page(SHARED, "saved text for the shared page")
    assert store.acquire_fetch_lease(SHARED, "c", run_id="r3", job_id="j1", ttl=60) == ("saved", None)
    assert store.fetch_lease(SHARED) is None


def _dead_pid():
    import subprocess
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_lease_left_by_a_crashed_controller_is_taken_over(tmp_path):
    import socket
    fake = FakeHermes({})
    fr = FastResearch(tmp_path, "Q?", run_id="after-crash", config=small_config(), **fake.hooks())
    fr.lease_poll = 0.02
    dead = _dead_pid()
    fr.store.acquire_fetch_lease(SHARED, f"x:{dead}:old", run_id="crashed", job_id="j9", ttl=3600,
                                 host=socket.gethostname(), pid=dead)
    result = fr.execute_job(_fetch_job(fr))
    assert result["mode"] == "fetch" and fake.count("fetch", SHARED) == 1
    assert "took over fetch lease" in (fr.dir / "events.log").read_text()
    assert fr.store.fetch_lease(SHARED) is None


def test_expired_lease_is_taken_over_even_if_its_holder_lives(tmp_path):
    import socket
    fake = FakeHermes({})
    fr = FastResearch(tmp_path, "Q?", run_id="after-expiry", config=small_config(), **fake.hooks())
    fr.store.acquire_fetch_lease(SHARED, "someone-else", run_id="slow", job_id="j9", ttl=-1,
                                 host=socket.gethostname(), pid=os.getpid())
    assert fr.execute_job(_fetch_job(fr))["mode"] == "fetch" and fake.count("fetch", SHARED) == 1


def test_lease_waits_while_the_crashed_runs_worker_is_still_alive(tmp_path):
    import socket
    import subprocess
    import time
    fake = FakeHermes({})
    fr = FastResearch(tmp_path, "Q?", run_id="orphan-wait", config=small_config(), **fake.hooks())
    fr.lease_poll = 0.05
    dead = _dead_pid()
    wd = tmp_path / "orphan-worker"
    wd.mkdir()
    orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)", str(wd)])
    fr.store.acquire_fetch_lease(SHARED, f"x:{dead}:old", run_id="crashed", job_id="j9", ttl=3600,
                                 host=socket.gethostname(), pid=dead)
    fr.store.set_lease_worker(SHARED, f"x:{dead}:old", orphan.pid, str(wd))
    started = time.time()
    try:
        result = fr.execute_job(_fetch_job(fr))
        elapsed = time.time() - started
        assert orphan.poll() is not None, "the fetch started while the orphan worker was still running"
    finally:
        orphan.wait()
    assert elapsed >= 0.4
    assert result["mode"] == "fetch" and fake.count("fetch", SHARED) == 1


def test_failed_fetch_releases_its_lease(tmp_path):
    class Broken(FakeHermes):
        def oneshot(self, model, prompt, **kwargs):
            if "Page to read:" in prompt:
                raise RuntimeError("worker could not start")
            return super().oneshot(model, prompt, **kwargs)

    fr = FastResearch(tmp_path, "Q?", run_id="broken", config=small_config(), **Broken({}).hooks())
    with pytest.raises(RuntimeError):
        fr.execute_job(_fetch_job(fr))
    assert fr.store.fetch_lease(SHARED) is None


def test_two_full_runs_at_once_on_one_root_fetch_each_page_once(tmp_path):
    import threading
    urls = [f"https://example.com/doc{i}" for i in range(1, 6)]
    search = {"q1": urls[:2], "q2": urls[1:3], "q3": [urls[3], urls[0]], "q4": [urls[4]]}

    class Slow(FakeHermes):
        def oneshot(self, model, prompt, **kwargs):
            if "Page to read:" in prompt:
                threading.Event().wait(0.05)  # keep fetches in flight long enough to overlap
            return super().oneshot(model, prompt, **kwargs)

    fake = Slow(search)
    controllers = [FastResearch(tmp_path, f"Question {i}?", run_id=f"full-{i}", config=small_config(), **fake.hooks())
                   for i in range(2)]
    for fr in controllers:
        fr.lease_poll = 0.01
    records = [None, None]

    def go(i):
        records[i] = controllers[i].run()

    threads = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert all(r and r["status"] == "completed" for r in records), [r and r["error"] for r in records]
    for url in urls:
        assert fake.count("fetch", url) == 1, url
    assert controllers[0].store.page_count() == 5
    assert controllers[0].store.conn().execute("SELECT COUNT(*) FROM fetch_leases").fetchone()[0] == 0


# -- fix 4: the hard ceiling of 40 search and fetch jobs holds on every path --------------------
def test_programmatic_config_cannot_lift_the_hard_job_ceiling(tmp_path):
    cfg = small_config(max_fetch_jobs=100, max_search_jobs=100, max_search_fetch_jobs=100, picks_per_search=41,
                       concurrency=50)
    fake = FakeHermes({})
    fr = FastResearch(tmp_path, "Hard cap check", run_id="cap", config=cfg, **fake.hooks())
    assert fr.cfg["limits"]["max_search_fetch_jobs"] == 40 and fr.cfg["limits"]["concurrency"] == 8
    assert cfg["limits"]["max_search_fetch_jobs"] == 100  # the caller's dictionary is not changed
    picks = [{"url": f"https://example.invalid/page-{i}"} for i in range(41)]
    fr._enqueue_pages(SEARCH_JOB, {"reply": {"picks": picks}, "search_results": picks})
    assert fr.store.count_jobs(fr.run_id, ("search", "fetch")) == 40
    assert "over_limit" in (fr.dir / "events.log").read_text()
    assert fake.log == []


def test_store_admission_enforces_the_hard_ceiling_whatever_the_limits_say(tmp_path):
    from fast_store import JobLimitError
    store = Store(tmp_path / "fast.db")
    store.create_run("r1", "Q?")
    outcomes = [store.add_job_within("r1", f"f{i}", "fetch", {}, None, {"fetch": 100, "total": 100}) for i in range(41)]
    assert outcomes.count("added") == 40 and outcomes[-1] == "over_limit"
    store.create_run("r2", "Q2?")
    for i in range(40):
        assert store.add_job("r2", f"s{i}", "search")
    with pytest.raises(JobLimitError):
        store.add_job("r2", "s40", "search")
    assert store.add_job("r2", "s0", "search") is False  # an existing job is still fine to re-add
    assert store.add_job("r2", "w1", "writer")  # the ceiling counts search and fetch jobs only


def test_runner_never_runs_more_than_the_hard_ceiling(tmp_path):
    import time
    from fast_runner import Runner
    store = Store(tmp_path / "fast.db")
    store.create_run("r1", "Q?")
    with store.tx() as c:  # a database written past the ceiling by some other path
        for i in range(45):
            c.execute("INSERT INTO jobs (run_id, id, kind, payload_json, created_at) VALUES ('r1', ?, 'fetch', '{}', ?)",
                      (f"f{i}", time.time()))
    ran = []
    Runner(store, "r1", lambda job: ran.append(job["id"]) or {}, concurrency=4, poll=0.01).run()
    assert len(ran) == 40
    counts = store.job_counts("r1")["fetch"]
    assert counts == {"done": 40, "skipped": 5}
    assert all(j["error"] == "over_hard_limit" for j in store.jobs("r1", status="skipped"))


# -- fix 5: a writer citing an unknown or unchecked finding id cannot deliver an answer --------
FULL_SEARCH = {"q1": ["https://example.com/doc1", "https://example.com/doc2"], "q2": ["https://example.com/doc3"],
               "q3": ["https://example.com/doc4"], "q4": []}


class _InventingWriter(FakeHermes):
    """The writer cites the findings it was given plus one id it was never given."""

    def __init__(self, search, cite, **kw):
        super().__init__(search, **kw)
        self.cite = cite

    def oneshot(self, model, prompt, **kwargs):
        res = super().oneshot(model, prompt, **kwargs)
        if "final answer" in prompt:
            ids = re.findall(r"^\[C(\d+)\] \(topic", prompt, flags=re.M)  # the findings list, not the prompt's example ids
            res.stdout = ("Deep research branches into sub-questions. "
                          + " ".join(f"Finding {i} holds.[C{i}]" for i in ids[:3])
                          + f" A made-up point.[C{self.cite()}]\n\n## What is still missing\n\nCost figures.")
        return res


def _assert_stopped_without_answer(fr, rec, fake, bad_id):
    assert rec["status"] == "stopped" and bad_id in rec["error"]
    assert not (fr.dir / "answer.md").exists() and rec["answer_path"] is None
    assert not (fr.dir / "citations.json").exists()
    draft = (fr.dir / "answer_draft.md").read_text()
    assert "A made-up point." in draft and f"[{bad_id}]" in draft  # kept for inspection, unchanged
    qcheck = json.loads((fr.dir / "quote_check.json").read_text())
    assert qcheck["verdict"] == "fail" and rec["quote_check"] == "fail"
    assert {"n": None, "kind": "unknown_finding_id", "id": bad_id} in qcheck["problems"]
    assert fake.count("review") == 0  # nothing was delivered, so nothing was reviewed
    assert fr.store.phase(fr.run_id, "answer")["status"] == "failed"


def test_invented_finding_id_stops_the_run_and_keeps_the_draft(tmp_path):
    fake = _InventingWriter(FULL_SEARCH, cite=lambda: 9999)
    fr = FastResearch(tmp_path, "How does it work?", run_id="invented", config=small_config(), **fake.hooks())
    _assert_stopped_without_answer(fr, fr.run(), fake, "C9999")


def test_citing_a_finding_that_failed_its_check_stops_the_run(tmp_path):
    holder = {}
    fake = _InventingWriter(FULL_SEARCH, cite=lambda: holder["fr"].store.claims(holder["fr"].run_id, status="fail")[0]["id"],
                            bad_quote_urls={"https://example.com/doc3"})
    fr = holder["fr"] = FastResearch(tmp_path, "How does it work?", run_id="unchecked", config=small_config(), **fake.hooks())
    rec = fr.run()
    failed = fr.store.claims(fr.run_id, status="fail")[0]["id"]
    _assert_stopped_without_answer(fr, rec, fake, f"C{failed}")


def test_writer_that_cites_only_checked_findings_still_delivers(tmp_path):
    fake = FakeHermes(FULL_SEARCH)
    fr = FastResearch(tmp_path, "How does it work?", run_id="honest", config=small_config(), **fake.hooks())
    rec = fr.run()
    assert rec["status"] == "completed" and rec["quote_check"] == "pass"
    assert "A made-up point." not in (fr.dir / "answer.md").read_text()
    assert json.loads((fr.dir / "quote_check.json").read_text())["dropped_markers"] == []
