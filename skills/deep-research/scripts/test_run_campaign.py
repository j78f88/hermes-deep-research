"""Tests for run_campaign.py, the phased run driver. A fake hermes binary; no model call."""
import json
import os
import sqlite3
import stat
import sys
import textwrap
from pathlib import Path

import pytest

import run_campaign as rc
import run_state_machine as rsm

FAKE = textwrap.dedent(r'''
    #!{python}
    import hashlib, json, os, re, sys
    from pathlib import Path
    argv = sys.argv[1:]
    fake = Path(os.environ["FAKE_DIR"])
    fake.mkdir(parents=True, exist_ok=True)
    def arg(flag):
        return argv[argv.index(flag) + 1] if flag in argv else None
    root = Path(arg("--in"))
    if "--resume" in argv:  # one file per session: lanes and reviews run in parallel
        sid = arg("--resume"); name = (fake / f"sid-{{sid}}").read_text(); kind = "resume"
    else:
        head = Path(arg("--query-file")).read_text().splitlines()[0]
        name = re.search(r"name: ([^ ]+) -->", head).group(1); kind = "start"
    counter = fake / f"count-{{name}}"
    n = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(n))
    if kind == "start":
        sid = f"sess-{{name}}-{{n}}"; (fake / f"sid-{{sid}}").write_text(name)
    with open(fake / "calls.jsonl", "a") as fh:
        fh.write(json.dumps({{"name": name, "kind": kind, "argv": argv}}) + "\n")
    plan = json.loads(os.environ.get("FAKE_PLAN", "{{}}")).get(name, ["ok"])
    outcome = plan[min(n - 1, len(plan) - 1)]
    def done(code):
        print("reply")
        print("\nsession_id: " + sid, file=sys.stderr)
        sys.exit(code)
    if outcome == "steps":
        with open(os.environ["FAKE_AGENT_LOG"], "a") as fh:
            fh.write(f"INFO Turn ended: reason=max_iterations_reached(5/5) model=x api_calls=5/5 session={{sid}}\n")
        done(1)
    if outcome == "fail":
        done(1)
    RECEIPTS = {{"evidence_privacy": "qa/evidence-privacy-review.json", "context_handoff": "qa/context-handoff-review.json",
                "visual": "qa/visual-review.json", "taxonomy_review": "qa/taxonomy-review.json",
                "librarian_repair": "qa/librarian-repair-receipt.json", "final_assurance": "qa/final-assurance.json"}}
    if name.startswith("lane-"):
        lane = name[5:]
        src = root / "workspace" / "sources" / lane
        src.mkdir(parents=True, exist_ok=True)
        f = src / "S01.md"
        if not f.exists():
            f.write_text("<!-- url: https://example.com -->\nThe example protocol defines sixteen event types for streaming agent output.\n")
            row = {{"file": "S01.md", "source_id": "S01", "url": "https://example.com", "method": "direct",
                   "sha256": hashlib.sha256(f.read_bytes()).hexdigest(), "chars": 80, "fetched_at": "2026-11-11T00:00:00+00:00"}}
            (src / "index.jsonl").write_text(json.dumps(row) + "\n")
        p = root / "lanes" / f"{{lane}}.json"
        d = json.loads(p.read_text())
        d.update(status="pass", summary=f"fake lane run {{n}}",
                 sources=[{{"id": "S01", "title": "Example", "url_or_ref": "https://example.com", "source_class": "official",
                           "accessed_or_checked": "2026-11-11"}}],
                 findings=[{{"id": "F01", "claim": "The protocol defines sixteen event types.", "evidence_class": "reported",
                            "confidence": "high", "source_ids": ["S01"], "source_file": "S01.md",
                            "quote": "defines sixteen event types for streaming agent output"}}],
                 acquisition={{"source_type": "web_page", "method": "fake", "provenance": "live",
                              "coverage": "full_text_captured", "degraded_state": ["none"]}})
        d["verification"]["open_verification_gaps"] = []
        p.write_text(json.dumps(d, indent=2))
        (root / "lanes" / f"{{lane}}.md").write_text(f"# {{lane}}\n\nfake run {{n}}\n")
    elif name == "synthesis":
        pack = root / "internal" / "reference-pack.md"
        text = pack.read_text()
        fm = text[: text.index("\n---\n", 4) + 5]
        body = "\n## Answer and material limitation\n\nIt defines events [L01-S01].\n"
        body += "".join(f"\n### Exhibit E{{i}} — Table {{i}}\n\n| a | b |\n|---|---|\n| x | y [L01-S01] |\n" for i in (1, 2, 3))
        body += f"\n## Sources\n\n- [L01-S01] Example. https://example.com\n\nsynthesis run {{n}}\n"
        pack.write_text(fm + body)
        ex = [{{"id": f"E{{i}}", "title": f"Table {{i}}", "type": "table", "output_class": "reference_pack",
               "appears_in": ["reference_pack"], "anchor": f"Exhibit E{{i}} — Table {{i}}", "evidence_refs": ["L01-S01"],
               "decision_use": "shows the events"}} for i in (1, 2, 3)]
        (root / "synthesis" / "visual-exhibits.json").write_text(json.dumps({{"status": "complete", "exhibits": ex}}))
        (root / "synthesis" / "action-register.md").write_text("No material actions: this is a factual report.\n")
    elif name.startswith("review-"):
        review = re.sub(r"-r\d+$", "", name[7:])
        status = "HOLD" if outcome == "hold" else "PASS"
        pack_sha = hashlib.sha256((root / "internal" / "reference-pack.md").read_bytes()).hexdigest()
        (root / RECEIPTS[review]).write_text(json.dumps({{"schema_version": "deep-research-qa-receipt-v1", "name": review,
            "status": status, "reviewer": "fake/reviewer", "producers": ["fake/writer"],
            "reference_pack_sha256": pack_sha,
            "output_classes": {{"reference_pack": status}},
            "requested_repairs": ["fix it"] if status == "HOLD" else []}}))
    elif name.startswith("repair-"):
        pack = root / "internal" / "reference-pack.md"
        pack.write_text(pack.read_text() + f"\nrepaired {{n}}\n")
        (root / "qa" / "repairs").mkdir(parents=True, exist_ok=True)
        (root / "qa" / "repairs" / f"round-{{name[8:]}}-log.json").write_text("{{}}")
    done(0)
''')

