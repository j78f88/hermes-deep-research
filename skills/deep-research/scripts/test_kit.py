"""Tests for the promoted per-campaign scripts in scripts/kit/. Offline."""
import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent
KIT = SCRIPTS / "kit"
sys.path.insert(0, str(SCRIPTS / "kit"))

import campaign_setup  # noqa: E402
import check_my_lane  # noqa: E402
import closeout  # noqa: E402
import context_edit  # noqa: E402
import fan_in  # noqa: E402
import gh_get  # noqa: E402
import mechanical_qa  # noqa: E402
import phase_runner  # noqa: E402
import read_audit  # noqa: E402
import save_source  # noqa: E402
import units  # noqa: E402


def make_lane(root: Path, lane: str, quote: str, text: str = "Alpha beta gamma delta epsilon zeta eta theta iota.") -> None:
    (root / "workers").mkdir(parents=True, exist_ok=True)
    campaign_setup.install_worker_kit(root)
    src = root / "workspace" / "sources" / lane
    src.mkdir(parents=True)
    (src / "S01.md").write_text(text)
    row = {"file": "S01.md", "source_id": "S01", "sha256": hashlib.sha256((src / "S01.md").read_bytes()).hexdigest()}
    (src / "index.jsonl").write_text(json.dumps(row) + "\n")
    (root / "lanes").mkdir(exist_ok=True)
    lane_json = {
        "schema_version": "deep-research-lane-output-v1",
        "campaign": {"target": "t", "target_type": "technical_idea", "decision_question": "q"},
        "lane": {"id": lane, "name": lane, "type": "steering", "objective": "o", "artifact_path": f"lanes/{lane}.md"},
        "status": "pass", "summary": "s",
        "sources": [{"id": "S01", "title": "T", "url_or_ref": "https://example.com", "source_class": "official",
                     "accessed_or_checked": "2026-11-11"}],
        "findings": [{"id": "F01", "claim": "c", "evidence_class": "reported", "confidence": "high",
                      "source_ids": ["S01"], "source_file": "S01.md", "quote": quote},
                     {"id": "F02", "claim": "judgement", "evidence_class": "analyst_inference", "confidence": "low",
                      "source_ids": []}],
        "gaps": [], "contradictions": [], "action_register_candidates": [], "visual_implications": [],
        "verification": {"checks_run": [], "forbidden_actions_absent": [], "open_verification_gaps": []},
        "acquisition": {"source_type": "web_page", "method": "m", "provenance": "live", "coverage": "full_text_captured",
                        "degraded_state": ["none"]},
        "coverage": [],
    }
    (root / "lanes" / f"{lane}.json").write_text(json.dumps(lane_json))


def test_check_my_lane_passes_verbatim_quote_and_rejects_others(tmp_path):
    make_lane(tmp_path, "01-a", "gamma delta epsilon zeta")
    result = check_my_lane.check_lane(tmp_path, "01-a")
    assert result["ok"], result
    assert result["quoted"] == 1 and result["inference_or_gap"] == 1
    data = json.loads((tmp_path / "lanes" / "01-a.json").read_text())
    data["findings"][0]["quote"] = "words that are not in the saved file"
    (tmp_path / "lanes" / "01-a.json").write_text(json.dumps(data))
    assert check_my_lane.check_lane(tmp_path, "01-a")["quote_problems"][0]["kind"] == "quote_not_in_source"
    data["findings"][0].update(quote="gamma delta epsilon zeta", source_file="../02-b/S01.md")
    (tmp_path / "lanes" / "01-a.json").write_text(json.dumps(data))
    assert check_my_lane.check_lane(tmp_path, "01-a")["quote_problems"][0]["kind"] == "source_outside_lane"


