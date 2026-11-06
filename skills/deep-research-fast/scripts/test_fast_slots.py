"""Host slots exercised with local Python stubs, never Hermes."""
import fcntl
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

import fast_worker as fw


def test_six_workers_share_three_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(fw, "HOST_SLOTS", 3)
    monkeypatch.setattr(fw, "build_command", lambda *a, **kw: [sys.executable, "-c", "import time; time.sleep(0.12)"])
    original = subprocess.Popen
    gauge = {"live": 0, "peak": 0}
    lock = threading.Lock()
    def popen(*a, **kw):
        p = original(*a, **kw)
        original_wait = p.wait
        counted = False
        with lock:
            gauge["live"] += 1
            gauge["peak"] = max(gauge["peak"], gauge["live"])
        def wait(*args, **kwargs):
            nonlocal counted
            result = original_wait(*args, **kwargs)
            with lock:
                if not counted:
                    gauge["live"] -= 1
                    counted = True
            return result
        p.wait = wait
        return p
    monkeypatch.setattr(subprocess, "Popen", popen)
    def run(i):
        return fw.oneshot("fixture/stub", "test", workdir=tmp_path / str(i), toolsets="file", run_budget=2, timeout=3, poll=0.01)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(run, range(6)))
    assert gauge["peak"] == 3
    assert all(r.exit_code == 0 for r in results)
    assert any(r.extra["slot_wait_seconds"] > 0 for r in results)
    assert any("waited" in (tmp_path / str(i) / "slots.log").read_text() for i in range(6)
               if (tmp_path / str(i) / "slots.log").exists())


def test_killed_slot_holder_releases_kernel_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "SLOT_DIR", tmp_path)
    monkeypatch.setattr(fw, "HOST_SLOTS", 1)
    code = ("import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); "
            "print('ready',flush=True); time.sleep(30)")
    p = subprocess.Popen([sys.executable, "-c", code, str(tmp_path / "slot-0.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "ready"
        with pytest.raises(TimeoutError, match="worker slot"):
            with fw.worker_slot(time.monotonic() + 0.05):
                pytest.fail("slot must still be held")
        p.kill()
        p.wait(timeout=3)
        with fw.worker_slot(time.monotonic() + 1):
            pass
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_slot_wait_consumes_timeout_and_finished_adoption_skips_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "SLOT_DIR", tmp_path / "slots")
    monkeypatch.setattr(fw, "HOST_SLOTS", 1)
    fw.SLOT_DIR.mkdir()
    workdir = tmp_path / "worker"
    with open(fw.SLOT_DIR / "slot-0.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: pytest.fail("no slot, no launch"))
        start = time.monotonic()
        with pytest.raises(TimeoutError):
            fw.oneshot("fixture/stub", "test", workdir=workdir, toolsets="file", run_budget=1, timeout=0.08)
        assert time.monotonic() - start < 0.5
        assert "waited" in (workdir / "slots.log").read_text()
        (workdir / "meta.json").write_text(json.dumps({"pid": None, "exit_code": 0}))
        (workdir / "stdout.txt").write_text("Finished")
        (workdir / "stderr.txt").write_text("session_id: fixture")
        assert fw.oneshot("fixture/stub", "test", workdir=workdir, toolsets="file", run_budget=1, timeout=0.08).adopted
