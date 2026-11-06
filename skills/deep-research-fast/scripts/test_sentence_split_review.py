"""round 8 review probes for the abbreviation rule, kept as regression tests. Independent, offline ISSUE-861 round 8 probes; all writes use pytest tmp_path."""
import json
import types

import pytest

import fact_grades as fg
from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config
from test_fast_sentence_check import prepared, sentence_reply, SEARCH

from pathlib import Path

# fact_grades.py as it was just before the abbreviation rule, kept as a fixture.
BEFORE_FIX = Path(__file__).resolve().parent / "fixtures" / "fact_grades_before_abbrev.py"
SETTING = "Set `CACHE_TTL=60`."


@pytest.fixture(scope="module")
def baseline():
    module = types.ModuleType("baseline_fact_grades")
    exec(compile(BEFORE_FIX.read_text(encoding="utf-8"), "before-fix fact_grades.py", "exec"), module.__dict__)
    return module


def test_uncited_abbreviation_end_joins_the_next_sentence_and_is_checked_with_it():
    # Known trade-off: "sold in the U.S. The tool" cannot be told apart from "the U.S. Etsy sellers".
    # The joined sentence carries the citation, so the sentence check judges both parts against its quotes.
    rows = fg.split_sentences("The product is sold in the U.S. The tool runs on Linux.[1]")
    assert [r["cites"] for r in rows] == [[1]]

@pytest.mark.parametrize("body", [
    "The U.S. market is large.[1] " + SETTING,
    "  - The U.S. market is large.[1] " + SETTING,
    "  12. The U.S. market is large.[1] " + SETTING,
    "> The U.S. market is large.[1] " + SETTING,
    "| The U.S. market is large.[1] | " + SETTING + " |",
    "| The U.S. market is large.[1] " + SETTING + " | Next fact.[2] |",
    "# Heading\n\nThe U.S. market is large.[1]\n\n" + SETTING,
    "```\nU.S. `argv[1]`\n```\nThe U.S. market is large.[1] " + SETTING,
    "Use `a | b[2]. next` in the U.S. market.[1] " + SETTING,
    "The U.S. market is large.[1] (grade D2: weak) " + SETTING,
    "Le marché U.S. coûte €10.[1] " + SETTING,
])
def test_offsets_marking_and_idempotence(body):
    previous = {}
    for row in fg.split_sentences(body):
        assert body.split("\n")[row["line"]][row["start"]:row["end"]] == row["text"]
        assert row["start"] >= previous.get(row["line"], 0)
        previous[row["line"]] = row["end"]
    marked, items = fg.mark_not_verified(body)
    assert items == [SETTING]
    assert SETTING + " (Not verified)" in marked
    assert fg.mark_not_verified(marked) == (marked, [])


def write_answer(fr, body):
    claims = fr._admitted()
    text, cites, dropped = fr.render(body, {c["id"]: c for c in claims})
    assert not dropped
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)


@pytest.mark.parametrize("implementation", ["baseline", "head"])
def test_narrowing_to_initialism_must_not_hide_setting(tmp_path, monkeypatch, baseline, implementation):
    if implementation == "baseline":
        monkeypatch.setattr(fg, "split_sentences", baseline.split_sentences)
    fr, fake = prepared(tmp_path)
    fr.show_grades = False
    first = fr._admitted()[0]
    write_answer(fr, f"Available domestically.[C{first['id']}] {SETTING}")
    original = fake.oneshot
    prompts = []

    def narrow(model, prompt, **kwargs):
        result = original(model, prompt, **kwargs)
        if prompt.startswith("You check sentences"):
            prompts.append(prompt)
            result.stdout = json.dumps({"checks": [{
                "n": 1, "verdict": "partly", "supported_claim": "Available in the U.S.",
            }]})
        return result

    fr.oneshot = narrow
    result = fr.check()
    assert result["narrowed"] == 1
    assert all("CACHE_TTL" not in prompt for prompt in prompts)
    assert fr._read_json("quote_check.json")["verdict"] == "pass"
    answer = fr.path("answer.md").read_text()
    assert SETTING in answer
    assert fr._read_json("not_verified.json") == [SETTING]
    assert SETTING + " (Not verified)" in answer


def test_offsets_renumbering_and_cached_retry_with_abbreviations(tmp_path):
    fr, fake = prepared(tmp_path)
    a, b = fr._admitted()[:2]
    write_answer(fr, f"Remove me.[C{b['id']}] The U.S. market grows.[C{a['id']}] {SETTING}")
    fr.oneshot = sentence_reply(fake, {"Remove me.": "unsupported"})
    result = fr.check()
    assert result["checked"] == 2 and result["removed"] == 1
    answer = fr.path("answer.md").read_text()
    assert "Remove me." not in answer
    assert "The U.S. market grows.[1]" in answer
    assert SETTING + " (Not verified)" in answer
    assert len(fr._read_json("citations.json")) == 1
    assert fr._read_json("grades.json")["sentences"][0]["cites"] == [1]
    assert fr._read_json("quote_check.json")["verdict"] == "pass"
    before = len(fake.sessions)
    fr.check()
    assert len(fake.sessions) == before
    assert fr.path("answer.md").read_text() == answer


def test_interrupted_old_check_invalidates_changed_segmentation(tmp_path, monkeypatch, baseline):
    fr, fake = prepared(tmp_path)
    first = fr._admitted()[0]
    write_answer(fr, f"The U.S. market grows.[C{first['id']}]")
    current_split = fg.split_sentences
    monkeypatch.setattr(fg, "split_sentences", baseline.split_sentences)
    fr.check()
    old_signature = fr._read_json("sentence_check.json")["input_sha256"]
    before = fake.count("sentence_check")
    monkeypatch.setattr(fg, "split_sentences", current_split)
    fr.check()
    current_signature = fr._read_json("sentence_check.json")["input_sha256"]
    assert current_signature != old_signature
    assert fake.count("sentence_check") == before + 1
    assert fr._read_json("sentence_check.json")["sentences"][0]["text"] == "The U.S. market grows.[1]"
    before = fake.count("sentence_check")
    fr.check()
    assert fake.count("sentence_check") == before


def test_completed_resume_remains_stable(tmp_path):
    fake = FakeHermes(SEARCH)
    original = fake.oneshot

    def answer_with_initialism(model, prompt, **kwargs):
        result = original(model, prompt, **kwargs)
        if prompt.startswith("You write the final answer"):
            import re
            first = re.findall(r"^\[C(\d+)\] \(topic", prompt, flags=re.M)[0]
            result.stdout = f"The U.S. market grows.[C{first}] {SETTING}"
        return result

    hooks = {**fake.hooks(), "oneshot": answer_with_initialism}
    fr = FastResearch(tmp_path, "Question?", run_id="r8-resume", config=small_config(), **hooks)
    assert fr.run()["status"] == "completed"
    answer = fr.path("answer.md").read_text()
    assert "The U.S. market grows.[1]" in answer
    assert SETTING + " (Not verified)" in answer
    before = len(fake.sessions)
    resumed = FastResearch(tmp_path, fr.question, run_id=fr.run_id, config=small_config(), **hooks)
    assert resumed.run()["status"] == "completed"
    assert resumed.path("answer.md").read_text() == answer
    assert len(fake.sessions) == before
