"""Port of the round-3 reviewer regression for same-run saved-page recovery.

All worker output and saved session data are synthetic offline fixtures.
Popen is replaced before execution, so no Hermes/model/web call can occur.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

import pytest

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from fast_research import FastResearch
from fast_store import Store
from fast_test_support import small_config
import fast_worker as fw

URL = "https://example.invalid/r3-resume"
TEXT = "Offline fixture: a worker fetched this page before the controller crashed."
TOPIC = {"id": "t1", "title": "Crash recovery", "need": "Reuse a usable fetch"}


@pytest.mark.parametrize("same_run,stdout,active_worker,usable_page", [
    (True, "", False, True),
    (True, '{"claims": []}', False, True),
    (False, "", False, True),
    (True, "", True, True),
    (True, "", False, False),
    (True, "", True, False),
], ids=["resume-empty-stdout", "resume-complete-stdout", "other-run-empty-stdout",
        "resume-live-empty-stdout", "resume-no-page", "resume-live-no-page"])
def test_saved_session_is_harvested_before_any_new_fetch(
        tmp_path, monkeypatch, same_run, stdout, active_worker, usable_page):
    # A failing subprocess assertion must never include inherited credentials.
    for name in list(os.environ):
        if name not in {"PATH", "HOME", "TMPDIR", "PYTHONDONTWRITEBYTECODE"}:
            monkeypatch.delenv(name)
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    old_pid = child.pid
    store = Store(tmp_path / "fast.db")
    store.create_run("crashed", "Offline first question")
    store.add_job("crashed", "old-fetch", "fetch", {"url": URL, "topic": TOPIC}, "t1")
    store.claim_next("crashed")
    wd = tmp_path / "runs/crashed/jobs/old-fetch/attempt-1"
    wd.mkdir(parents=True)
    orphan = None
    if active_worker:
        orphan = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)", str(wd)])
        old_pid = orphan.pid
    (wd / "meta.json").write_text(json.dumps({
        "pid": old_pid, "model": "fixture/worker", "started_at": time.time() - 5,
        "exit_code": None, "ended_at": None,
    }))
    (wd / "stdout.txt").write_text(stdout)
    (wd / "stderr.txt").write_text("session_id: offline-old-fetch\n")
    store.acquire_fetch_lease(URL, "old-owner", run_id="crashed", job_id="old-fetch",
                              ttl=3600, host=socket.gethostname(), pid=old_pid)
    store.set_lease_worker(URL, "old-owner", old_pid, str(wd))

    # Use the real page harvester against a local fixture database, not state.db.
    session_db = tmp_path / "offline-sessions.sqlite"
    with sqlite3.connect(session_db) as c:
        c.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_name TEXT, content TEXT)")
        payload = json.dumps({"results": [{"url": URL, "content": TEXT}]})
        for sid in ("offline-old-fetch", "offline-new-worker"):
            if sid == "offline-old-fetch" and not usable_page:
                continue
            c.execute("INSERT INTO messages (session_id, role, tool_name, content) VALUES (?, 'tool', 'web_extract', ?)",
                      (sid, payload))
    harvested, fetch_commands = [], []

    def harvest(sid):
        if orphan is not None:
            assert orphan.poll() is not None, "Harvest must wait for the original worker to end"
        harvested.append(sid)
        return fw.harvest_pages(sid, session_db)

    class OfflineProc:
        pid = old_pid

        def __init__(self, cmd, stdout, stderr, **kwargs):
            prompt = Path(cmd[cmd.index("--query-file") + 1]).read_text()
            if "Page to read:" in prompt:
                if orphan is not None:
                    assert orphan.poll() is not None, "A new fetch must not overlap the old worker"
                assert harvested == ["offline-old-fetch"], "Harvest must precede a new fetch"
                fetch_commands.append(cmd)
            stdout.write('{"claims": []}')
            stderr.write("session_id: offline-new-worker\n")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(fw.subprocess, "Popen", OfflineProc)
    monkeypatch.setattr(fw, "hermes_bin", lambda: "/offline-fixture/hermes-never-executed")
    fr = FastResearch(tmp_path, "Offline first question" if same_run else "Offline second question",
                      run_id="crashed" if same_run else "waiting", config=small_config(),
                      harvest_pages=harvest, usage=lambda _: [], harvest_search=lambda _: [])
    fr.lease_poll = 0.01
    if same_run:
        fr.store.recover(fr.run_id)
    else:
        fr.store.add_job(fr.run_id, "new-fetch", "fetch", {"url": URL, "topic": TOPIC}, "t1")
    job = fr.store.claim_next(fr.run_id)
    assert job is not None
    assert bool(fw.harvest_pages("offline-old-fetch", session_db)) == usable_page
    try:
        result = fr.execute_job(job)
    finally:
        if orphan is not None:
            if orphan.poll() is None:
                orphan.terminate()
            orphan.wait(timeout=5)
    print(json.dumps({"same_run": same_run, "old_stdout_empty": not bool(stdout),
                      "new_fetches": len(fetch_commands), "harvested": harvested,
                      "mode": result["mode"]}))
    assert len(fetch_commands) == (0 if usable_page else 1), (
        "Refetch only after the old worker ends and its session leaves no usable page")
    assert harvested == (["offline-old-fetch"] if usable_page
                         else ["offline-old-fetch", "offline-new-worker"])
    assert result["mode"] == ("read" if usable_page else "fetch")
    page = fr.store.get_page(URL)
    assert page is not None and page["text"] == TEXT
