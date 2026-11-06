"""Regression tests for the review round 2 fix: fetch lease crash and expiry recovery.

The first two cases are ported from the independent reviewer's offline lease tests and assert
the fixed behaviour. Worker outputs are local fixtures. No network, no models, no state.db.
"""
import hashlib
import json
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fast_research import FastResearch  # noqa: E402
from fast_store import Store  # noqa: E402
from fast_test_support import FakeHermes, small_config  # noqa: E402
from fast_worker import OneshotResult  # noqa: E402

URL = "https://example.invalid/orphaned-fetch"
TEXT = "Offline fixture: this page was returned to the first run's worker."
TOPIC = {"id": "t1", "title": "Offline lease check", "need": "Reuse the first fetch"}


def dead_pid():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def setup(tmp_path, active_worker=False, sleep=1.0, exit_code=0, session=True, harvest=None):
    """A crashed run's lease on URL, its worker folder, and a second run waiting to fetch URL.

    ``active_worker`` keeps a local sleeper alive with the worker folder on its command line, as
    a real orphaned ``hermes chat --oneshot`` worker would have, and the lease is already expired.
    """
    store = Store(tmp_path / "fast.db")
    store.create_run("crashed", "First question")
    old_workdir = tmp_path / "runs/crashed/jobs/old-fetch/attempt-1"
    old_workdir.mkdir(parents=True)
    controller_pid = dead_pid()
    orphan = None
    worker_pid = controller_pid
    if active_worker:
        orphan = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({sleep})", str(old_workdir)])
        worker_pid = orphan.pid
    (old_workdir / "meta.json").write_text(json.dumps({
        "pid": worker_pid, "started_at": time.time() - 5, "model": "fixture/worker",
        "exit_code": None if active_worker else exit_code,
        "ended_at": None if active_worker else time.time(),
    }))
    (old_workdir / "stdout.txt").write_text('{"claims": []}')
    (old_workdir / "stderr.txt").write_text("session_id: fixture-first-fetch\n" if session else "")
    store.acquire_fetch_lease(URL, "old-owner", run_id="crashed", job_id="old-fetch",
                              ttl=-1 if active_worker else 3600,
                              host=socket.gethostname(), pid=controller_pid)
    store.set_lease_worker(URL, "old-owner", worker_pid, str(old_workdir))
    new_fetches, overlaps, harvested, prompts = [], [], [], []

    def oneshot(model, prompt, *, workdir, **kwargs):
        # Calling the first worker's folder is adoption; a new folder plus FETCH
        # is a second network fetch in production. No network is used here.
        adopted = Path(workdir) == old_workdir
        prompts.append(prompt)
        if "Page to read:" in prompt and not adopted:
            new_fetches.append(str(workdir))
            if orphan is not None and orphan.poll() is None:
                overlaps.append(True)
        sid = "fixture-first-fetch" if adopted else "fixture-second-fetch"
        return OneshotResult('{"claims": []}', "", sid, 0, None, 1.0, 2.0, str(workdir), adopted=adopted)

    def harvest_pages(sid):
        harvested.append(sid)
        if harvest is not None:
            return harvest(sid)
        return [{"url": URL, "text": TEXT}]

    fr = FastResearch(tmp_path, "Second question", run_id="waiting-run", config=small_config(),
                      oneshot=oneshot, harvest_pages=harvest_pages,
                      harvest_search=lambda _: [], usage=lambda _: [])
    fr.lease_poll = 0.01
    jid = "page-" + hashlib.sha1(URL.encode()).hexdigest()[:16]
    fr.store.add_job(fr.run_id, jid, "fetch", {"url": URL, "topic": TOPIC}, "t1")
    job = fr.store.claim_next(fr.run_id)
    return {"fr": fr, "job": job, "orphan": orphan, "fetches": new_fetches, "overlaps": overlaps,
            "harvested": harvested, "prompts": prompts, "old_workdir": old_workdir}


def _stop(orphan):
    if orphan is not None:
        if orphan.poll() is None:
            orphan.terminate()
        orphan.wait(timeout=5)


# -- ported from the reviewer's test_review_r2_leases.py ---------------------------------------
def test_finished_orphan_fetch_is_reused_after_controller_crash(tmp_path):
    s = setup(tmp_path)
    fr = s["fr"]
    result = fr.execute_job(s["job"])
    assert fr.store.get_page(URL)["text"] == TEXT
    assert s["fetches"] == [], "A completed orphan has saved output, but the waiting run fetched the URL again"
    assert s["harvested"] == ["fixture-first-fetch"]
    assert result["mode"] == "read"
    page = fr.store.get_page(URL)
    assert (page["run_id"], page["job_id"]) == ("crashed", "old-fetch")  # credited to the run that fetched it
    assert fr.store.fetch_lease(URL) is None
    assert "adopted the finished fetch" in (fr.dir / "events.log").read_text()


def test_expired_lease_does_not_allow_fetch_beside_active_orphan(tmp_path):
    s = setup(tmp_path, active_worker=True)
    try:
        result = s["fr"].execute_job(s["job"])
        assert s["overlaps"] == [], "Expiry authorized a second fetch while the first worker was still alive"
        assert s["orphan"].poll() is not None
    finally:
        _stop(s["orphan"])
    # Once the orphan ended, its session was harvested instead of fetching again.
    assert s["fetches"] == [] and s["harvested"] == ["fixture-first-fetch"]
    assert result["mode"] == "read" and s["fr"].store.get_page(URL)["text"] == TEXT