def test_check_my_lane_detects_changed_source_and_uncited_fact(tmp_path):
    make_lane(tmp_path, "01-a", "gamma delta epsilon zeta")
    (tmp_path / "workspace/sources/01-a/S01.md").write_text("Alpha beta gamma delta epsilon zeta, edited.")
    data = json.loads((tmp_path / "lanes" / "01-a.json").read_text())
    data["findings"][1]["evidence_class"] = "reported"
    (tmp_path / "lanes" / "01-a.json").write_text(json.dumps(data))
    kinds = {p["kind"] for p in check_my_lane.check_lane(tmp_path, "01-a")["quote_problems"]}
    assert kinds == {"source_hash_mismatch", "uncited_fact"}


def test_worker_kit_copy_runs_from_campaign_root(tmp_path):
    make_lane(tmp_path, "01-a", "gamma delta epsilon zeta")
    proc = subprocess.run([sys.executable, str(tmp_path / "workers" / "check_my_lane.py"), "01-a"],
                          capture_output=True, text=True)
    assert proc.returncode == 0 and json.loads(proc.stdout)["ok"]
    schema = json.loads((tmp_path / "workers" / "lane-output-schema.json").read_text())
    assert schema["properties"]["coverage"] == {"type": "array"}
    bad = subprocess.run([sys.executable, str(tmp_path / "workers" / "check_my_lane.py"), "../x"],
                         capture_output=True, text=True)
    assert bad.returncode == 2


def test_gh_get_refuses_private_account_paths():
    assert gh_get.path_problem("repos/ag-ui-protocol/ag-ui", "someone") is None
    assert gh_get.path_problem("search/code?q=repo:a/b+x&per_page=100", None) is None
    for path in ("user/repos", "notifications", "gists", "orgs/acme/members", "repos/a/b;rm", "graphql",
                 "repos/a/b $(x)"):
        assert gh_get.path_problem(path, None), path
    assert gh_get.path_problem("users/someone/repos", "someone")
    assert gh_get.path_problem("repos/someone/private-thing", "someone")
    for path in ("repos/../user", "repos/x/../../notifications", "repos/a/./b"):
        assert gh_get.path_problem(path, None), path


def test_gh_get_reads_repo_paths_only_after_a_public_visibility_check(monkeypatch, capsys):
    calls = []

    def fake(path):
        calls.append(path)
        body = json.dumps({"private": True}) if path == "repos/acme/secret" else json.dumps({"private": False})
        return subprocess.CompletedProcess(["gh"], 0, stdout=body, stderr="")

    monkeypatch.setattr(gh_get, "gh_get", fake)
    monkeypatch.setattr(gh_get, "own_login", lambda: None)
    assert gh_get.main(["L1", "S1", "repos/acme/secret/contents/README.md"]) == 2
    assert "private" in capsys.readouterr().out
    assert calls == ["repos/acme/secret"]

    def missing(path):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(gh_get, "gh_get", missing)
    assert gh_get.main(["L1", "S1", "search/code?q=x"]) == 1
    assert "gh api failed" in capsys.readouterr().out


def test_save_source_writes_indexed_copy_and_refuses_reuse(tmp_path):
    row = save_source.save(tmp_path, "01-a", "S01", "https://example.com", "direct", "x" * 100)
    saved = tmp_path / "workspace/sources/01-a/S01.md"
    assert row["sha256"] == hashlib.sha256(saved.read_bytes()).hexdigest() and row["file"] == "S01.md"
    assert json.loads((tmp_path / "workspace/sources/01-a/index.jsonl").read_text())["sha256"] == row["sha256"]
    with pytest.raises(FileExistsError):
        save_source.save(tmp_path, "01-a", "S01", "https://example.com", "direct", "y" * 100)
    assert save_source.main(["../x", "S01", "https://example.com"]) == 2
    assert save_source.main(["01-a", "S02", "file:///etc/passwd"]) == 2


def test_lane_codes_keep_numbers_and_split_shared_ones():
    codes = fan_in.lane_codes(["01-steering", "06-adversarial", "06-verification", "07-fit"])
    assert codes == {"01-steering": "L01", "06-adversarial": "L06A", "06-verification": "L06B", "07-fit": "L07"}


