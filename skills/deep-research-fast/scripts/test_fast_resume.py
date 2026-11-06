import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import fast_worker as fw  # noqa: E402
from fast_research import FastResearch  # noqa: E402
from fast_runner import Runner  # noqa: E402
from fast_store import Store  # noqa: E402
from fast_test_support import FakeHermes, small_config  # noqa: E402

U1, U2, U3, U4, U5 = (f"https://example.com/doc{i}" for i in range(1, 6))
SEARCH = {"q1": [U1, U2], "q2": [U2, U3], "q3": [U4, U1], "q4": [U5]}
Q = "How does it work?"


def test_controller_resumes_after_a_crash_without_redoing_finished_work(tmp_path):
    fake = FakeHermes(SEARCH, crash_on_fetch=U4)
    first = FastResearch(tmp_path, Q, config=small_config(concurrency=1), **fake.hooks())
    with pytest.raises(KeyboardInterrupt):
        first.run()
    store = Store(tmp_path / "fast.db")
    run_id = first.run_id
    assert store.get_run(run_id)["status"] == "running"
    assert store.phase(run_id, "research")["status"] == "running"
    crashed = [j for j in store.jobs(run_id) if j["status"] == "running"]
    assert [j["payload"]["url"] for j in crashed] == [U4]
    before = {k: fake.count(k) for k in ("preflight", "plan", "search", "fetch")}
    done_fetches = {j["payload"]["url"] for j in store.jobs(run_id, kind="fetch", status="done")}

    # Same command again: same run, finished phases and jobs are not repeated.
    second = FastResearch(tmp_path, Q, config=small_config(concurrency=1), **fake.hooks())
    assert second.run_id == run_id
    rec = second.run()
    assert rec["status"] == "completed", rec["error"]
    assert fake.count("preflight") == before["preflight"]
    assert fake.count("plan") == before["plan"]
    for url in done_fetches:
        assert fake.count("fetch", url) == 1
    assert fake.count("fetch", U4) == 1  # the crashed call never finished, so one real fetch
    job = store.job(run_id, "page-" + __import__("hashlib").sha1(b"https://example.com/doc4").hexdigest()[:16])
    assert job["status"] == "done" and job["attempts"] == 1  # the crash did not use up an attempt
    assert rec["quote_check"] == "pass"


def test_landed_results_are_checked_on_restart_without_rerunning_the_worker(tmp_path):
    store = Store(tmp_path / "fast.db")
    store.create_run("r1", "Q?")
    store.add_job("r1", "j1", "fetch")
    store.claim_next("r1")
    store.land_job("r1", "j1", {"claims": 2})  # crash happened here, before the check ran
    handled = []

    def execute(job):
        raise AssertionError("worker must not run again")

    Runner(store, "r1", execute, lambda job, result: handled.append(result), concurrency=2, poll=0.01).run()
    assert handled == [{"claims": 2}]
    assert store.job("r1", "j1")["status"] == "done"


def _no_launch(*a, **k):
    raise AssertionError("must not launch a new worker")


def test_oneshot_reads_a_finished_worker_instead_of_relaunching(tmp_path, monkeypatch):
    wd = tmp_path / "jobs" / "j1" / "attempt-1"
    wd.mkdir(parents=True)
    (wd / "meta.json").write_text(json.dumps({"pid": 999999, "started_at": 1.0, "exit_code": 0, "ended_at": 2.0}))
    (wd / "stdout.txt").write_text('{"claims": []}')
    (wd / "stderr.txt").write_text("\nsession_id: local-abc\n")
    monkeypatch.setattr(fw.subprocess, "Popen", _no_launch)
    res = fw.oneshot("m", "prompt", workdir=wd, toolsets="file", run_budget=10, timeout=10)
    assert res.adopted and res.session_id == "local-abc" and res.stdout == '{"claims": []}'


def test_oneshot_waits_for_a_worker_that_outlived_the_controller(tmp_path, monkeypatch):
    wd = tmp_path / "jobs" / "j2" / "attempt-1"
    wd.mkdir(parents=True)
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.0)", str(wd)])
    (wd / "meta.json").write_text(json.dumps({"pid": proc.pid, "started_at": time.time(), "exit_code": None}))
    (wd / "stdout.txt").write_text('{"picks": []}')
    (wd / "stderr.txt").write_text("\nsession_id: local-live\n")
    monkeypatch.setattr(fw.subprocess, "Popen", _no_launch)
    started = time.time()
    try:
        res = fw.oneshot("m", "prompt", workdir=wd, toolsets="file", run_budget=10, timeout=20, poll=0.1)
    finally:
        proc.wait()
    assert time.time() - started >= 0.5
    assert res.adopted and res.session_id == "local-live"


def test_oneshot_relaunches_when_the_earlier_attempt_left_nothing(tmp_path, monkeypatch):
    wd = tmp_path / "jobs" / "j3" / "attempt-1"
    wd.mkdir(parents=True)
    (wd / "meta.json").write_text(json.dumps({"pid": 999999, "started_at": 1.0, "exit_code": None}))
    launched = []

    class FakeProc:
        pid = 4242

        def __init__(self, cmd, stdout, stderr, **kw):
            launched.append(cmd)
            stdout.write("OK")
            stderr.write("\nsession_id: local-new\n")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(fw.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(fw, "hermes_bin", lambda: "/usr/bin/hermes")
    res = fw.oneshot("anthropic/claude-haiku-5.5", "Reply with exactly: OK", workdir=wd, toolsets="web,terminal,file",
                     run_budget=60, timeout=90)
    assert not res.adopted and res.session_id == "local-new" and res.stdout == "OK"
    cmd = launched[0]
    assert cmd[:6] == ["/usr/bin/hermes", "chat", "--oneshot", "-Q", "--ignore-rules", "-t"]
    assert "web,terminal,file" in cmd and Path(cmd[cmd.index("--query-file") + 1]).is_absolute()
    assert list(wd.glob("meta.json.stale-*"))
