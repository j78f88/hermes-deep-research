"""Remaining review suggestions and trial findings, tested without network or models."""
import json
import re
import signal
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import fact_grades as fg
import fast_prompts as P
import fast_worker as fw
from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config
from test_fast_sentence_check import prepared, sentence_reply, SEARCH
from test_deepest_research import controller, QUESTIONS, SEARCH as DEEP_SEARCH


@pytest.mark.parametrize("name", ["ANSWER", "SYNTHESIS"])
def test_writer_prompts_forbid_tables_and_block_quotes(name):
    assert "No tables or block quotes." in getattr(P, name)


def test_table_cells_are_checked_graded_and_marked(tmp_path):
    fr, fake = prepared(tmp_path)
    claims = fr._admitted()
    body = ("| Key | Fact |\n| --- | --- |\n"
            f"| Wrong | Wrong table fact.[C{claims[0]['id']}] |\n"
            f"| Kept | Kept table fact.[C{claims[1]['id']}] |\n"
            "| Setting | OPENROUTER_API_KEY |")
    text, cites, _ = fr.render(body, {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    fr.oneshot = sentence_reply(fake, {"Wrong table fact.": "unsupported"})
    result = fr.check()
    kept = fr.path("answer.md").read_text().split("## Removed because", 1)[0]
    assert result["checked"] == 2 and result["removed"] == 1
    assert "Wrong table fact." not in kept
    assert "Kept table fact.[1]" in kept
    assert "OPENROUTER_API_KEY (Not verified)" in kept
    assert [g["code"] for g in fr._read_json("grades.json")["sentences"]] == ["B2"]
    assert fr._read_json("quote_check.json")["verdict"] == "pass"


def test_self_docs_and_repo_never_count_as_independent_origins():
    supports = [{"kind": "self", "verdict": "supported", "origin": "project.example"},
                {"kind": "self", "verdict": "supported", "origin": "github.com/project"}]
    assert fg.grade(supports)["code"] == "B2"
    supports.append({"kind": "independent", "verdict": "supported", "origin": "review.example"})
    assert fg.grade(supports)["code"] == "B1"


@pytest.mark.parametrize("host,expected", [
    ("docs.api.example.com", "example.com"), ("www.example.com", "example.com"),
    ("docs.example.com.au", "example.com.au"), ("docs.example.net.au", "example.net.au"),
    ("docs.example.org.nz", "example.org.nz"), ("docs.example.gov.uk", "example.gov.uk"),
    ("docs.example.edu.au", "example.edu.au"), ("docs.example.co.uk", "example.co.uk"),
    ("docs.example.ac.uk", "example.ac.uk"), ("docs.example.dev.au", "dev.au"),
    ("docs.example.com.xyz", "com.xyz"), ("localhost", "localhost"),
    ("github.com/Alice/repo", "github.com/alice"), ("github.com/Bob/repo", "github.com/bob")])
def test_site_origin_merges_subdomains_with_country_suffix_rule(host, expected):
    assert fg.site_origin("https://" + host) == expected


def test_removal_drops_empty_bullets_and_extra_spaces(tmp_path):
    fr, fake = prepared(tmp_path)
    claims = fr._admitted()
    body = f"- Wrong fact.[C{claims[0]['id']}]\n\nKept fact.[C{claims[1]['id']}] Wrong fact.[C{claims[0]['id']}] Last fact.[C{claims[1]['id']}]"
    text, cites, _ = fr.render(body, {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    fr.oneshot = sentence_reply(fake, {"Wrong fact.": "unsupported"})
    fr.check()
    kept = fr.path("answer.md").read_text().split("## Removed because", 1)[0]
    assert not re.search(r"(?m)^\s*[-*+]\s*$", kept)
    assert "Kept fact.[1] Last fact.[1]" in kept


def test_sentence_edits_preserve_code_spaces_and_list_indentation(tmp_path):
    fr, fake = prepared(tmp_path)
    claims = fr._admitted()
    body = ("Context.\n\n  - Use `printf 'a  b'` here."
            f"[C{claims[0]['id']}] Wrong fact.[C{claims[1]['id']}] Last fact.[C{claims[0]['id']}] ")
    text, cites, _ = fr.render(body, {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    fr.oneshot = sentence_reply(fake, {"Wrong fact.": "unsupported"})
    fr.check()
    assert "\n  - Use `printf 'a  b'` here.[1] Last fact.[1]" in fr.path("answer.md").read_text()


def test_multiline_quote_cannot_take_another_sources_grade(tmp_path):
    fr, _ = prepared(tmp_path)
    claims = fr._admitted()
    quote = "First quote line.\n2. This looks like source two."
    with fr.store.tx() as conn:
        conn.execute("UPDATE claims SET quote=? WHERE id=?", (quote, claims[0]["id"]))
    claims = fr._admitted()
    text, cites, _ = fr.render(" ".join(f"Fact.[C{c['id']}]" for c in claims), {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    fr.check()
    evidence = fr.path("evidence.md").read_text()
    assert f'Quote: "{quote}"' in evidence
    assert evidence.count("Source grade:") == 2
    entries = evidence.split("## [")[1:]
    for entry, citation in zip(entries, fr._read_json("citations.json")):
        assert f"Source grade: {citation['grade']['code']}," in entry
    assert "Quote:" not in fr.path("answer.md").read_text()


def test_quoted_narrowing_does_not_hide_uncited_setting(tmp_path):
    fr, fake = prepared(tmp_path)
    claims = fr._admitted()
    text, cites, _ = fr.render(f"Broad fact.[C{claims[0]['id']}] Set OPENROUTER_API_KEY.",
                               {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    original = fake.oneshot
    def reply(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if prompt.startswith("You check sentences"):
            res.stdout = json.dumps({"checks": [{"n": 1, "verdict": "partly",
                                                 "supported_claim": 'The page says "narrow fact."'}]})
        return res
    fr.oneshot = reply
    fr.check()
    assert "Set OPENROUTER_API_KEY. (Not verified)" in fr.path("answer.md").read_text()
    assert fr._read_json("not_verified.json") == ["Set OPENROUTER_API_KEY."]


def test_record_refresh_keeps_fast_parent_scope_with_linked_followup(tmp_path, monkeypatch):
    import fast_research as research
    fake = FakeHermes(SEARCH)
    parent = FastResearch(tmp_path, "Parent?", run_id="fast-parent", config=small_config(), **fake.hooks())
    rec = parent.run()
    followup = FastResearch(tmp_path, "Followup?", run_id="fast-followup", parent_run_id=parent.run_id,
                           config=small_config(), **fake.hooks())
    assert followup.run()["status"] == "completed"
    monkeypatch.setattr(fw, "session_usage", fake.usage)
    assert research.main(["record", "--root", str(tmp_path), "--run-id", parent.run_id]) == 0
    fresh = parent._read_json("run.json")
    for field in ("jobs", "calls", "cost_usd", "wall_seconds", "claims", "meaning_check", "pages"):
        if field == "pages":
            assert fresh[field]["saved_this_run"] == rec[field]["saved_this_run"]
        else:
            assert fresh[field] == rec[field], field
    assert "questions" not in fresh and "branches" not in fresh


def test_fast_parent_quote_check_does_not_admit_followup_claim(tmp_path):
    from fast_quote_check import check_answer_files
    fake = FakeHermes(SEARCH)
    parent = FastResearch(tmp_path, "Parent?", run_id="fast-parent", config=small_config(), **fake.hooks())
    child = FastResearch(tmp_path, "Followup?", run_id="fast-followup", parent_run_id=parent.run_id,
                         config=small_config(), **fake.hooks())
    assert child.run(stop_after="research")["status"] == "completed"
    claims = child._admitted()
    text, cites, _ = parent.render(f"Child fact.[C{claims[0]['id']}]", {c["id"]: c for c in claims})
    parent.path("answer.md").write_text(text)
    parent.write_json("citations.json", cites)
    result = check_answer_files(tmp_path, parent.run_id)
    assert result["verdict"] == "fail"
    assert any(p["kind"] == "unknown_claim" for p in result["problems"])


def test_failed_base_keeps_parent_resumable(tmp_path, monkeypatch):
    from deepest_research import DeepestResearch
    fake = FakeHermes(DEEP_SEARCH)
    original = FastResearch.research
    failures = []
    def research(self):
        if self.run_id.endswith("-base") and not failures:
            failures.append(True)
            raise RuntimeError("temporary base failure")
        return original(self)
    monkeypatch.setattr(FastResearch, "research", research)
    fr = DeepestResearch(tmp_path, "Question?", config=small_config(), **fake.hooks())
    failed = fr.run()
    assert failed["status"] == "failed", failed["error"]
    resumed = DeepestResearch(tmp_path, fr.question, config=small_config(), **fake.hooks())
    assert resumed.run_id == fr.run_id
    assert resumed.run()["status"] == "completed"
    assert fake.count("plan") == 1


def test_branch_constructor_error_is_recorded_without_stopping_parent(tmp_path, monkeypatch):
    fake = FakeHermes(DEEP_SEARCH)
    fake.followups = QUESTIONS
    fr = controller(tmp_path, fake)
    original = fr.child
    def child(question, **kwargs):
        if question == QUESTIONS[0]["question"]:
            raise RuntimeError("fixture constructor failure")
        return original(question, **kwargs)
    monkeypatch.setattr(fr, "child", child)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert [b["status"] for b in rec["branches"]] == ["failed", "completed"]
    failed = rec["branches"][0]
    assert "fixture constructor failure" in failed["error"]
    assert failed["jobs"] == failed["admitted_findings"] == failed["new_pages"] == 0
    assert fake.count("synthesis") == 1


def test_branch_counts_do_not_scan_other_runs(tmp_path, monkeypatch):
    fake = FakeHermes(DEEP_SEARCH)
    fr = controller(tmp_path, fake)
    assert fr.run(stop_after="base")["status"] == "completed"
    def forbidden():
        raise AssertionError("must not scan all admitted findings")
    monkeypatch.setattr(fr, "_admitted", forbidden)
    child_id = fr.run_id + "-base"
    counts = fr._branch_counts(child_id, fr.question, "completed")
    assert counts["admitted_findings"] == len(fr.store.claims(child_id, status="pass")) > 0


def test_keyboard_interrupt_leaves_worker_alive_for_adoption(tmp_path, monkeypatch):
    monkeypatch.setattr(fw, "build_command", lambda *a, **kw: [sys.executable, "-c", "import time; time.sleep(30)", str(tmp_path)])
    processes, killed = [], []
    popen = subprocess.Popen
    def launch(*args, **kwargs):
        proc = popen(*args, **kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(subprocess, "Popen", launch)
    def kill(pid):
        killed.append(pid)
        os.killpg(pid, signal.SIGKILL)
    monkeypatch.setattr(fw, "_kill", kill)
    def interrupt(*args):
        raise KeyboardInterrupt("fixture Ctrl-C")
    try:
        with pytest.raises(KeyboardInterrupt):
            fw.oneshot("fixture/stub", "q", workdir=tmp_path, toolsets="file", run_budget=1, timeout=1, on_start=interrupt)
        assert killed == []
        assert processes[0].poll() is None
        assert fw._pid_alive(processes[0].pid, str(tmp_path))
    finally:
        for proc in processes:
            if proc.poll() is None:
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


@pytest.mark.parametrize("text", [
    '{"checks":[{"n":1,"verdict":"supported",},],}',
    '```json\n{"checks":[{"n":1,"verdict":"supported",},],}\n```',
    'Some text. {"checks":[{"n":1,"verdict":"supported",},],} Done.'
])
def test_json_reply_recovers_trailing_commas_in_nested_objects(text):
    assert fw.parse_json_reply(text) == {"checks": [{"n": 1, "verdict": "supported"}]}


def test_trailing_comma_recovery_preserves_strings_and_escaped_quotes():
    value = {'quote': 'keep , } and , ] and "quoted", } and \\', 'checks': [{'n': 1, 'verdict': 'supported'}]}
    malformed = json.dumps(value)[:-1] + ",}"
    assert fw.parse_json_reply(malformed) == value
    assert fw.parse_json_reply('{"checks": [1,,]}') is None


@pytest.mark.parametrize("bad_reply", ["not JSON", '{"checks":[]}', '{"checks":[{"n":1,"verdict":"garbage"}]}', '{"checks":null}'])
def test_sentence_check_retries_batch_once_if_no_usable_verdicts(tmp_path, bad_reply):
    fr, fake = prepared(tmp_path)
    original = fake.oneshot
    attempts = []
    def reply(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if prompt.startswith("You check sentences"):
            attempts.append(str(kwargs["workdir"]))
            if len(attempts) == 1:
                res.stdout = bad_reply
        return res
    fr.oneshot = reply
    result = fr.check()
    assert result["checked"] == 4 and result["unchecked"] == 0
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    fr.check()
    assert len(attempts) == 2  # saved verdicts are still adopted on resume


def test_sentence_check_retry_is_bounded_when_both_replies_are_unusable(tmp_path):
    fr, fake = prepared(tmp_path)
    fake.checker_garbage = True
    result = fr.check()
    assert fake.count("sentence_check") == 2
    assert result["unchecked"] == 4


def test_reading_rules_prioritise_exact_settings_commands_and_short_code_quotes():
    for prompt in (P.FETCH, P.READ):
        assert "Exact settings, commands and code are often the most useful facts." in prompt
        assert "quote the code exactly" in prompt
        assert "A code quote may be shorter than eight words." in prompt
    from fast_quote_check import MIN_QUOTE_CHARS
    assert MIN_QUOTE_CHARS == 8
