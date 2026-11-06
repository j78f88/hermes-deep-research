"""Regressions from the round 5 review of the meaning check and liveness fix.

Ported from the independent reviewer's offline sandbox tests, review/out-r5/test_review_r5.py.
Offline only: process launches other than two local fixtures are blocked, and no model, web or
state.db is used.
"""
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import fast_quote_check as qc
import fast_research as research
import fast_store
import fast_worker as fw
from fast_research import FastResearch, PhaseError
from fast_store import Store
from fast_test_support import FakeHermes, small_config

URL = "https://example.invalid/offline-r5"
QUOTE_A = "The tool explores a tree of sub-questions."
QUOTE_B = "The tool runs on Linux with Python 3.11."
CLAIM_A = "The tool explores sub-questions."
CLAIM_B = "The tool runs on Linux with Python 3.11 and needs much RAM."
TOPIC = {"id": "t1", "title": "Offline topic", "need": "Evidence"}


@pytest.fixture(autouse=True)
def prevent_live_processes(monkeypatch):
    original = subprocess.Popen

    def guarded(cmd, *args, **kwargs):
        # Only the two explicitly coded local process fixtures may execute.
        allowed = ("import time; time.sleep(2)", "pass")
        if (isinstance(cmd, list) and len(cmd) >= 3 and cmd[0] == sys.executable
                and cmd[1] == "-c" and cmd[2] in allowed):
            return original(cmd, *args, **kwargs)
        raise AssertionError("Blocked a non-fixture process launch")

    monkeypatch.setattr(subprocess, "Popen", guarded)
    monkeypatch.setattr(fw, "STATE_DB", BASE / "absent-session-r5.db")
    monkeypatch.setattr(fw, "WEB_CACHE", BASE / "absent-cache-r5")


class Replies:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    def __call__(self, model, prompt, *, workdir, **kwargs):
        self.calls.append((model, prompt, str(workdir)))
        if prompt.startswith("You check findings"):
            out = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        elif prompt.startswith("You write the final answer"):
            ids = re.findall(r"^\[C(\d+)\] \(topic", prompt, flags=re.M)
            out = "\n".join("An offline finding.[C%s]" % cid for cid in ids)
        else:
            raise AssertionError("Unexpected fixture prompt")
        now = time.time()
        return fw.OneshotResult(out, "", "offline-r5-%s" % len(self.calls), 0, None,
                                now, now, str(workdir))


def seed(tmp_path, engine, count=2):
    fr = FastResearch(tmp_path, "Offline question?", run_id="r5", config=small_config(),
                      oneshot=engine, harvest_pages=lambda _: [], harvest_search=lambda _: [], usage=lambda _: [])
    page = fr.store.save_page(URL, QUOTE_A + "\n" + QUOTE_B, title="Offline page")
    fr.store.add_job(fr.run_id, "page-offline", "read", {"url": URL, "topic": TOPIC}, "t1")
    job = fr.store.claim_next(fr.run_id)
    result = {"reply": {"claims": [
        {"claim": CLAIM_A, "quote": QUOTE_A},
        {"claim": CLAIM_B, "quote": QUOTE_B},
    ][:count]}, "pages": [{"url": page["url"], "sha256": page["sha256"]}]}
    fr.store.land_job(fr.run_id, job["id"], result)
    return fr, job, result


def citation(fr, cid):
    c = fr.store.claim(cid)
    return {"n": 1, "claim_id": cid, "quote": c["quote"], "page_url": c["page_url"],
            "page_sha256": c["page_sha256"]}


def test_live_pid_marker_mismatch_and_reaped_pid():
    marker = str(BASE / "local-live-marker")
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)", marker])
    try:
        assert fw._pid_alive(proc.pid, marker)
        assert not fw._pid_alive(proc.pid, "not-the-worker-marker")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert not fw._pid_alive(proc.pid, marker)
    assert not fw._pid_alive(None, marker)


def test_zombie_pid_is_dead_without_settle_wait():
    proc = subprocess.Popen([sys.executable, "-c", "pass", str(BASE / "zombie-marker")])
    try:
        deadline = time.monotonic() + 5
        while fw._proc_state(proc.pid) != "Z" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fw._proc_state(proc.pid) == "Z"
        started = time.monotonic()
        assert not fw._pid_alive(proc.pid, str(BASE / "zombie-marker"))
        assert time.monotonic() - started < 0.5
    finally:
        proc.wait(timeout=5)


