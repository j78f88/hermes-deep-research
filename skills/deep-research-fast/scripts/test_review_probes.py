"""Round 1 reviewer probes, inverted to assert the round 2 fixes."""
import json
import re
import subprocess
import sys

import pytest

import fact_grades as fg
import fast_worker as fw
from fast_research import FastResearch
from fast_quote_check import check_answer_files
from fast_test_support import FakeHermes, small_config
from test_fast_sentence_check import prepared, SEARCH
from test_deepest_research import QUESTIONS, SEARCH as DEEP_SEARCH


def test_deepest_does_not_attach_to_open_fast_run(tmp_path):
    from deepest_research import DeepestResearch
    fake = FakeHermes(SEARCH)
    fast = FastResearch(tmp_path, "Same question?", config=small_config(), **fake.hooks())
    fast.store.set_run(fast.run_id, status="failed")
    deep = DeepestResearch(tmp_path, fast.question, config=small_config(), **fake.hooks())
    assert deep.run_id != fast.run_id
    assert fast.run_id.startswith("fast-") and deep.run_id.startswith("deepest-")
    assert DeepestResearch(tmp_path, deep.question, config=small_config(), **fake.hooks()).run_id == deep.run_id


def test_fast_does_not_attach_to_open_deepest_run(tmp_path):
    from deepest_research import DeepestResearch
    fake = FakeHermes(SEARCH)
    deep = DeepestResearch(tmp_path, "Same question?", config=small_config(), **fake.hooks())
    deep.store.set_run(deep.run_id, status="failed")
    fast = FastResearch(tmp_path, deep.question, config=small_config(), **fake.hooks())
    assert fast.run_id != deep.run_id
    assert FastResearch(tmp_path, fast.question, config=small_config(), **fake.hooks()).run_id == fast.run_id


@pytest.mark.parametrize("items", [None, "not a list", 7, {"n": 1}])
def test_review_non_list_does_not_stop_run(tmp_path, items):
    fake = FakeHermes(SEARCH)
    original = fake.oneshot
    def reply(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if "independent reviewer" in prompt:
            res.stdout = json.dumps({"verdict": "reject", "unsupported_citations": items,
                                     "uncited_claims": None, "missing_parts": None, "other_issues": None})
        return res
    fr = FastResearch(tmp_path, "Question?", config=small_config(), **fake.hooks())
    fr.oneshot = reply
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_verdict"] == "reject"
    assert set(rec["review_issue_counts"].values()) == {0}


@pytest.mark.parametrize("code", ["`sys.argv[7]`", "`results[0]`", "```python\nresults[0]\n```"])
def test_bracket_number_in_code_does_not_crash_check(tmp_path, code):
    fr, _ = prepared(tmp_path)
    claims = fr._admitted()
    text, cites, _ = fr.render(f"Read {code} first.[C{claims[0]['id']}]", {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    fr.check()
    assert code in fr.path("answer.md").read_text()
    assert fr.path("grades.json").exists()


def test_narrowed_quoted_text_keeps_sentence_grade_alignment(tmp_path):
    fr, fake = prepared(tmp_path)
    claims = fr._admitted()
    cid = claims[0]["id"]
    text, cites, _ = fr.render(f"Broad fact.[C{cid}] Unknown fact.[C{cid}] Kept fact.[C{cid}]", {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    original = fake.oneshot
    def reply(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if prompt.startswith("You check sentences"):
            res.stdout = json.dumps({"checks": [
                {"n": 1, "verdict": "partly", "supported_claim": 'The page says "narrow fact."'},
                {"n": 2, "verdict": "nonsense"}, {"n": 3, "verdict": "supported"}]})
        return res
    fr.oneshot = reply
    fr.check()
    answer = fr.path("answer.md").read_text()
    assert 'The page says "narrow fact."[1] (grade B3' in answer
    assert "Unknown fact.[1] (Not checked)" in answer
    assert "Kept fact.[1] (grade" not in answer
    grades = fr._read_json("grades.json")
    assert [g["code"] for g in grades["sentences"]] == ["B3", "B2", "B2"]
    assert grades["counts"] == {"B2": 2, "B3": 1}


@pytest.mark.parametrize("abbreviation", ["e.g.", "i.e.", "etc.", "vs.", "cf."])
def test_common_abbreviations_do_not_split_a_sentence(abbreviation):
    text = f"Set a key, {abbreviation} in the shell, before you start.[1] Next fact.[2]"
    assert [r["text"] for r in fg.split_sentences(text)] == [text.rsplit(" Next", 1)[0], "Next fact.[2]"]


def test_quote_check_and_record_commands_preserve_deepest_run(tmp_path, monkeypatch):
    import fast_quote_check as qc
    import fast_research as research
    from deepest_research import DeepestResearch
    fake = FakeHermes(DEEP_SEARCH)
    fake.followups = QUESTIONS
    deep = DeepestResearch(tmp_path, "How does deep research work?", run_id="deep", config=small_config(), **fake.hooks())
    rec = deep.run()
    assert rec["status"] == "completed" and rec["quote_check"] == "pass"
    assert check_answer_files(tmp_path, "deep")["verdict"] == "pass"
    assert qc.main(["--root", str(tmp_path), "--run-id", "deep", "--write"]) == 0
    assert deep._read_json("quote_check.json")["verdict"] == "pass"
    monkeypatch.setattr(fw, "session_usage", fake.usage)
    assert research.main(["record", "--root", str(tmp_path), "--run-id", "deep"]) == 0
    fresh = deep._read_json("run.json")
    for field in ("jobs", "calls", "cost_usd", "questions", "branches", "wall_seconds"):
        assert fresh[field] == rec[field], field
    assert json.loads(deep.store.get_run("deep")["record_json"]) == fresh


def test_adopted_worker_relaunch_gets_its_full_timeout(tmp_path, monkeypatch):
    workdir = (tmp_path / "w").resolve()
    workdir.mkdir()
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(workdir)], start_new_session=True)
    (workdir / "meta.json").write_text(json.dumps({"pid": live.pid, "exit_code": None}))
    monkeypatch.setattr(fw, "build_command", lambda *a, **kw: [sys.executable, "-c", "print('fresh')"])
    monkeypatch.setattr(fw, "_kill", lambda pid: live.kill() or live.wait())
    try:
        res = fw.oneshot("fixture/stub", "q", workdir=workdir, toolsets="file", run_budget=1, timeout=0.5, poll=0.05)
        assert res.stdout.strip() == "fresh" and res.exit_code == 0
    finally:
        if live.poll() is None:
            live.kill()
            live.wait()


def test_repeated_sentence_is_marked_once_per_occurrence():
    out, items = fg.mark_not_verified("Use `x`. Use `x`.")
    assert out == "Use `x`. (Not verified) Use `x`. (Not verified)"
    assert items == ["Use `x`.", "Use `x`."]
    assert fg.mark_not_verified(out) == (out, [])
