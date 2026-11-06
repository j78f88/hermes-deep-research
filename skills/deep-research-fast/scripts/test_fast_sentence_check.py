"""Sentence attribution, resume and finish use only FakeHermes."""
import json
import re

import pytest

from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config

SEARCH = {"q1": ["https://example.invalid/docs"]}


def prepared(tmp_path, fake=None):
    fake = fake or FakeHermes(SEARCH)
    fr = FastResearch(tmp_path, "Question?", run_id="sentences", skip_preflight=True,
                      config=small_config(), **fake.hooks())
    assert fr.run(stop_after="research")["status"] == "completed"
    claims = fr._admitted()
    body = (f"Kept fact.[C{claims[0]['id']}] Broad fact.[C{claims[0]['id']}] "
            f"Wrong fact.[C{claims[1]['id']}] Unknown fact.[C{claims[0]['id']}]\n\nSet OPENROUTER_API_KEY.")
    text, cites, _ = fr.render(body, {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text)
    fr.write_json("citations.json", cites)
    return fr, fake


def sentence_reply(fake, verdicts, served=None):
    original = fake.oneshot
    def reply(model, prompt, **kwargs):
        if not prompt.startswith("You check sentences"):
            return original(model, prompt, **kwargs)
        # Produce a real fixture session so served-vendor lookup remains exercised.
        res = original(model, prompt.replace("You check sentences from a research answer. Each sentence is a claim.",
                                           "You check findings from a research run.", 1), **kwargs)
        rows = []
        for n, claim in re.findall(r"^(\d+)\. Claim: (.*)$", prompt, re.M):
            value = verdicts.get(claim.strip(), "supported")
            row = {"n": int(n), "verdict": value}
            if value == "partly":
                row["supported_claim"] = "Narrow fact."
            rows.append(row)
        res.stdout = json.dumps({"checks": rows})
        if served:
            fake.sessions[res.session_id]["model"] = served
        return res
    return reply


def test_sentence_verdicts_fix_only_their_sentence_and_renumber(tmp_path):
    fr, fake = prepared(tmp_path)
    fr.oneshot = sentence_reply(fake, {"Broad fact.": "partly", "Wrong fact.": "unsupported", "Unknown fact.": "garbage"})
    result = fr.check()
    text = fr.path("answer.md").read_text()
    kept, sources = text.split("## Sources", 1)
    evidence = fr.path("evidence.md").read_text()
    removed = evidence.split("## Removed because the sources did not support them", 1)[1]
    assert "Kept fact.[1]" in kept and "Narrow fact.[1]" in kept
    assert "Broad fact." not in kept and "Wrong fact." not in kept
    assert "Wrong fact." in removed and not re.search(r"\[\d+\]", removed)
    assert "Unknown fact.[1] (Not checked)" in kept
    assert "OPENROUTER_API_KEY. (Not verified)" in kept
    assert "usually reliable source" in evidence and "Quote:" not in sources
    assert len(json.loads(fr.path("citations.json").read_text())) == 1
    grades = json.loads(fr.path("grades.json").read_text())
    assert grades["citation_recall"] == 0.25 and grades["citation_precision"] is None
    assert grades["precision_note"] == "not measured"
    assert result["checked"] == 3 and result["narrowed"] == result["removed"] == 1
    assert json.loads(fr.path("quote_check.json").read_text())["verdict"] == "pass"
    assert "Page:" in fake.last_prompt["check"] and "Quote:" in fake.last_prompt["check"]
    # Re-running an interrupted phase works from pristine backups, not marked text.
    fr.check()
    assert fr.path("answer.md").read_text() == text


def test_sentence_checker_same_served_vendor_stops(tmp_path):
    fr, fake = prepared(tmp_path)
    # Simulate writer served by Google, while its requested vendor was Anthropic.
    fr.store.add_call(fr.run_id, "writer-served", "writer", fr.model("writer"))
    fake.sessions["writer-served"] = {"model": "google/writer"}
    fr.oneshot = sentence_reply(fake, {}, served="google/checker")
    with pytest.raises(RuntimeError, match="sentence checker.*vendor"):
        fr.check()
    assert not fr.path("grades.json").exists()


def test_garbage_sentence_reply_is_marked_not_checked(tmp_path):
    fr, fake = prepared(tmp_path)
    original = sentence_reply(fake, {})
    def garbage(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        res.stdout = "not JSON"
        return res
    fr.oneshot = garbage
    result = fr.check()
    assert result["unchecked"] == 4
    assert fr.path("answer.md").read_text().count("(Not checked)") == 4


def test_reject_review_is_recorded_and_finish_is_idempotent(tmp_path):
    fake = FakeHermes(SEARCH)
    original = fake.oneshot
    def reject(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if "independent reviewer" in prompt:
            res.stdout = json.dumps({"verdict": "reject", "unsupported_citations": [{"n": 1}],
                                     "uncited_claims": ["one"], "missing_parts": ["two"], "other_issues": ["three"]})
        return res
    fr = FastResearch(tmp_path, "Question?", config=small_config(), **fake.hooks())
    fr.oneshot = reject
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_verdict"] == "reject"
    assert rec["review_issue_counts"] == {"unsupported_citations": 1, "uncited_claims": 1, "missing_parts": 1, "other_issues": 1}
    assert rec["grade_counts"] and rec["sentence_check_counts"]
    text = fr.path("answer.md").read_text()
    assert text.index("Finding") < text.index("## Checks") < text.index("Sentence check:") < text.index("## Sources")
    assert text.splitlines()[2] == "Partial answer. Do not rely on it alone."
    assert "questioned citations 1 (1)" in text
    assert "Fact grades (NATO Admiralty Code):" in text
    fr.run()
    assert fr.path("answer.md").read_text() == text
