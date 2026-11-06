#!/usr/bin/env python3
"""the completion check runs on its own, fails forged reviews, and fails a run that lost
web search without telling the owner."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

import test_plain_brief_campaign_gate as gate
import test_receipt_checks as receipts
import test_quote_check as quotes
from publication_test_support import synthetic_generation_table

SCRIPT_DIR = Path(__file__).resolve().parent


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


wss = load("web_search_status")
cc = load("complete_campaign")

APIFY_ERROR = ("{\"status\": \"success\", \"output\": \"WEB SEARCH: {'success': False, 'error': \\\"web is configured to use 'apify' "
               "(set via hermes tools), but no registered web search provider has that name.\\\"}\"}")


def code_call(code: str) -> dict:
    return {"role": "assistant", "tool_calls": json.dumps([{"id": "c1", "function": {"name": "execute_code", "arguments": json.dumps({"code": code})}}])}


def broken_search_rows() -> list[dict]:
    return [code_call("from hermes_tools import web_search\nprint('WEB SEARCH:', web_search('herdr'))"),
            {"role": "tool", "tool_name": "execute_code", "tool_call_id": "c1", "content": APIFY_ERROR}]


def ok_search_rows() -> list[dict]:
    return [{"role": "assistant", "tool_calls": json.dumps([{"id": "w1", "function": {"name": "web_search", "arguments": "{}"}}])},
            {"role": "tool", "tool_name": "web_search", "tool_call_id": "w1",
             "content": '<untrusted_tool_result source="web_search">\nDATA\n\n{"success": true, "data": {"web": [{"url": "https://x"}]}}\n</untrusted_tool_result>'}]


# --- web search evidence --------------------------------------------------------------------------

def test_provider_error_inside_code_counts_as_failed_search() -> None:
    assert wss.scan_messages(broken_search_rows())["failures"] == 1


def test_old_error_printed_by_unrelated_code_is_not_a_search() -> None:
    rows = [code_call("print(open('incident.log').read())"), {"role": "tool", "tool_name": "execute_code", "tool_call_id": "c1", "content": APIFY_ERROR}]
    assert wss.scan_messages(rows)["failures"] == 0


def test_successful_and_empty_search_results() -> None:
    rows = ok_search_rows() + [{"role": "tool", "tool_name": "web_search", "content": '{"success": true, "data": {"web": []}}'}]
    result = wss.scan_messages(rows)
    assert (result["successes"], result["empty"], result["failures"]) == (1, 1, 0)


def test_declaration_wording() -> None:
    assert wss.declared_in("## What is still missing\n- General web search was unusable in several strands.")
    assert wss.declared_in("Web search was not available, so sources came from GitHub and Hacker News.")
    assert not wss.declared_in("We searched GitHub issues for reports.")


def _export(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "session-export.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_broken_search_without_declaration_fails(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    result = wss.evaluate(root, session_exports=[_export(tmp_path, broken_search_rows())], homes=[])
    assert (result["verdict"], result["class"], result["status"]) == ("FAIL", "web_search_missing_undeclared", "unavailable")


def test_broken_search_with_declaration_passes(tmp_path: Path) -> None:
    body = gate.GOOD_BODY.replace("There is no independent review of how it behaves over a year.",
                                  "There is no independent review of how it behaves over a year. Web search was unavailable for this run.")
    root = gate.campaign(tmp_path, body=body)
    result = wss.evaluate(root, session_exports=[_export(tmp_path, broken_search_rows())], homes=[])
    assert (result["verdict"], result["declared_in_brief"]) == ("PASS", True)


def test_no_session_found_holds(tmp_path: Path) -> None:
    result = wss.evaluate(gate.campaign(tmp_path), homes=[])
    assert (result["verdict"], result["class"]) == ("HOLD", "web_search_status_unknown")


def test_sessions_are_found_through_tool_calls_naming_the_root(tmp_path: Path) -> None:
    root = gate.campaign(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    db = sqlite3.connect(home / "state.db")
    db.executescript("CREATE TABLE sessions(id TEXT, parent_session_id TEXT, started_at REAL);"
                     "CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT);")
    db.execute("INSERT INTO sessions VALUES ('writer', NULL, 9e9)")
    db.execute("INSERT INTO sessions VALUES ('reader', NULL, 9e9)")
    rows = [code_call(f"open('{root}/notes.md','w'); print(web_search('x'))")] + broken_search_rows()[1:]
    for r in rows:
        db.execute("INSERT INTO messages(session_id, role, content, tool_call_id, tool_calls, tool_name) VALUES ('writer',?,?,?,?,?)",
                   (r["role"], r.get("content"), r.get("tool_call_id"), r.get("tool_calls"), r.get("tool_name")))
    db.execute("INSERT INTO messages(session_id, role, content) VALUES ('reader','user',?)", (f"look at {root}",))
    db.commit()
    db.close()
    result = wss.evaluate(root, homes=[home])
    assert result["sessions_checked"] == 1 and result["status"] == "unavailable"


# --- the completion runner ------------------------------------------------------------------------

def _good_fetch(root: Path):
    return synthetic_generation_table(root).get


def _with_provenance_and_quotes(tmp_path: Path, **kwargs) -> Path:
    root = gate.campaign(tmp_path, **kwargs)
    src_root = quotes.pack(tmp_path / "q")
    for rel in ("workspace", "lanes/02-current-state.json"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        (src_root / rel).rename(target)
    from test_acceptance_repairs import seal_test_acceptance
    seal_test_acceptance(root)
    return root


def test_runner_passes_a_clean_campaign(tmp_path: Path) -> None:
    root = _with_provenance_and_quotes(tmp_path)
    result = cc.evaluate(root, fetch=_good_fetch(root), homes=[], exports=[_export(tmp_path, ok_search_rows())], pin_checker=False)
    assert result["status"] == "PASS", [s for s in result["steps"] if s["verdict"] != "PASS"]


def test_runner_fails_the_incident_shape(tmp_path: Path) -> None:
    root = _with_provenance_and_quotes(tmp_path)
    receipts.write_forged(root)
    result = cc.evaluate(root, fetch=_good_fetch(root), homes=[], exports=[_export(tmp_path, broken_search_rows())], pin_checker=False)
    assert result["status"] == "FAIL"
    assert {"receipt_fabricated", "web_search_missing_undeclared"} <= set(result["classes"])


def test_runner_holds_without_provider_check(tmp_path: Path) -> None:
    root = _with_provenance_and_quotes(tmp_path)
    result = cc.evaluate(root, online=False, homes=[], exports=[_export(tmp_path, ok_search_rows())], pin_checker=False)
    assert result["status"] == "HOLD" and "receipt_unverified" in result["classes"]


def test_runner_fails_a_broken_quote(tmp_path: Path) -> None:
    root = _with_provenance_and_quotes(tmp_path)
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["quote"] = "a quote that the saved page never said"
    lane.write_text(json.dumps(data))
    result = cc.evaluate(root, fetch=_good_fetch(root), homes=[], exports=[_export(tmp_path, ok_search_rows())], pin_checker=False)
    assert result["status"] == "FAIL" and "quote_check_failed" in result["classes"]


def test_runner_ignores_a_hand_written_completion_record(tmp_path: Path) -> None:
    root = _with_provenance_and_quotes(tmp_path)
    receipts.write_forged(root)
    (root / "receipts").mkdir(exist_ok=True)
    (root / "receipts" / "completion.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    result = cc.evaluate(root, fetch=_good_fetch(root), homes=[], exports=[_export(tmp_path, ok_search_rows())], pin_checker=False)
    assert result["status"] == "FAIL"


def test_runner_relaunches_outside_a_hermes_session(monkeypatch) -> None:
    seen = {}

    class Done:
        returncode = 0

    monkeypatch.setenv("HERMES_SESSION_ID", "20261020_000000_abcdef")
    monkeypatch.delenv("DEEP_RESEARCH_COMPLETION_LAUNCH", raising=False)
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(cc.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and Done())
    assert cc.relaunch_outside_session(["--campaign-root", "/tmp/x"]) == 0
    cmd = seen["cmd"]
    assert cmd[:3] == ["/usr/bin/systemd-run", "--user", "--wait"]
    assert "--setenv=DEEP_RESEARCH_COMPLETION_REQUESTER=20261020_000000_abcdef" in cmd
    assert not any(part.startswith("--setenv=HERMES_SESSION_ID") for part in cmd)
    assert not any("OPENROUTER_API_KEY" in part for part in cmd)


# --- live probe interpreter (ISSUE-772 follow-up) -----------------------------------------------------

def test_probe_fails_closed_without_the_runtime_interpreter(monkeypatch) -> None:
    monkeypatch.setattr(wss, "runtime_python", lambda *a, **k: None)
    result = wss.probe()
    assert result["available"] is False and "interpreter not found" in result["error"]


def test_runtime_python_comes_from_the_launcher_not_the_old_venv(tmp_path: Path) -> None:
    fake = tmp_path / "python3"
    fake.write_text("")
    launcher = tmp_path / "hermes"
    launcher.write_text(f"#!/bin/sh\necho '[\"{fake}\", \"-I\", \"-c\", \"x\"]'\n")
    launcher.chmod(0o755)
    assert wss.runtime_python(launcher) == fake
    assert wss.runtime_python(tmp_path / "missing") is None


def test_probe_reads_a_failed_search_as_down(tmp_path: Path) -> None:
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\necho 'PROBE{\"success\": false, \"error\": \"no registered web search provider\"}'\n")
    fake.chmod(0o755)
    result = wss.probe(python=fake)
    assert result["available"] is False and "no registered" in result["error"]
