"""Tests for launch_controller.py, using a fake hermes script. No real model call."""
import json
import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

import launch_controller as lc

FAKE = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    calls = os.environ["FAKE_CALLS"]
    n = sum(1 for _ in open(calls)) if os.path.exists(calls) else 0
    with open(calls, "a") as fh:
        fh.write(json.dumps(sys.argv[1:]) + "\\n")
    plan = os.environ["FAKE_PLAN"].split(",")
    outcome = plan[min(n, len(plan) - 1)]
    sid = "sess-%d" % (n + 1) if "--resume" not in sys.argv else sys.argv[sys.argv.index("--resume") + 1]
    print("final reply text")
    if outcome == "steps":
        with open(os.environ["FAKE_AGENT_LOG"], "a") as fh:
            fh.write("2026-11-08 01:00:00,000 INFO [%s] agent.conversation_loop: Turn ended: "
                     "reason=max_iterations_reached(500/500) model=x api_calls=500/500 session=%s\\n" % (sid, sid))
    if outcome == "other_session_steps":  # a step line for a different session must not count
        with open(os.environ["FAKE_AGENT_LOG"], "a") as fh:
            fh.write("2026-11-08 01:00:00,000 INFO [zzz] agent.conversation_loop: Turn ended: "
                     "reason=max_iterations_reached(500/500) model=x session=zzz\\n")
    if outcome == "fork_step_then_provider_error":  # fork step stop, then the controller's own different end
        with open(os.environ["FAKE_AGENT_LOG"], "a") as fh:
            fh.write("2026-11-08 01:00:00,000 INFO [%s] agent.conversation_loop: Turn ended: "
                     "reason=max_iterations_reached(5/5) model=x api_calls=5/5 session=%s origin=background_review\\n"
                     % (sid, sid))
            fh.write("2026-11-08 01:00:01,000 INFO [%s] agent.conversation_loop: Turn ended: "
                     "reason=provider_error model=x api_calls=3/500 session=%s\\n" % (sid, sid))
    if outcome == "rotated_steps":  # step stop, then the log rotates before the process exits
        log = os.environ["FAKE_AGENT_LOG"]
        with open(log, "a") as fh:
            fh.write("2026-11-08 01:00:00,000 INFO [%s] agent.conversation_loop: Turn ended: "
                     "reason=max_iterations_reached(500/500) model=x api_calls=500/500 session=%s\\n" % (sid, sid))
        os.rename(log, log + ".1")
        with open(log, "w") as fh:
            fh.write("2026-11-08 01:00:02,000 INFO [%s] shutdown logging\\n" % sid)
    if outcome != "nosid":
        print("\\nsession_id: " + sid, file=sys.stderr)
    sys.exit(0 if outcome == "ok" else 1)