LANES = ["01-steering-source-map", "02-current-state-truth", "04-prior-art-alternatives"]


def make_state_db(path: Path) -> Path:
    """An empty Hermes session store, so the lane read audit runs and records zero hits."""
    con = sqlite3.connect(path)
    con.execute("create table sessions (id text, parent_session_id text, model text, tool_call_count int, "
                "actual_cost_usd real, estimated_cost_usd real)")
    con.execute("create table messages (session_id text, tool_calls text)")
    con.commit()
    con.close()
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    fake = tmp_path / "hermes"
    fake.write_text(FAKE.format(python=sys.executable).lstrip())
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    agent_log = tmp_path / "agent.log"
    agent_log.write_text("")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path / "fake"))
    monkeypatch.setenv("FAKE_AGENT_LOG", str(agent_log))
    monkeypatch.setenv("FAKE_PLAN", "{}")
    root = tmp_path / "campaign"
    state_db = make_state_db(tmp_path / "state.db")
    mechanics = {"plain_brief": 0, "completion": 0, "export": 0}

    def preflight(root, primary):
        (root / "receipts" / "tool-preflight.json").write_text('{"tools": []}\n')
        return {"step": "tool_preflight", "status": "done", "failed": []}

    def owned(root, target, supplied):
        (root / "context" / "owned-assets.md").write_text("# Owned assets\n\n- none\n")
        return {"step": "owned_assets", "status": "done"}

    def plain_brief(root, log, timeout):
        mechanics["plain_brief"] += 1
        (root / "internal" / "plain-brief.md").write_text(
            "---\ntitle: brief\ncreated: 2026-11-11\ntype: source\nstage: import\n"
            "source: https://example.com\nauthor: Research contributors\ncredibility: practitioner\n"
            "captured_via: agent-deep-research\ntags: [source/adapted]\nrelated:\n  - \"[[Research Hub]]\"\n---\n\n"
            "# Example protocol\n\nBrief text.\n"
        )
        (root / "qa" / "plain-brief-review.json").write_text('{"status": "PASS", "verdict": "PASS", "editor_verdict": "PASS"}\n')
        return {"rc": 0, "log": str(log)}

    def completion(root, log, timeout):
        mechanics["completion"] += 1
        return {"rc": 2, "verdict": "HOLD", "classes": ["memo_depth"], "verdict_line": "HOLD (memo_depth)"}

    def export(root, mode, reason, run_id):
        mechanics["export"] += 1
        return {"status": "dry_run", "mode": mode, "reason": reason}

    monkeypatch.setattr(rc, "run_tool_preflight", preflight)
    monkeypatch.setattr(rc, "run_owned_assets", owned)
    monkeypatch.setattr(rc, "run_plain_brief", plain_brief)
    monkeypatch.setattr(rc, "run_completion", completion)
    monkeypatch.setattr(rc, "run_export", export)
    monkeypatch.setattr(rc, "run_verifier", lambda root: {"status": "HOLD", "holds": [], "failures": []})
    import campaign_controller
    monkeypatch.setattr(campaign_controller, "record_plain_brief", lambda root, manifest=None: {"status": "complete"})

    def start(plan=None, *extra):
        monkeypatch.setenv("FAKE_PLAN", json.dumps(plan or {}))
        return rc.main(["start", "--root", str(root), "--question", "What does the example protocol specify?",
                        "--target", "Example protocol", "--lanes", ",".join(LANES), "--date", "2026-11-11",
                        "--export", "dry-run", "--export-reason", "test campaign", "--hermes-bin", str(fake),
                        "--launcher", "direct", "--agent-log", str(agent_log), "--state-db", str(state_db),
                        "--wait-timeout", "120", "--wait-interval", "0.05", *extra])

    def resume(plan=None, *extra):
        monkeypatch.setenv("FAKE_PLAN", json.dumps(plan or {}))
        return rc.main(["resume", "--root", str(root), *extra])

    def calls():
        path = tmp_path / "fake" / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state():
        return json.loads((root / rc.STATE_REL).read_text())

    env = type("Env", (), {})()
    env.start, env.resume, env.calls, env.state, env.root, env.mechanics = start, resume, calls, state, root, mechanics
    return env


