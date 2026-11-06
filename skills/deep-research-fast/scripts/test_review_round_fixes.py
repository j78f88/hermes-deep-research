"""Round 6 review fixes for the review round. Offline, FakeHermes only."""
import json

import pytest

from fast_research import FastResearch
from fast_test_support import small_config
from test_deepest_research import QUESTIONS, controller
from test_review_round import MISSING, REVIEW_QUESTIONS, ReviewFake


def _fail_once(monkeypatch, question, exc=RuntimeError):
    original = FastResearch.research
    done = []

    def research(child):
        if child.question == question and not done:
            done.append(True)
            raise exc("fixture failure")
        return original(child)
    monkeypatch.setattr(FastResearch, "research", research)


class FirstNotesFake(ReviewFake):
    """First review: accept_with_notes with missing parts. Every later review: reject."""

    def oneshot(self, model, prompt, **kwargs):
        res = super().oneshot(model, prompt, **kwargs)
        if "independent reviewer" in prompt and self.count("review") == 1:
            body = json.loads(res.stdout)
            body["verdict"] = "accept_with_notes"
            res.stdout = json.dumps(body)
        return res



def test_crash_in_round_after_first_wave_failure_keeps_round_one(tmp_path, monkeypatch):
    """A first-wave branch failed. The controller then dies inside the review-round wave."""
    fake = FirstNotesFake()
    original = FastResearch.research
    state = {"first": False, "crash": False}

    def research(child):
        if child.question == QUESTIONS[0]["question"] and not state["first"]:
            state["first"] = True
            raise RuntimeError("temporary first-wave failure")
        if child.question == REVIEW_QUESTIONS[-1]["question"] and not state["crash"]:
            state["crash"] = True
            raise KeyboardInterrupt("controller killed in review-round wave")
        return original(child)
    monkeypatch.setattr(FastResearch, "research", research)
    fr = controller(tmp_path, fake)
    with pytest.raises(KeyboardInterrupt):
        fr.run()
    before = {k: fake.count(k) for k in ("synthesis", "review", "sentence_check")}
    assert before["synthesis"] == before["review"] == 1
    rec = controller(tmp_path, fake).run()
    assert rec["status"] == "completed", rec["error"]
    rr = rec["review_round"]
    round1_review = json.loads(fr.path("round1", "review.json").read_text())
    report = {"synthesis": fake.count("synthesis"), "review": fake.count("review"),
              "first_readiness": rr["first_readiness"], "round1_review_verdict": round1_review["verdict"],
              "legacy_reviews": sorted(p.name for p in fr.dir.glob("review.json.legacy-*"))}
    print("PROBE", json.dumps(report))
    # The round had already started. Only the second answer needs the retried branch.
    assert fake.count("synthesis") == 2, report
    assert fake.count("review") == 2, report
    # The verdict does not set the line. The round-one review's missing parts do.
    expected = ("Answers part of the question. Still missing:" if (round1_review.get("review") or {}).get("missing_parts")
                else "Answers the question. Notes are at the end.")
    assert rr["first_readiness"] == expected, report


class UnknownIdSecondSynthesis(ReviewFake):
    def oneshot(self, model, prompt, **kwargs):
        res = super().oneshot(model, prompt, **kwargs)
        if prompt.startswith("You write one combined") and len(self.synthesis_prompts) >= 2:
            res.stdout += " Invented.[C9999]"
        return res




@pytest.mark.parametrize("value", [False, 0, "false", "no", None])
def test_falsy_enabled_values_disable_the_round(tmp_path, value):
    fake = ReviewFake()
    fr = controller(tmp_path, fake)
    fr.rcfg["enabled"] = value
    rec = fr.run()
    assert rec["review_round"]["skipped"] == "disabled" and fake.count("followups") == 1


def test_zero_review_questions_skips_without_a_writer_call(tmp_path):
    fake = ReviewFake()
    fr = controller(tmp_path, fake)
    fr.rcfg["max_questions"] = 0
    rec = fr.run()
    assert rec["review_round"]["skipped"] == "disabled" and fake.count("followups") == 1


def test_review_round_prompt_lists_earlier_questions(tmp_path):
    fake = ReviewFake()
    controller(tmp_path, fake).run()
    assert "already_asked_do_not_repeat" in fake.followup_prompts[1]
    assert all(q["question"] in fake.followup_prompts[1] for q in QUESTIONS[:2])


def test_disagreement_rule_sits_before_findings_and_asks_for_one_sentence():
    import fast_prompts as P
    assert P.SENTENCE_CHECK.index("disagree or conflict") < P.SENTENCE_CHECK.index("Findings:")
    assert "in one sentence that names and cites both findings" in P.SYNTHESIS
    P.SENTENCE_CHECK.format(findings="x")


def test_stopped_second_round_points_at_first_answer(tmp_path, monkeypatch):
    import deepest_research as dr
    from fast_research import PhaseError
    fake = ReviewFake()
    fr = controller(tmp_path, fake)
    original = dr.DeepestResearch.answer
    calls = []

    def answer(self):
        calls.append(1)
        if len(calls) == 2:
            raise PhaseError("fixture stop in second synthesis")
        return original(self)
    monkeypatch.setattr(dr.DeepestResearch, "answer", answer)
    rec = fr.run()
    assert rec["status"] == "stopped"
    assert rec["answer_path"].endswith("round1/answer.md") and "first-round answer" in rec["readiness"]
