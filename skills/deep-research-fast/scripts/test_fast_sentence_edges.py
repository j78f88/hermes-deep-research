"""Sentence check edge cases use the same offline setup as the core checks."""
import json
import re

from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config
from test_fast_sentence_check import prepared, SEARCH


def test_all_removed_answer_resumes_without_rewriting(tmp_path):
    fake = FakeHermes(SEARCH)
    original = fake.oneshot
    def unsupported(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if prompt.startswith("You check sentences"):
            data = json.loads(res.stdout)
            for row in data["checks"]:
                row["verdict"] = "unsupported"
            res.stdout = json.dumps(data)
        return res
    fr = FastResearch(tmp_path, "Question?", config=small_config(), **fake.hooks())
    fr.oneshot = unsupported
    rec = fr.run()
    assert rec["quote_check"] == "fail"  # No citations are left. The CLI must not claim a pass.
    text = fr.path("answer.md").read_text()
    before = len(fake.sessions)
    fr.run()
    assert len(fake.sessions) == before and fr.path("answer.md").read_text() == text


def test_no_review_verdict_is_record_only(tmp_path):
    fake = FakeHermes(SEARCH)
    original = fake.oneshot
    def no_verdict(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if "independent reviewer" in prompt:
            res.stdout = "I cannot decide."
        return res
    fr = FastResearch(tmp_path, "Question?", config=small_config(), **fake.hooks())
    fr.oneshot = no_verdict
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_verdict"] == "no_verdict"


def test_sentence_check_batches_twenty_and_lists_each_quote(tmp_path):
    fr, fake = prepared(tmp_path)
    claims = fr._admitted()
    rendered, cites, _ = fr.render(" ".join(f"Fact {i}.[C{claims[i % 2]['id']}]" for i in range(45)),
                                    {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(rendered)
    fr.write_json("citations.json", cites)
    result = fr.check()
    assert result["checked"] == 45 and fake.count("sentence_check") == 3
    assert len(re.findall(r"^\d+\. Claim:", fake.last_prompt["sentence_check"], re.M)) == 5