# -- further cases for the same fix ----------------------------------------------------------
def test_orphan_that_left_no_usable_page_is_followed_by_one_fetch(tmp_path):
    """The prior worker is gone and its session gave no page: only then is the page fetched again."""
    s = setup(tmp_path, harvest=lambda sid: [] if sid == "fixture-first-fetch" else [{"url": URL, "text": TEXT}])
    result = s["fr"].execute_job(s["job"])
    assert s["harvested"][0] == "fixture-first-fetch"
    assert len(s["fetches"]) == 1 and result["mode"] == "fetch"
    assert s["fr"].store.get_page(URL)["run_id"] == "waiting-run"
    assert s["fr"].store.fetch_lease(URL) is None


def test_orphan_without_a_session_is_followed_by_one_fetch(tmp_path):
    s = setup(tmp_path, exit_code=1, session=False)
    result = s["fr"].execute_job(s["job"])
    assert "fixture-first-fetch" not in s["harvested"]
    assert len(s["fetches"]) == 1 and result["mode"] == "fetch"


def test_expired_lease_with_active_orphan_and_no_output_waits_then_fetches_once(tmp_path):
    s = setup(tmp_path, active_worker=True, sleep=0.5, session=False)
    started = time.time()
    try:
        result = s["fr"].execute_job(s["job"])
    finally:
        _stop(s["orphan"])
    assert time.time() - started >= 0.4
    assert s["overlaps"] == [] and len(s["fetches"]) == 1 and result["mode"] == "fetch"


def test_finished_orphan_whose_controller_never_wrote_its_exit_is_reused(tmp_path):
    """A controller killed mid-fetch never updates meta.json. The worker's session line is enough."""
    s = setup(tmp_path, active_worker=True, sleep=0.0)
    s["orphan"].wait(timeout=5)
    assert json.loads((s["old_workdir"] / "meta.json").read_text())["exit_code"] is None
    result = s["fr"].execute_job(s["job"])
    assert s["fetches"] == [] and result["mode"] == "read"


def test_acquire_does_not_take_an_expired_lease_by_itself(tmp_path):
    store = Store(tmp_path / "fast.db")
    store.acquire_fetch_lease(URL, "old", run_id="r1", job_id="j1", ttl=-1)
    state, holder = store.acquire_fetch_lease(URL, "new", run_id="r2", job_id="j2", ttl=60)
    assert state == "held" and holder["owner"] == "old"


def test_take_over_keeps_the_previous_worker_and_checks_it_did_not_change(tmp_path):
    store = Store(tmp_path / "fast.db")
    store.acquire_fetch_lease(URL, "old", run_id="r1", job_id="j1", ttl=60)
    store.set_lease_worker(URL, "old", 4242, "/w/a")
    kw = {"run_id": "r2", "job_id": "j2", "ttl": 60}
    # The old holder started another worker after the caller looked: the take-over must fail.
    assert store.take_over_fetch_lease(URL, "old", "new", expect_worker=(4242, "/w/other"), **kw) == "lost"
    assert store.take_over_fetch_lease(URL, "old", "new", expect_worker=(4242, "/w/a"), **kw) == "acquired"
    lease = store.fetch_lease(URL)
    assert (lease["owner"], lease["worker_pid"], lease["worker_dir"]) == ("new", 4242, "/w/a")
    # A resume by the same run and job keeps the recorded worker too.
    assert store.acquire_fetch_lease(URL, "new2", **kw)[0] == "acquired"
    assert store.fetch_lease(URL)["worker_dir"] == "/w/a"


def test_worker_folder_is_on_the_lease_before_the_worker_starts(tmp_path):
    seen = []

    class Watching(FakeHermes):
        def oneshot(self, model, prompt, **kwargs):
            if "Page to read:" in prompt:
                seen.append((fr.store.fetch_lease(URL) or {}).get("worker_dir"))
            return super().oneshot(model, prompt, **kwargs)

    fr = FastResearch(tmp_path, "Q?", run_id="early", config=small_config(), **Watching({}).hooks())
    jid = "page-" + hashlib.sha1(URL.encode()).hexdigest()[:16]
    fr.store.add_job(fr.run_id, jid, "fetch", {"url": URL, "topic": TOPIC}, "t1")
    job = fr.store.claim_next(fr.run_id)
    fr.execute_job(job)
    assert seen == [str(fr._job_dir(job).resolve())]


def test_live_holder_whose_worker_finished_is_not_taken_over_before_expiry(tmp_path):
    """A live controller between its worker's exit and save_page keeps its lease."""
    fake = FakeHermes({})
    fr = FastResearch(tmp_path, "Q?", run_id="patient", config=small_config(), **fake.hooks())
    fr.lease_poll = 0.01
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        fr.store.acquire_fetch_lease(URL, "live", run_id="busy", job_id="j1", ttl=3600,
                                     host=socket.gethostname(), pid=sleeper.pid)
        fr.store.set_lease_worker(URL, "live", dead_pid(), str(tmp_path / "runs/busy/jobs/j1/attempt-1"))
        jid = "page-" + hashlib.sha1(URL.encode()).hexdigest()[:16]
        fr.store.add_job(fr.run_id, jid, "fetch", {"url": URL, "topic": TOPIC}, "t1")
        job = fr.store.claim_next(fr.run_id)

        def holder_saves():
            time.sleep(0.3)
            fr.store.save_page(URL, TEXT, run_id="busy", job_id="j1")
            fr.store.release_fetch_lease(URL, "live")

        t = threading.Thread(target=holder_saves)
        t.start()
        result = fr.execute_job(job)
        t.join()
    finally:
        sleeper.terminate()
        sleeper.wait()
    assert result["mode"] == "read" and fake.count("fetch") == 0