@pytest.mark.parametrize("eventual_match", [False, True])
def test_empty_cmdline_is_retried_with_a_bounded_deadline(monkeypatch, eventual_match):
    clock = [0.0]
    reads = [0]
    marker = "/offline-r5-marker"
    monkeypatch.setattr(fw.os, "kill", lambda *_: None)
    monkeypatch.setattr(fw, "_proc_state", lambda _: "S")
    monkeypatch.setattr(fw.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fw.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def read_bytes(_):
        reads[0] += 1
        assert reads[0] < 150, "An empty command line must not loop forever"
        return b"python\0/offline-r5-marker\0" if eventual_match and reads[0] >= 3 else b""

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    assert fw._pid_alive(12345, marker) is eventual_match
    assert 3 <= reads[0] <= 102
    assert clock[0] <= 2.02
    print(json.dumps({"eventual_match": eventual_match, "reads": reads[0], "virtual_seconds": clock[0]}))


def test_process_stat_name_can_contain_spaces_and_parentheses(monkeypatch):
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: "123 (a name ) with spaces) Z 1 2 3")
    assert fw._proc_state(123) == "Z"


def test_supported_partly_and_saved_verdicts_gate_writer_and_citations(tmp_path):
    engine = Replies({"checks": [{"n": 1, "verdict": "supported"},
                                  {"n": 2, "verdict": "partly", "supported_claim": "The tool runs on Linux."}]})
    fr, job, result = seed(tmp_path, engine)
    fr.on_job_done(job, result)
    before = len(engine.calls)
    fr.on_job_done(job, result)
    assert len(engine.calls) == before == 1
    assert all(c["check_status"] == "pass" for c in fr.store.claims(fr.run_id))
    answer = fr.answer()
    assert answer["quote_check"] == "pass"
    writer = engine.calls[-1][1]
    assert CLAIM_A in writer and CLAIM_B not in writer
    assert "The tool runs on Linux." in writer
    citations = json.loads(fr.path("citations.json").read_text())
    assert citations[1]["claim"] == "The tool runs on Linux."
    assert citations[1]["original_claim"] == CLAIM_B


@pytest.mark.parametrize("reply", [
    "not JSON", {}, {"checks": []}, {"checks": ["garbage", None]},
    {"checks": [{"n": 1, "verdict": "nonsense"}]},
    {"checks": [{"n": "1", "verdict": "supported"}]},
    {"checks": [{"n": 99, "verdict": "supported"}]},
    {"checks": [{"n": 1, "verdict": "unsupported"}]},
    {"checks": [{"n": 1, "verdict": "partly", "supported_claim": ""}]},
    {"checks": [{"n": 1, "verdict": "partly", "supported_claim": "It needs 128 GB RAM."}]},
], ids=["non-json", "missing-checks", "empty", "invalid-rows", "unknown-verdict",
        "string-number", "out-of-range-number", "unsupported", "empty-partly", "added-number"])
def test_garbage_missing_and_unsupported_stay_out(tmp_path, reply):
    engine = Replies(reply)
    fr, job, result = seed(tmp_path, engine, count=1)
    fr.on_job_done(job, result)
    assert fr.store.claims(fr.run_id)[0]["check_status"] != "pass"
    with pytest.raises(PhaseError):
        fr.answer()
    assert not any(p.startswith("You write the final answer") for _, p, _ in engine.calls)
    assert not fr.path("citations.json").exists()
    before = len(engine.calls)
    fr.on_job_done(job, result)
    assert len(engine.calls) == before


@pytest.mark.parametrize("bad_row", [
    {"n": True, "verdict": "supported"},
    {"n": 1, "verdict": "partly", "supported_claim": {"invented": "object is not a claim string"}},
    {"n": 1, "verdict": "partly", "supported_claim": True},
], ids=["boolean-finding-number", "object-partly", "boolean-partly"])
def test_malformed_checker_rows_cannot_reach_writer(tmp_path, bad_row):
    engine = Replies({"checks": [bad_row]})
    fr, job, result = seed(tmp_path, engine, count=1)
    fr.on_job_done(job, result)
    status = fr.store.claims(fr.run_id)[0]["check_status"]
    if status == "pass":
        answer = fr.answer()
        print(json.dumps({"malformed_row": bad_row, "status": status,
                          "quote_check": answer["quote_check"],
                          "citations": json.loads(fr.path("citations.json").read_text())}))
    assert status != "pass", "Malformed checker JSON must fail closed, not be coerced into a valid verdict"