''')


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "hermes"
    fake.write_text(FAKE.format(python=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    root = tmp_path / "campaign"
    root.mkdir()
    packet = tmp_path / "packet.md"
    packet.write_text("do the research\n")
    agent_log = tmp_path / "agent.log"
    agent_log.write_text("2026-11-07 old line max_iterations_reached(200/200) session=sess-1\n")  # stale, must be ignored
    monkeypatch.setenv("FAKE_CALLS", str(tmp_path / "calls.jsonl"))
    monkeypatch.setenv("FAKE_AGENT_LOG", str(agent_log))

    def run(plan, *extra):
        monkeypatch.setenv("FAKE_PLAN", plan)
        code = lc.main(["--packet", str(packet), "--root", str(root), "--hermes-bin", str(fake),
                        "--agent-log", str(agent_log), *extra])
        calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
        record = json.loads((root / "receipts" / "controller-launch.json").read_text())
        return code, calls, record

    run.root, run.packet = root, packet
    return run


def test_start_command_session_id_and_record(env):
    code, calls, record = env("ok")
    assert code == 0 and len(calls) == 1
    assert calls[0] == ["chat", "--oneshot", "-Q", "--ignore-rules", "--max-turns", "500",
                        "--query-file", str(env.packet), "--in", str(env.root)]
    assert "--source" not in calls[0]
    assert record["status"] == "completed"
    assert record["session_id"] == "sess-1"
    assert record["exit_code"] == 0 and record["resume_count"] == 0
    assert record["started_at"] and Path(record["log"]).is_file()
    assert record["command"][1:3] == ["chat", "--oneshot"]
    assert "session_id: sess-1" in Path(record["log"]).read_text()


def test_one_resume_after_step_limit(env):
    code, calls, record = env("steps,ok")
    assert len(calls) == 2
    resume = calls[1]
    assert resume[:3] == ["chat", "--oneshot", "-Q"]
    assert resume[3:5] == ["--resume", "sess-1"]
    assert resume[5] == "--query-file" and resume[7:] == ["--in", str(env.root)]
    for policy_flag in ("--max-turns", "--ignore-rules", "--source", "-m", "-t"):
        assert policy_flag not in resume
    assert Path(resume[6]).is_file()
    assert code == 0
    assert record["resume_count"] == 1 and record["status"] == "completed"
    assert [r["kind"] for r in record["runs"]] == ["start", "resume"]
    assert record["runs"][0]["stopped_for_steps"] is True
    assert record["session_id"] == "sess-1"


def test_no_second_resume(env):
    code, calls, record = env("steps")
    assert len(calls) == 2
    assert code == 1
    assert record["resume_count"] == 1
    assert record["status"] == "step_limit_after_resume"
    assert record["runs"][1]["stopped_for_steps"] is True


def test_no_resume_after_other_failure(env):
    code, calls, record = env("fail")
    assert len(calls) == 1 and code == 1
    assert record["status"] == "failed" and record["resume_count"] == 0
    assert record["exit_code"] == 1 and record["session_id"] == "sess-1"


def test_step_line_for_other_session_does_not_resume(env):
    code, calls, record = env("other_session_steps")
    assert len(calls) == 1 and record["resume_count"] == 0


def test_missing_session_id_is_not_resumed(env):
    code, calls, record = env("nosid")
    assert len(calls) == 1 and record["session_id"] is None and record["status"] == "failed"


def test_custom_resume_packet_and_passthrough(env, tmp_path):
    resume_packet = tmp_path / "resume.md"
    resume_packet.write_text("continue\n")
    code, calls, record = env("steps,ok", "--resume-packet", str(resume_packet), "--max-turns", "400",
                              "--", "-m", "some/model", "-t", "web,terminal,file")
    assert calls[0][calls[0].index("--max-turns") + 1] == "400"
    assert calls[0][-4:] == ["-m", "some/model", "-t", "web,terminal,file"]
    assert calls[1][calls[1].index("--query-file") + 1] == str(resume_packet)
    assert "-m" not in calls[1]


def test_owned_flags_are_rejected(env):
    with pytest.raises(SystemExit):
        lc.parse(["--packet", "p", "--root", "r", "--", "--source", "dr875-dr"])
    with pytest.raises(SystemExit):
        lc.parse(["--packet", "p", "--root", "r", "--", "--resume=abc"])


def test_missing_binary_is_recorded(env, tmp_path):
    code = lc.main(["--packet", str(env.packet), "--root", str(env.root), "--hermes-bin", str(tmp_path / "nope"),
                    "--agent-log", str(tmp_path / "agent.log")])
    record = json.loads((env.root / "receipts" / "controller-launch.json").read_text())
    assert code == 127 and record["status"] == "failed" and record["resume_count"] == 0


def test_fork_step_stop_then_controller_error_does_not_resume(env):
    code, calls, record = env("fork_step_then_provider_error")
    assert len(calls) == 1 and code == 1
    assert record["resume_count"] == 0 and record["status"] == "failed"
    assert record["runs"][0]["stopped_for_steps"] is False


def test_step_stop_moved_to_rotated_log_still_resumes_once(env):
    code, calls, record = env("rotated_steps,ok")
    assert len(calls) == 2 and calls[1][3:5] == ["--resume", "sess-1"]
    assert record["resume_count"] == 1 and record["status"] == "completed"
    assert record["runs"][0]["stopped_for_steps"] is True


def _turn(reason, sid, origin=""):
    return f"INFO Turn ended: reason={reason} model=x api_calls=1/1 session={sid}{origin}\n"


STEP = "max_iterations_reached(500/500)"


def test_stopped_for_steps_reads_original_file_across_several_rotations(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(_turn(STEP, "s1") + "pre-launch line\n")  # stale line before the launch point
    mark = lc.log_mark(log)
    with log.open("a") as fh:
        fh.write(_turn(STEP, "s1"))
    log.rename(tmp_path / "agent.log.1")  # rotation 1
    (tmp_path / "agent.log.1").rename(tmp_path / "agent.log.2")
    (tmp_path / "agent.log.3").write_text(_turn(STEP, "s1"))  # older backup, before the launch file; must be ignored
    log.write_text("INFO newer file\n")
    (tmp_path / "agent.log.1").write_text("INFO middle file\n")
    assert lc.stopped_for_steps(log, mark, "s1") is True
    assert lc.stopped_for_steps(log, mark, "other") is False


def test_stopped_for_steps_ignores_stale_stop_before_launch_mark(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text(_turn(STEP, "s1"))
    mark = lc.log_mark(log)
    log.rename(tmp_path / "agent.log.1")
    log.write_text(_turn("provider_error", "s1"))
    assert lc.stopped_for_steps(log, mark, "s1") is False


def test_stopped_for_steps_when_log_absent_at_launch_or_original_gone(tmp_path):
    log = tmp_path / "agent.log"
    mark = lc.log_mark(log)
    assert mark == (None, 0)
    log.write_text(_turn(STEP, "s1"))
    assert lc.stopped_for_steps(log, mark, "s1") is True
    gone = (log.stat().st_ino + 12345, 10)  # launch file no longer exists anywhere
    assert lc.stopped_for_steps(log, gone, "s1") is True


def test_last_foreground_turn_end_decides(tmp_path):
    log = tmp_path / "agent.log"
    log.write_text("")
    mark = lc.log_mark(log)
    log.write_text(_turn(STEP, "s1") + _turn("provider_error", "s1"))
    assert lc.stopped_for_steps(log, mark, "s1") is False
    log.write_text(_turn("provider_error", "s1") + _turn(STEP, "s1") + _turn("text_response", "s1", " origin=background_review"))
    assert lc.stopped_for_steps(log, mark, "s1") is True


def test_wait_for_jobs_markers_timeout_and_fake_clock(tmp_path):
    import wait_for_jobs as w

    m1 = tmp_path / "m1"
    m1.write_text("x")
    assert w.wait_for_jobs(markers=[str(m1)], timeout=5, interval=0.05)["status"] == "done"
    m2 = tmp_path / "m2"
    assert w.wait_for_jobs(markers=[str(m2)], timeout=0.1, interval=0.02)["status"] == "timeout"

    ticks = {"t": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        ticks["t"] += s
        if ticks["t"] >= 2:
            m2.write_text("x")

    res = w.wait_for_jobs(markers=[str(m2)], timeout=10, interval=1, sleep=sleep, clock=lambda: ticks["t"])
    assert res["status"] == "done" and sleeps == [1, 1]