def test_full_run_phase_order_and_valid_state_segment(env, capsys):
    assert env.start() == 0
    st = env.state()
    assert st["status"] == "complete"
    assert [p for p in rc.PHASES if st["phases"][p]["status"] in ("complete", "hold")] == list(rc.PHASES)
    names = [c["name"] for c in env.calls()]
    lane_calls = [n for n in names if n.startswith("lane-")]
    assert sorted(lane_calls) == sorted(f"lane-{l}" for l in LANES)
    order = [n.split("-")[0] for n in names]
    # lanes first, then synthesis, then the single pre-brief model review (mechanical checks run in Python)
    assert order.index("synthesis") > max(i for i, n in enumerate(order) if n == "lane")
    assert names[-1] == "review-evidence_privacy-r1"
    assert names.index("review-evidence_privacy-r1") > names.index("synthesis")
    for mech_name in ("context-handoff-review.json", "taxonomy-review.json",
                      "librarian-repair-receipt.json", "visual-review.json",
                      "final-assurance.json", "mechanical-checks.json"):
        assert json.loads((env.root / "qa" / mech_name).read_text())["status"] == "PASS"
    assert env.mechanics == {"plain_brief": 1, "completion": 1, "export": 1}
    assert st["completion"]["verdict_line"] == "HOLD (memo_depth)"
    assert st["phases"]["completion"]["status"] == "hold"
    # the run segment replays through run_state_machine and ends on the completion verdict
    seg = st["runs"][-1]["transitions"]
    state = rsm.RunState()
    for t in seg:
        state = state.append(rsm.Transition(**t))
    assert seg[0]["to_state"] == "accepted" and state.current_state() == "blocked_review_hold"
    # every model phase is a fresh one-shot session with its packet, budget and minimal toolsets; never --source
    for call in env.calls():
        argv = call["argv"]
        assert "--source" not in argv and argv[:5] == ["chat", "--oneshot", "-Q", "--ignore-rules", "--max-turns"]
        assert Path(argv[argv.index("--query-file") + 1]).is_absolute()
        if call["name"].startswith("lane-"):
            assert argv[argv.index("-t") + 1] == "web,terminal,file" and argv[5] == "120"
        elif call["name"] == "synthesis":
            assert argv[5] == "150" and "web" not in argv[argv.index("-t") + 1]
        else:
            assert argv[5] == "80"
    # lanes reached campaign_state and the context release came after them
    cs = json.loads((env.root / "campaign_state.json").read_text())
    assert all(l["status"] == "completed" for l in cs["lanes"]) and cs["context_release"]["released_at"]
    assert [l["id"] for l in cs["lanes"]] == LANES + ["08-controller-synthesis-actionregister",
                                                     "09-board-brief-verification"]
    assert (env.root / "workers" / "save_source.py").is_file() and (env.root / "receipts" / "worker-read-audit.json").is_file()
    packet = (env.root / "workers" / f"{LANES[0]}-packet.md").read_text()
    assert "skill_view" not in packet and "references/" not in packet and str(env.root) in packet
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert "complete" in line and "verdict=HOLD (memo_depth)" in line