@pytest.mark.parametrize("status", ["quote_ok", "pending", "fail", "unsupported", "meaning_unchecked"])
def test_quote_checker_rejects_every_nonpass_status(tmp_path, status):
    fr, job, result = seed(tmp_path, Replies({}))
    page = fr.store.get_page(URL)
    cid = fr.store.add_claim(fr.run_id, job["id"], CLAIM_A, QUOTE_A, page["url"], page["sha256"])
    fr.store.set_claim_check(cid, status, "offline status")
    out = qc.check_answer(fr.store, fr.run_id, "An offline finding.[1]", [citation(fr, cid)])
    assert out["verdict"] == "fail" and out["problems"][0]["kind"] == "claim_not_passed"


def test_old_database_without_support_table_opens(tmp_path):
    db = tmp_path / "fast.db"
    old_schema = re.sub(r"CREATE TABLE IF NOT EXISTS claim_support \(.*?\);", "", fast_store.SCHEMA, flags=re.S)
    with sqlite3.connect(db) as c:
        c.executescript(old_schema)
        assert not c.execute("SELECT name FROM sqlite_master WHERE name='claim_support'").fetchall()
    s = Store(db)
    assert s.supports("old-run") == []
    assert s.conn().execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_unfinished_legacy_run_does_not_send_quote_only_pass_to_writer(tmp_path):
    fake = FakeHermes({})
    fr = FastResearch(tmp_path, "Legacy offline question?", run_id="legacy", config=small_config(), **fake.hooks())
    p = fr.store.save_page(URL, QUOTE_A, title="Legacy page")
    cid = fr.store.add_claim(fr.run_id, "old-page-job", CLAIM_A, QUOTE_A, p["url"], p["sha256"], "t1")
    # This is exactly the old code's quote-only pass, on a private synthetic database.
    assert qc.check_claim(fr.store, cid) == (True, "ok")
    with fr.store.tx() as c:
        c.execute("DROP TABLE claim_support")
    for name in ("preflight", "plan", "research"):
        fr.store.start_phase(fr.run_id, name)
        fr.store.end_phase(fr.run_id, name, {})
    fr.store.close()
    resumed = FastResearch(tmp_path, "Legacy offline question?", run_id="legacy", config=small_config(), **fake.hooks())
    rec = resumed.run()
    print(json.dumps({"status": rec["status"], "meaning_check": rec["meaning_check"],
                      "writer_calls": fake.count("writer"), "quote_check": rec["quote_check"]}))
    assert fake.count("writer") == 0 or resumed.store.support(cid) is not None, (
        "Opening an old DB must not treat its quote-only pass as a meaning-checked finding")


class SimulatedCrash(BaseException):
    pass


def test_rehandling_never_downgrades_a_saved_pass_even_if_interrupted(tmp_path, monkeypatch):
    engine = Replies({"checks": [{"n": 1, "verdict": "supported"}, {"n": 2, "verdict": "supported"}]})
    fr, job, result = seed(tmp_path, engine)
    fr.on_job_done(job, result)
    saved = {c["id"]: fr.store.support(c["id"]) for c in fr.store.claims(fr.run_id)}

    def crash_before_restore(*_):
        raise SimulatedCrash("Controller died between quote recheck and support restoration")

    monkeypatch.setattr(fr, "_meaning_check", crash_before_restore)
    with pytest.raises(SimulatedCrash):
        fr.on_job_done(job, result)
    statuses = [c["check_status"] for c in fr.store.claims(fr.run_id)]
    assert all(fr.store.support(cid) == sup for cid, sup in saved.items())
    print(json.dumps({"saved_supports": len(saved), "statuses_after_interruption": statuses}))
    assert statuses == ["pass", "pass"], "A repeated handler downgraded already saved passes"


def test_resume_adopts_the_original_checker_batch_without_checking_findings_twice(tmp_path, monkeypatch):
    launches = []

    class OfflineCheckerProc:
        pid = 999999999

        def __init__(self, cmd, stdout, stderr, **kwargs):
            prompt = Path(cmd[cmd.index("--query-file") + 1]).read_text()
            assert prompt.startswith("You check findings")
            wd = cmd[cmd.index("--in") + 1]
            launches.append({"folder": wd, "claims": re.findall(r"^\d+\. Claim: (.*)$", prompt, flags=re.M)})
            nums = re.findall(r"^(\d+)\. Claim:", prompt, flags=re.M)
            stdout.write(json.dumps({"checks": [{"n": int(n), "verdict": "supported"} for n in nums]}))
            stderr.write("session_id: offline-checker-%s\n" % len(launches))

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(fw.subprocess, "Popen", OfflineCheckerProc)
    monkeypatch.setattr(fw, "hermes_bin", lambda: "/offline-never-executed/hermes")
    monkeypatch.setattr(fw, "_pid_alive", lambda *_a, **_k: False)
    fr, job, result = seed(tmp_path, fw.oneshot)

    def crash_on_save(*args, **kwargs):
        raise SimulatedCrash("Crash after the checker finished, before its verdicts were saved")

    monkeypatch.setattr(fr.store, "set_supports", crash_on_save)
    with pytest.raises(SimulatedCrash):
        fr.on_job_done(job, result)
    fr.store.close()
    resumed = FastResearch(tmp_path, "Offline question?", run_id="r5", config=small_config(),
                           oneshot=fw.oneshot, harvest_pages=lambda _: [], harvest_search=lambda _: [], usage=lambda _: [])
    resumed.on_job_done(job, result)
    assert all(c["check_status"] == "pass" for c in resumed.store.claims(resumed.run_id))
    assert len(launches) == 1, "Resume started another checker for findings already covered by a saved reply"