def test_build_citations_from_lane_sources(tmp_path):
    make_lane(tmp_path, "01-a", "gamma delta epsilon zeta")
    data = fan_in.build_citations(tmp_path, ["01-a"])
    row = data["sources"][0]
    assert row["code"] == "L01-S01" and row["saved_file"] == "workspace/sources/01-a/S01.md"
    assert len(row["saved_sha256"]) == 64 and "sha256" not in row


def test_context_edit_backs_up_and_logs_once(tmp_path):
    (tmp_path / "context").mkdir()
    (tmp_path / "context" / "c.json").write_text('{"a": 1}\n')
    entry = context_edit.logged_json_edit(tmp_path, "context/c.json", lambda d: d.update(a=2), "test")
    assert entry["before_sha256"] != entry["after_sha256"] and (tmp_path / entry["backup"]).read_text() == '{"a": 1}\n'
    assert context_edit.logged_json_edit(tmp_path, "context/c.json", lambda d: d.update(a=2), "test") is None
    assert len((tmp_path / "context" / "edit-log.jsonl").read_text().splitlines()) == 1


def test_systemd_command_carries_path_and_home(monkeypatch):
    monkeypatch.setenv("PATH", "/a:/b")
    monkeypatch.setenv("HOME", "/var/empty/x")
    cmd = units.systemd_command("u1", ["python3", "x.py"], {"TMPDIR": "/t"})
    assert cmd[:4] == ["systemd-run", "--user", "--unit=u1", "--collect"]
    assert "--setenv=PATH=/a:/b" in cmd and "--setenv=HOME=/var/empty/x" in cmd and "--setenv=TMPDIR=/t" in cmd
    assert cmd[-2:] == ["python3", "x.py"]


def make_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.execute("create table sessions (id text, parent_session_id text, model text, tool_call_count int, "
                "actual_cost_usd real, estimated_cost_usd real)")
    con.execute("create table messages (session_id text, tool_calls text)")
    con.executemany("insert into sessions values (?,?,?,?,?,?)",
                    [("s1", None, "m/one", 7, None, 0.5), ("c1", "s1", "m/one", 3, 0.25, None), ("s2", None, "m", 1, 0, 0)])
    calls = [{"function": {"name": "skill_view", "arguments": "{}"}},
             {"function": {"name": "read_file", "arguments": json.dumps({"path": "/r/context/context-brief.md"})}},
             {"function": {"name": "read_file", "arguments": json.dumps({"path": "lanes/02-b.json"})}}]
    con.execute("insert into messages values (?, ?)", ("c1", json.dumps(calls)))
    con.commit()
    con.close()


def test_session_tool_calls_include_child_sessions(tmp_path):
    db = tmp_path / "state.db"
    make_db(db)
    out = units.session_tool_calls("s1", db)
    assert out["tool_calls"] == 10 and out["model"] == "m/one" and out["cost_usd"] == 0.75
    assert units.session_tool_calls(None, db)["tool_calls"] is None
    assert units.session_tool_calls("s1", tmp_path / "missing.db")["tool_calls"] is None


def test_read_audit_counts_hits_without_bodies(tmp_path):
    db = tmp_path / "state.db"
    make_db(db)
    data = read_audit.audit(Path("/r"), {"01-a": "s1"}, {"01-a"}, db, ["01-a", "02-b"])
    hits = data["sessions"][0]["pattern_hits"]
    assert hits["skills_tool"] == 1 and hits["context_brief"] == 1 and hits["other_lane_files"] == 1
    assert data["sessions"][0]["sessions_scanned"] == ["s1", "c1"]


def pre_brief_root(tmp_path: Path, audit: dict | None) -> Path:
    """Minimal campaign for check_pre_brief: two research lanes plus a controller lane, released context."""
    root = tmp_path / "campaign"
    (root / "context").mkdir(parents=True)
    (root / "receipts").mkdir()
    lanes = [{"id": lane, "status": "completed"} for lane in ("01-a", "02-b", "08-controller-synthesis-actionregister")]
    (root / "campaign_state.json").write_text(json.dumps(
        {"lanes": lanes, "context_release": {"released_at": "2026-11-11T00:00:00+00:00"}}))
    (root / "context" / "context-brief.json").write_text(json.dumps(
        {"schema_version": "deep-research-context-brief-v1", "status": "complete", "items": [],
         "no_relevant_context_reason": "external only"}))
    if audit is not None:
        (root / "receipts" / "worker-read-audit.json").write_text(json.dumps(audit))
    return root