def test_rerun_skips_phases_whose_hashes_match(env):
    assert env.start() == 0
    before = len(env.calls())
    assert env.resume() == 0
    st = env.state()
    assert len(env.calls()) == before
    assert env.mechanics["completion"] == 1 and env.mechanics["plain_brief"] == 1
    assert all(st["phases"][p].get("last_skip") for p in rc.PHASES)
    assert st["runs"][-1]["resume_plan"]["outcome"] == "reuse_valid_stages_without_duplicate_output"


def test_changed_lane_output_reruns_that_lane_and_downstream(env):
    assert env.start() == 0
    before = env.calls()
    lane_file = env.root / "lanes" / f"{LANES[1]}.json"
    data = json.loads(lane_file.read_text())
    data["summary"] = "edited outside the driver"
    lane_file.write_text(json.dumps(data))
    assert env.resume() == 0
    new = [c["name"] for c in env.calls()[len(before):]]
    assert [n for n in new if n.startswith("lane-")] == [f"lane-{LANES[1]}"]
    assert "synthesis" in new and env.mechanics["completion"] == 2
    st = env.state()
    assert st["phases"]["setup"]["last_skip"] and st["phases"]["lanes"]["run_reason"].startswith("output changed")
    assert st["runs"][-1]["resume_plan"]["outcome"] == "repair_required"


def test_step_stop_is_not_resumed_and_stops_the_run(env):
    assert env.start({"synthesis": ["steps"]}) == 1
    syn = [c for c in env.calls() if c["name"] == "synthesis"]
    assert [c["kind"] for c in syn] == ["start"]
    st = env.state()
    assert st["status"] == "failed" and st["current_phase"] == "synthesis"
    assert st["phases"]["synthesis"]["status"] == "failed"
    assert "step_limit_reached" in st["reason"]
    assert st["runs"][-1]["transitions"][-1]["to_state"] == "blocked_exhausted_retry"
    assert not any(c["name"].startswith("review-") for c in env.calls())
    assert st["phases"].get("qa_reviews", {}).get("status", "pending") == "pending"
    assert json.loads((env.root / rc.DONE_REL).read_text())["status"] == "failed"