def test_a_checker_batch_is_saved_whole_or_not_at_all(tmp_path):
    fr, job, result = seed(tmp_path, Replies({}))
    rows = [{"claim_id": 1, "run_id": fr.run_id, "verdict": "supported"},
            {"claim_id": 2, "run_id": fr.run_id}]  # the second row is broken
    with pytest.raises(KeyError):
        fr.store.set_supports(rows)
    assert fr.store.supports(fr.run_id) == []


def test_a_new_pending_finding_uses_a_new_checker_folder(tmp_path, monkeypatch):
    engine = Replies({"checks": [{"n": 1, "verdict": "supported"}]})
    fr, job, result = seed(tmp_path, engine, count=1)
    fr.on_job_done(job, result)
    first_folder = engine.calls[0][2]
    result["reply"]["claims"].append({"claim": "Linux is mentioned.", "quote": QUOTE_B})
    fr.on_job_done(job, result)
    assert len(engine.calls) == 2
    assert engine.calls[1][2] != first_folder
    assert CLAIM_A not in engine.calls[1][1]
    assert "1. Claim: Linux is mentioned." in engine.calls[1][1]


def test_same_vendor_served_checker_cannot_approve_findings(tmp_path):
    fake = FakeHermes({"q1": [URL], "q2": [], "q3": [], "q4": []})
    original = fake.usage

    def served_usage(sids):
        rows = original(sids)
        for row in rows:
            if row["model"].startswith("google/"):
                row["model"] = "anthropic/claude-haiku-5.5"
        return rows

    hooks = fake.hooks()
    hooks["usage"] = served_usage
    fr = FastResearch(tmp_path, "Vendor fallback offline fixture?", config=small_config(), **hooks)
    rec = fr.run()
    print(json.dumps({"status": rec["status"], "models_served": rec["models_served"],
                      "meaning_check": rec["meaning_check"]}))
    assert rec["status"] != "completed" or rec["claims"]["pass"] == 0, (
        "A checker served by the worker's vendor must not provide independent approval")


# Round 6 review regressions, ported from review/out-r6/test_review_r6.py.

def test_worker_and_checker_served_by_same_vendor_cannot_write(tmp_path):
    fake = FakeHermes({"q1": [URL], "q2": [], "q3": [], "q4": []})
    original_usage = fake.usage

    def usage(sids):
        rows = original_usage(sids)
        for row in rows:
            if row["model"] == small_config()["models"]["worker"]["id"]:
                row["model"] = small_config()["models"]["checker"]["id"]
        return rows

    hooks = fake.hooks()
    hooks["usage"] = usage
    fr = FastResearch(tmp_path, "Offline worker vendor fallback", run_id="served-worker", config=small_config(), **hooks)
    rec = fr.run()
    assert rec["status"] == "stopped" and "same vendor" in rec["error"]
    assert fake.count("writer") == 0


def test_legacy_answer_already_marked_done_is_checked_and_rewritten(tmp_path):
    fake = FakeHermes({})
    fr = FastResearch(tmp_path, "Offline old answer", run_id="legacy-answer", config=small_config(), **fake.hooks())
    p = fr.store.save_page(URL, QUOTE_A, title="Legacy page")
    cid = fr.store.add_claim(fr.run_id, "old-page-job", CLAIM_A, QUOTE_A, p["url"], p["sha256"], "t1")
    assert qc.check_claim(fr.store, cid) == (True, "ok")  # the old code's quote-only pass
    rendered, cits, _ = fr.render("Old finding.[C%s]" % cid, {cid: fr.store.claim(cid)})
    fr.path("answer.md").write_text(rendered)
    fr.write_json("citations.json", cits)
    with fr.store.tx() as c:
        c.execute("DROP TABLE claim_support")
    for name in ("preflight", "plan", "research", "answer"):
        fr.store.start_phase(fr.run_id, name)
        fr.store.end_phase(fr.run_id, name, {})
    fr.store.close()
    resumed = FastResearch(tmp_path, "Offline old answer", run_id="legacy-answer", config=small_config(), **fake.hooks())
    rec = resumed.run()
    assert resumed.store.support(cid) is not None and fake.count("check") == 1
    assert rec["status"] == "completed" and fake.count("writer") == 1 and rec["quote_check"] == "pass"
    assert list(resumed.dir.glob("answer.md.legacy-*")) and list(resumed.dir.glob("citations.json.legacy-*"))


