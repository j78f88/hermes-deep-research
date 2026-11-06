"""Meaning check: a finding reaches the writer only when its quote supports its claim.

All model calls are stubbed by FakeHermes. No network, models or state.db.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fast_research import FastResearch, narrowed_ok  # noqa: E402
from fast_test_support import FakeHermes, small_config  # noqa: E402
import fast_worker as fw  # noqa: E402

U1, U2 = "https://example.com/m1", "https://example.com/m2"
SEARCH = {"q1": [U1], "q2": [U2], "q3": [], "q4": []}
TREE = "Deep research explores a tree of sub-questions."
LINUX = "It runs on Linux with Python 3.11."


def run(tmp_path, fake, **kw):
    fr = FastResearch(tmp_path, "How does it work?", config=kw.pop("config", None) or small_config(), **fake.hooks(), **kw)
    return fr, fr.run()


def test_unsupported_findings_never_reach_the_writer(tmp_path):
    fake = FakeHermes(SEARCH, verdicts={LINUX: ("unsupported", None)})
    fr, rec = run(tmp_path, fake)
    assert rec["status"] == "completed", rec["error"]
    assert rec["claims"]["pass"] == 2 and rec["claims"]["unsupported"] == 2
    assert rec["meaning_check"] == {"supported": 2, "unsupported": 2}
    citations = json.loads((fr.dir / "citations.json").read_text())
    assert citations and all(c["claim"] == TREE for c in citations)
    writer_prompt = fake.last_prompt["writer"]
    assert TREE in writer_prompt and LINUX not in writer_prompt


def test_checker_sees_each_finding_page_title_and_address(tmp_path):
    fake = FakeHermes(SEARCH)
    fr, rec = run(tmp_path, fake)
    prompt = fake.last_prompt["check"]
    assert "Page: T https://example.com/m" in prompt and "Who says it matters" in prompt


def test_partly_supported_finding_is_narrowed_before_the_writer_sees_it(tmp_path):
    narrowed = "It runs on a Linux server."
    fake = FakeHermes(SEARCH, verdicts={LINUX: ("partly", narrowed)})
    fr, rec = run(tmp_path, fake)
    assert rec["status"] == "completed", rec["error"]
    assert rec["meaning_check"] == {"supported": 2, "partly": 2}
    citations = json.loads((fr.dir / "citations.json").read_text())
    linux = [c for c in citations if c["original_claim"] == LINUX]
    assert linux and all(c["claim"] == narrowed for c in linux)
    assert json.loads((fr.dir / "quote_check.json").read_text())["verdict"] == "pass"


def test_narrowed_claim_that_adds_a_number_is_rejected(tmp_path):
    fake = FakeHermes(SEARCH, verdicts={LINUX: ("partly", "It needs 2 GB of RAM on Linux.")})
    fr, rec = run(tmp_path, fake)
    assert rec["meaning_check"] == {"supported": 2, "unsupported": 2}
    assert all(c["check_reason"] == "narrowed claim was empty, not text, or added a number"
               for c in fr.store.claims(fr.run_id, status="unsupported"))


def test_checker_with_no_verdict_keeps_findings_out(tmp_path):
    fake = FakeHermes(SEARCH, checker_garbage=True)
    fr, rec = run(tmp_path, fake)
    assert rec["status"] == "stopped" and "meaning checks" in rec["error"]
    assert rec["claims"]["pass"] == 0 and rec["claims"]["meaning_unchecked"] == 4
    assert fake.count("check") == 4  # two tries per page, then the findings stay out
    assert rec["answer_path"] is None


def test_saved_verdicts_are_reused_when_a_landed_job_is_handled_again(tmp_path):
    fake = FakeHermes(SEARCH)
    fr, rec = run(tmp_path, fake)
    before = fake.count("check")
    job = fr.store.job(fr.run_id, next(j["id"] for j in fr.store.jobs(fr.run_id, kind="fetch")))
    fr.on_job_done(job, job["result"])
    assert fake.count("check") == before
    assert all(c["check_status"] == "pass" for c in fr.store.claims(fr.run_id))


def test_same_vendor_checker_stops_before_any_worker(tmp_path):
    cfg = small_config()
    cfg["models"]["checker"] = dict(cfg["models"]["worker"])
    fake = FakeHermes(SEARCH)
    fr, rec = run(tmp_path, fake, config=cfg)
    assert rec["status"] == "stopped" and "different vendor" in rec["error"]
    assert fake.count("search") == 0 and fake.count("fetch") == 0


def test_narrowed_ok_rules():
    quote = "runs on a Linux server with Python 3.11 and 2 GB RAM"
    assert narrowed_ok("It runs on Linux with Python 3.11.", quote)
    assert narrowed_ok("It needs 2 GB RAM.", quote)
    assert not narrowed_ok("It needs 4 GB RAM.", quote)
    assert not narrowed_ok("   ", quote)


def test_just_started_worker_is_seen_as_alive_and_a_reaped_one_as_dead():
    # Regression for the timing flake: a worker read right after launch looked dead under load.
    for i in range(20):
        marker = f"/offline/fast-mode-liveness/attempt-{i}"
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)", marker])
        try:
            assert fw._pid_alive(proc.pid, marker)
        finally:
            proc.wait(timeout=10)
        assert not fw._pid_alive(proc.pid, marker)


def test_zombie_worker_counts_as_dead():
    marker = "/offline/fast-mode-liveness/zombie"
    proc = subprocess.Popen([sys.executable, "-c", "pass", marker])
    deadline = time.time() + 10
    while fw._proc_state(proc.pid) != "Z" and time.time() < deadline:
        time.sleep(0.01)
    try:
        assert fw._proc_state(proc.pid) == "Z"
        started = time.monotonic()
        assert not fw._pid_alive(proc.pid, marker)
        assert time.monotonic() - started < 1  # no settle wait for a zombie
    finally:
        proc.wait(timeout=10)