def clean_audit(*workers: str) -> dict:
    return {"schema_version": "worker-read-audit-v1",
            "sessions": [{"worker": w, "session_id": f"s-{w}", "public_lane": True, "pattern_hits": {}} for w in workers]}


def test_pre_brief_isolation_passes_with_a_clean_audit_for_every_research_lane(tmp_path):
    out = mechanical_qa.check_pre_brief(pre_brief_root(tmp_path, clean_audit("01-a", "02-b")), ["p"], "c")
    assert out["checks"]["isolation"] == "PASS" and out["status"] == "PASS"


def test_pre_brief_isolation_fails_closed_when_the_read_audit_is_missing(tmp_path):
    out = mechanical_qa.check_pre_brief(pre_brief_root(tmp_path, None), ["p"], "c")
    assert out["checks"]["isolation"] == "HOLD" and out["status"] == "HOLD"
    assert any("missing" in r for r in out["requested_repairs"])


def test_pre_brief_isolation_fails_closed_when_the_read_audit_holds_an_error(tmp_path):
    audit = {"schema_version": "worker-read-audit-v1", "error": "state db unavailable: x", "sessions": []}
    out = mechanical_qa.check_pre_brief(pre_brief_root(tmp_path, audit), ["p"], "c")
    assert out["checks"]["isolation"] == "HOLD" and out["status"] == "HOLD"
    assert any("state db unavailable" in r for r in out["requested_repairs"])


def test_pre_brief_isolation_fails_closed_when_a_lane_has_no_audit_row(tmp_path):
    out = mechanical_qa.check_pre_brief(pre_brief_root(tmp_path, clean_audit("01-a")), ["p"], "c")
    assert out["checks"]["isolation"] == "HOLD" and out["status"] == "HOLD"
    assert any("02-b" in r and "no public lane row" in r for r in out["requested_repairs"])
    # a row that is not marked as a public lane does not count either
    audit = clean_audit("01-a", "02-b")
    audit["sessions"][1]["public_lane"] = False
    out = mechanical_qa.check_pre_brief(pre_brief_root(tmp_path / "x", audit), ["p"], "c")
    assert out["checks"]["isolation"] == "HOLD"


def test_pre_brief_isolation_names_the_worker_with_a_read_hit(tmp_path):
    audit = clean_audit("01-a", "02-b")
    audit["sessions"][1]["pattern_hits"] = {"session_db": 2}
    out = mechanical_qa.check_pre_brief(pre_brief_root(tmp_path, audit), ["p"], "c")
    assert out["checks"]["isolation"] == "HOLD"
    assert "Lane 02-b had 2 session_db read hit(s) in worker-read-audit.json" in out["requested_repairs"]


def test_pre_brief_isolation_holds_on_an_orchestration_read(tmp_path):
    audit = clean_audit("01-a", "02-b")
    audit["sessions"][0]["pattern_hits"] = {"orchestration_dir": 1}
    out = mechanical_qa.check_pre_brief(pre_brief_root(tmp_path, audit), ["p"], "c")
    assert out["checks"]["isolation"] == "HOLD"
    assert "Lane 01-a had 1 orchestration_dir read hit(s) in worker-read-audit.json" in out["requested_repairs"]


def test_read_audit_orchestration_pattern_matches_campaign_paths_not_public_urls():
    pat = read_audit.patterns(Path("/w/dr-x"))["orchestration_dir"]
    for hit in ('{"path": "/w/dr-x/orchestration/logs/a.log"}', '{"path": "orchestration/driver.done"}',
                '{"command": "cat ./orchestration/x"}', '{"command": "ls ../orchestration/"}'):
        assert pat.search(hit), hit
    for miss in ('{"url": "https://kubernetes.io/docs/concepts/orchestration/"}',
                 '{"command": "python3 /w/dr-x/workers/save_source.py 01-a S01 \'https://x.io/container-orchestration/\'"}'):
        assert not pat.search(miss), miss