# Round 7 review regressions, ported from review/out-r7/test_review_r7.py.

def _legacy_answer(root, verdict="partly"):
    narrowed = "The tool runs on Linux."
    fake = FakeHermes({}, verdicts={CLAIM_B: (verdict, narrowed if verdict == "partly" else None)})
    cfg = small_config()
    fr = FastResearch(root, "Offline legacy migration", run_id="legacy-r7", config=cfg, **fake.hooks())
    page = fr.store.save_page(URL, QUOTE_B, title="Legacy project page")
    cid = fr.store.add_claim(fr.run_id, "legacy-page", CLAIM_B, QUOTE_B, page["url"], page["sha256"], "t1")
    assert qc.check_claim(fr.store, cid) == (True, "ok")
    fake.sessions["legacy-reader"] = {"model": cfg["models"]["worker"]["id"]}
    fr.store.add_call(fr.run_id, "legacy-reader", "read", cfg["models"]["worker"]["id"], "legacy-page")
    text, citations, dropped = fr.render(CLAIM_B + "[C%s]" % cid, {cid: fr.store.claim(cid)})
    assert not dropped
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", citations)
    with fr.store.tx() as conn:
        conn.execute("DROP TABLE claim_support")
    for name in ("preflight", "plan", "research", "answer"):
        fr.store.start_phase(fr.run_id, name)
        fr.store.end_phase(fr.run_id, name, {})
    fr.store.close()
    return fake, cid, narrowed


def _reopen_legacy(root, fake):
    return FastResearch(root, "Offline legacy migration", run_id="legacy-r7", config=small_config(), **fake.hooks())


def test_crash_after_legacy_narrowing_cannot_reuse_the_old_broad_answer(tmp_path, monkeypatch):
    fake, cid, narrowed = _legacy_answer(tmp_path, "partly")
    fr = _reopen_legacy(tmp_path, fake)
    original = fr._check_legacy_passes

    def persist_then_crash():
        original()
        raise SimulatedCrash("Crash after durable narrowing but before invalidating the old answer")

    monkeypatch.setattr(fr, "_check_legacy_passes", persist_then_crash)
    with pytest.raises(SimulatedCrash):
        fr.run()
    assert fr.store.support(cid)["verdict"] == "partly"
    fr.store.close()
    resumed = _reopen_legacy(tmp_path, fake)
    rec = resumed.run()
    citations = json.loads(resumed.path("citations.json").read_text())
    assert rec["status"] == "completed" and citations[0]["claim"] == narrowed
    assert fake.count("check") == 1 and fake.count("writer") == 1


def test_resumed_saved_answer_rechecks_worker_checker_vendor_evidence(tmp_path, monkeypatch):
    fake = FakeHermes({"q1": [URL], "q2": [], "q3": [], "q4": []})
    hooks = fake.hooks()
    fr = FastResearch(tmp_path, "Offline resume served-vendor guard", run_id="vendor-resume", config=small_config(), **hooks)

    def stop_before_review():
        raise SimulatedCrash("Controller stopped after answer but before review")

    monkeypatch.setattr(fr, "review", stop_before_review)
    with pytest.raises(SimulatedCrash):
        fr.run()
    assert fr.store.phase(fr.run_id, "answer")["status"] == "done"
    worker, checker = small_config()["models"]["worker"]["id"], small_config()["models"]["checker"]["id"]
    for session in fake.sessions.values():
        if session["model"] == worker:
            session["model"] = checker  # usage rows now show the reading workers on the checker's vendor
    fr.store.close()
    resumed = FastResearch(tmp_path, "Offline resume served-vendor guard", run_id="vendor-resume", config=small_config(), **hooks)
    rec = resumed.run()
    assert rec["status"] == "stopped" and "same vendor" in rec["error"]
    assert fake.count("review") == 0