def test_failed_lane_stops_the_run_and_resume_relaunches_only_that_lane(env):
    assert env.start({f"lane-{LANES[2]}": ["fail", "ok"]}) == 1
    st = env.state()
    assert st["status"] == "failed" and st["current_phase"] == "lanes"
    assert LANES[2] in st["reason"] and "synthesis" not in [c["name"] for c in env.calls()]
    assert st["runs"][-1]["transitions"][-1]["to_state"] == "blocked_missing_evidence"
    before = len(env.calls())
    assert env.resume({f"lane-{LANES[2]}": ["fail", "ok"]}) == 0
    new = [c["name"] for c in env.calls()[before:]]
    assert [n for n in new if n.startswith("lane-")] == [f"lane-{LANES[2]}"]
    assert env.state()["status"] == "complete"
    assert env.state()["runs"][-1]["retry_of"] == "run-1"


HOLDS = {f"review-evidence_privacy-r{i}": ["hold"] for i in range(1, 9)}


def test_review_hold_runs_bounded_repairs_then_continues(env):
    assert env.start(HOLDS) == 0
    names = [c["name"] for c in env.calls()]
    assert [n for n in names if n.startswith("repair-")] == ["repair-r1", "repair-r2", "repair-r3"]
    st = env.state()
    assert st["phases"]["repairs"]["status"] == "hold" and len(st["phases"]["repairs"]["detail"]["rounds"]) == 3
    assert (env.root / "receipts" / "qa-history" / "repair-round-1" / "evidence-privacy-review.json").is_file()
    assert st["phases"]["completion"]["status"] == "hold"
    # the bound holds across runs, also for a state saved before rounds were counted separately
    raw = env.state()
    raw.pop("repair_rounds_done"), raw.pop("repair_history")  # older states kept rounds only in the phase detail
    (env.root / rc.STATE_REL).write_text(json.dumps(raw))
    before = len(env.calls())
    assert env.resume(HOLDS, "--from", "repairs") == 0
    assert not [c for c in env.calls()[before:] if c["name"].startswith("repair-")]
    # a new synthesis is a new pack, so it gets its own bound; round numbers and records never repeat
    lane_file = env.root / "lanes" / f"{LANES[0]}.json"
    lane_file.write_text(lane_file.read_text().replace("fake lane run 1", "edited"))
    before = len(env.calls())
    assert env.resume(HOLDS) == 0
    new = [c["name"] for c in env.calls()[before:]]
    assert "synthesis" in new and [n for n in new if n.startswith("repair-")] == ["repair-r4", "repair-r5", "repair-r6"]
    assert list((env.root / "orchestration" / "history").glob("*-synthesis/synthesis.json"))


def test_refreshed_config_allows_one_more_numbered_round(env, tmp_path):
    import shutil
    assert env.start(HOLDS) == 0
    config = tmp_path / "config"
    shutil.copytree(rc.SKILL / "config", config)
    text = (config / "phases.yaml").read_text()
    (config / "phases.yaml").write_text(re_sub_rounds(text, 4))
    before = len(env.calls())
    assert env.resume(HOLDS, "--from", "repairs", "--refresh-config", "--config-dir", str(config)) == 0
    new = [c["name"] for c in env.calls()[before:]]
    assert [n for n in new if n.startswith("repair-")] == ["repair-r4"] and "review-evidence_privacy-r5" in new
    st = env.state()
    assert st["config"]["repair_rounds"] == 4 and st["config_history"][0]["previous"]["repair_rounds"] == 3


def re_sub_rounds(text, n):
    import re
    return re.sub(r"repair_rounds: \d+", f"repair_rounds: {n}", text)