def test_archive_copies_and_verifies_every_file(tmp_path):
    src = tmp_path / "root"
    (src / "a").mkdir(parents=True)
    (src / "a" / "f.txt").write_text("one")
    (src / "g.json").write_text("{}")
    dest = tmp_path / "nas" / "2026-11" / "run"
    dry = closeout.archive(src, dest, dry_run=True)
    assert dry["status"] == "dry_run" and not dest.exists()
    live = closeout.archive(src, dest, dry_run=False)
    assert live["status"] == "verified" and live["files"] == 2 and (dest / "a" / "f.txt").read_text() == "one"
    assert "-rlt" in live["rsync"]["command"]


def test_export_skip_records_reason(tmp_path):
    rec = closeout.export(tmp_path, "skip", "test campaign", "run")
    assert rec["status"] == "skipped" and rec["reason"] == "test campaign"


def test_phase_runner_command_and_marker_on_bad_arguments(tmp_path):
    cmd = phase_runner.build_command(hermes="/h", packet=Path("/p.md"), root=Path("/r"), budget=80, toolsets="terminal,file",
                                     provider="openrouter", model="m/x", reasoning="high", run_budget=60)
    assert cmd[:7] == ["/h", "chat", "--oneshot", "-Q", "--ignore-rules", "--max-turns", "80"]
    assert "--source" not in cmd and cmd[cmd.index("--query-file") + 1] == "/p.md"
    marker = tmp_path / "m.done"
    assert phase_runner.main(["--marker", str(marker), "--name", "x"]) == 2
    assert json.loads(marker.read_text())["exit_code"] == 2


def test_keywords_drop_generic_words():
    assert campaign_setup.keywords("AG-UI protocol") == ["ag-ui", "ag-ui protocol"]


def test_prune_lanes_keeps_chosen_lanes_everywhere(tmp_path):
    root = tmp_path / "c"
    subprocess.run([sys.executable, str(SCRIPTS / "init_campaign.py"), "--root", str(root), "--target", "X",
                    "--target-type", "technical_idea", "--decision-question", "What is X?"], check=True,
                   capture_output=True)
    keep = ["01-steering-source-map", "02-current-state-truth"]
    rec = campaign_setup.prune_lanes(root, keep)
    assert "03-evidence-claims-map" in rec["dropped"]
    manifest = json.loads((root / "manifest.json").read_text())
    ids = [lane["id"] for lane in manifest["lanes"]]
    assert ids == keep + list(campaign_setup.CONTROLLER_LANES)
    state = json.loads((root / "campaign_state.json").read_text())
    assert [lane["id"] for lane in state["lanes"]] == ids
    assert not (root / "lanes" / "03-evidence-claims-map.json").exists()
    assert {Path(p).name for p in (root / "workers").glob("*-packet.md")} == {f"{i}-packet.md" for i in ids}
    assert campaign_setup.prune_lanes(root, keep)["status"] == "reused"
    with pytest.raises(ValueError):
        campaign_setup.prune_lanes(root, ["99-nope"])
    controls = campaign_setup.apply_factual_controls(root, "factual", export_mode="dry-run", export_reason="test")
    manifest = json.loads((root / "manifest.json").read_text())
    assert "pre_seed" not in manifest and manifest["non_applicable_controls"]["pre_seed"]
    assert "research_orchestration" not in manifest and controls["changes"]
    assert manifest["deliverable_exports"]["enabled"] is False
    assert manifest["deliverable_exports"]["disabled_reason"] == "test"
    campaign_setup.apply_history_mode(root, "external-only", "public lanes only")
    assert json.loads((root / "history" / "relevant-history.json").read_text())["status"] == "skipped_external_only"
    with pytest.raises(ValueError):
        campaign_setup.apply_factual_controls(root, "decision_assessment")