def test_plain_brief_gate_refusal_is_carried_to_the_completion_check(env, monkeypatch):
    def refused(root, log, timeout):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("reference pack review not accepted: reference_pack_review_not_pass_or_stale\n")
        return {"rc": 1, "log": str(log)}

    monkeypatch.setattr(rc, "run_plain_brief", refused)
    assert env.start() == 0
    st = env.state()
    assert st["phases"]["plain_brief"]["status"] == "hold"
    assert "not accepted" in st["phases"]["plain_brief"]["detail"]["gate_refusal"]
    assert st["phases"]["final_reviews"]["status"] == "hold" and st["phases"]["final_reviews"]["detail"]["skipped"]
    assert not [c for c in env.calls() if c["name"] in ("review-visual", "review-final_assurance")]
    assert env.mechanics["completion"] == 1 and st["status"] == "complete"


def test_plain_brief_crash_stops_the_run(env, monkeypatch):
    def crashed(root, log, timeout):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("Traceback (most recent call last):\n  boom\n")
        return {"rc": 1, "log": str(log)}

    monkeypatch.setattr(rc, "run_plain_brief", crashed)
    assert env.start() == 1
    st = env.state()
    assert st["status"] == "failed" and st["current_phase"] == "plain_brief" and env.mechanics["completion"] == 0


def test_status_line_and_json(env, capsys):
    assert env.start() == 0
    capsys.readouterr()
    assert rc.main(["status", "--root", str(env.root)]) == 0
    line = capsys.readouterr().out.strip()
    assert "\n" not in line and line.startswith("example-protocol-2026-11-11: complete")
    assert rc.main(["status", "--root", str(env.root), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["phases"]["completion"] == "hold" and len(data["sessions"]) == 5


def test_start_refuses_existing_state_and_missing_export_reason(env, tmp_path):
    assert env.start() == 0
    assert env.start() == 2
    other = tmp_path / "other"
    assert rc.main(["start", "--root", str(other), "--question", "q", "--target", "t", "--lanes", LANES[0],
                    "--export", "skip"]) == 2


def test_phase_config_budgets_models_and_toolsets():
    import research_settings as rs

    cfg = rs.load_phases()
    budgets = {k: v["budget"] for k, v in cfg["phases"].items()}
    assert budgets == {"lanes": 120, "synthesis": 150, "review": 80, "final_review": 80, "repair": 80}
    assert cfg["phases"]["lanes"]["toolsets"] == ["web", "terminal", "file"]
    producers = {cfg["phases"][k]["model"] for k in ("lanes", "synthesis", "repair")}
    for kind in ("review", "final_review"):
        assert cfg["phases"][kind]["model"] not in producers


def test_lane_toolsets_cannot_include_skills_or_code(tmp_path):
    import shutil
    import research_settings as rs

    config = tmp_path / "config"
    shutil.copytree(Path(rs.__file__).resolve().parent.parent / "config", config)
    text = (config / "phases.yaml").read_text()
    (config / "phases.yaml").write_text(text.replace("toolsets: [web, terminal, file]",
                                                     "toolsets: [web, terminal, file, code_execution]", 1))
    with pytest.raises(ValueError, match="public lanes"):
        rs.load_phases(config_dir=config)


def test_run_completion_reads_the_printed_json_status(tmp_path, monkeypatch):
    fake = tmp_path / "scripts"
    fake.mkdir()
    (fake / "complete_campaign.py").write_text(
        "import json, sys\nprint(json.dumps({'status': 'FAIL', 'classes': ['campaign_not_ready', 'receipt_unverified']},"
        " indent=2))\nsys.exit(3)\n")
    monkeypatch.setattr(rc, "SCRIPTS", fake)
    res = rc.run_completion(tmp_path / "root", tmp_path / "completion.log", 60)
    assert res["rc"] == 3 and res["verdict"] == "FAIL"
    assert res["verdict_line"] == "FAIL (campaign_not_ready, receipt_unverified)"
    assert '"status": "FAIL"' in (tmp_path / "completion.log").read_text()
