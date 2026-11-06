"""One review-driven deepest follow-up wave, using only offline workers."""
import json
import re

import pytest

import deepest_research as dr
import fast_prompts as P
from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config
from test_deepest_research import QUESTIONS, SEARCH, controller

REVIEW_QUESTIONS = [
    {"question": "Which release documents the command?", "gap": "release", "impact": 5},
    {"question": "What date is given for the price?", "gap": "date", "impact": 4},
]
MISSING = ["The exact command is missing.", "The price needs a source date."]
ISSUES = {"unsupported_citations": [{"n": 1, "problem": "scope"}],
          "uncited_claims": ["one claim"], "missing_parts": MISSING, "other_issues": ["scope"]}
ARCHIVED = ("answer.md", "evidence.md", "citations.json", "quote_check.json", "review.json",
            "sentence_check.json", "grades.json", "not_verified.json", "answer.pre-check.md",
            "citations.pre-check.json")


class ReviewFake(FakeHermes):
    def __init__(self, verdict="reject", missing=None, review_questions=None):
        super().__init__(SEARCH)
        self.followups = QUESTIONS
        self.first_verdict = verdict
        self.missing = MISSING if missing is None else missing
        self.review_questions = REVIEW_QUESTIONS if review_questions is None else review_questions
        self.followup_prompts = []
        self.synthesis_prompts = []

    def oneshot(self, model, prompt, **kwargs):
        res = super().oneshot(model, prompt, **kwargs)
        if prompt.startswith("You suggest follow-up"):
            self.followup_prompts.append(prompt)
            if len(self.followup_prompts) > 1:
                res.stdout = json.dumps({"questions": self.review_questions})
        elif prompt.startswith("You write one combined"):
            self.synthesis_prompts.append(prompt)
            # Cite every child so the second check cannot pass by reusing the first inputs.
            ids = re.findall(r"^\[C(\d+)\] \(topic", prompt, flags=re.M)
            res.stdout = " ".join(f"Round {len(self.synthesis_prompts)} finding {i} holds.[C{i}]" for i in ids)
        elif "independent reviewer" in prompt:
            res.stdout = json.dumps({"verdict": self.first_verdict if self.count("review") == 1 else "reject",
                                    "summary": "Fixture review.", **ISSUES, "missing_parts": self.missing})
        return res


def test_reject_runs_one_review_round_and_archives_first_answer(tmp_path):
    fake = ReviewFake()
    fr = controller(tmp_path, fake)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert fake.count("followups") == fake.count("synthesis") == fake.count("review") == 2
    assert len(fake.synthesis_prompts) == 2
    rr = rec["review_round"]
    assert rr["ran"] is True and rr["skipped"] is None
    assert rr["questions"] == REVIEW_QUESTIONS
    assert rr["first_readiness"] == "Answers part of the question. Still missing:"
    assert rr["first_review_issue_counts"] == {key: len(value) for key, value in ISSUES.items()}
    assert len(rr["branches"]) == 2
    for row in rr["branches"]:
        assert row["status"] == "completed" and row["jobs"] > 0 and row["admitted_findings"] > 0
        assert row["run_id"] == fr.child_id(row["question"])
        child = fr.child(row["question"])
        assert child.cfg["limits"]["max_search_fetch_jobs"] == 16
        assert child.cfg["limits"]["concurrency"] == 4
        assert child.cfg["limits"]["research_deadline_seconds"] == 720
        assert f"Original question: {row['question']}" in fake.synthesis_prompts[1]
    assert len(fr.store.child_runs(fr.run_id)) == 5
    assert all(fr.path("round1", name).is_file() for name in ARCHIVED)
    assert "Round 1 finding" in fr.path("round1", "answer.md").read_text()
    assert "Round 2 finding" in fr.path("answer.md").read_text()
    assert "Round 1 finding" not in fr.path("answer.pre-check.md").read_text()
    assert all(part in fake.followup_prompts[1] for part in MISSING)
    assert fake.count("sentence_check") >= 2
    assert rec["quote_check"] == "pass" and rec["readiness"] == "Answers part of the question. Still missing:"
    assert rec["review_verdict"] == "reject"  # The second review is final, not a third wave.
    saved = {name: fr.path("round1", name).read_bytes() for name in ARCHIVED}
    calls = len(fake.sessions)
    assert controller(tmp_path, fake).run()["status"] == "completed"
    assert len(fake.sessions) == calls
    assert saved == {name: fr.path("round1", name).read_bytes() for name in ARCHIVED}
    assert fr._read_json("run.json")["review_round"] == rr


@pytest.mark.parametrize("verdict,missing,enabled,reason", [
    ("accept", MISSING, True, "accepted"),
    ("reject", [], True, "no_missing_parts"),
    ("reject", [None, {}, 12, "  "], True, "no_missing_parts"),
    ("reject", MISSING, False, "disabled"),
])
def test_skip_reasons(tmp_path, verdict, missing, enabled, reason):
    fake = ReviewFake(verdict, missing)
    cfg = small_config()
    cfg["deepest"]["review_round"] = {"enabled": enabled}
    fr = controller(tmp_path, fake, config=cfg)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_round"]["ran"] is False
    assert rec["review_round"]["skipped"] == reason
    assert rec["review_round"]["questions"] == rec["review_round"]["branches"] == []
    assert fake.count("followups") == fake.count("synthesis") == fake.count("review") == 1
    assert not fr.path("round1").exists()


def test_accept_with_notes_can_fill_missing_parts(tmp_path):
    fake = ReviewFake("accept_with_notes")
    rec = controller(tmp_path, fake).run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_round"]["ran"] is True
    assert rec["review_round"]["first_readiness"] == "Answers part of the question. Still missing:"


def test_drops_earlier_followups_before_applying_question_cap(tmp_path):
    duplicate = {**QUESTIONS[0], "question": QUESTIONS[0]["question"].upper().rstrip("?") + " now?"}
    fake = ReviewFake(review_questions=[duplicate, {**REVIEW_QUESTIONS[0], "impact": 4}])
    cfg = small_config()
    cfg["deepest"]["review_round"] = {"max_questions": 1}
    rec = controller(tmp_path, fake, config=cfg).run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_round"]["questions"] == [{**REVIEW_QUESTIONS[0], "impact": 4}]
    assert len(rec["review_round"]["branches"]) == 1


@pytest.mark.parametrize("configured,expected", [(None, 4), (99, 4), (2, 2), (0, 0)])
def test_review_question_default_and_hard_cap(tmp_path, configured, expected):
    fake = ReviewFake(review_questions=[{"question": f"What is unique missing detail {i}?", "impact": 4} for i in range(8)])
    cfg = small_config()
    if configured is not None:
        cfg["deepest"]["review_round"] = {"max_questions": configured}
    else:
        cfg["deepest"].pop("review_round", None)
    rec = controller(tmp_path, fake, config=cfg).run()
    assert rec["status"] == "completed", rec["error"]
    assert len(rec["review_round"]["questions"]) == expected
    assert len(rec["review_round"]["branches"]) == expected
    if not expected:  # zero questions skips the round without a writer call
        assert rec["review_round"]["skipped"] == "disabled" and len(fake.followup_prompts) == 1
        assert fake.count("synthesis") == fake.count("review") == 1
    else:
        assert f"Write up to {expected} follow-up" in fake.followup_prompts[1]


def test_only_duplicate_questions_skips_rewrite(tmp_path):
    fake = ReviewFake(review_questions=QUESTIONS)
    rec = controller(tmp_path, fake).run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_round"]["skipped"] == "no_questions"
    assert not rec["review_round"]["ran"]
    assert fake.count("synthesis") == fake.count("review") == 1


@pytest.mark.parametrize("stage", ["branches", "synthesis", "review"])
def test_resume_crash_in_review_round(tmp_path, monkeypatch, stage):
    fake = ReviewFake()
    fr = controller(tmp_path, fake)
    if stage == "branches":
        original = FastResearch.research
        crashed = []
        def research(child):
            if child.question == REVIEW_QUESTIONS[-1]["question"] and not crashed:
                crashed.append(True)
                raise KeyboardInterrupt("fixture controller crash")
            return original(child)
        monkeypatch.setattr(FastResearch, "research", research)
    else:
        original = fr.oneshot
        def oneshot(model, prompt, **kwargs):
            kind = "synthesis" if prompt.startswith("You write one combined") else "review" if "independent reviewer" in prompt else None
            if kind == stage and fake.count(kind) == 1:
                raise KeyboardInterrupt("fixture controller crash")
            return original(model, prompt, **kwargs)
        fr.oneshot = oneshot
    with pytest.raises(KeyboardInterrupt):
        fr.run()
    prior = {kind: fake.count(kind) for kind in ("followups", "plan", "search", "fetch", "read", "check", "synthesis", "review")}
    resumed = controller(tmp_path, fake)
    rec = resumed.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_round"]["ran"] is True
    assert fake.count("followups") == 2
    assert fake.count("plan") == prior["plan"]
    assert fake.count("synthesis") == fake.count("review") == 2
    if stage != "branches":
        for kind in ("plan", "search", "fetch", "read", "check"):
            assert fake.count(kind) == prior[kind]
    if stage == "review":
        assert fake.count("synthesis") == prior["synthesis"]
    assert "Round 1 finding" in resumed.path("round1", "answer.md").read_text()
    calls = len(fake.sessions)
    assert resumed.run()["status"] == "completed" and len(fake.sessions) == calls


@pytest.mark.parametrize("wave", [1, 2])
def test_failed_followups_in_either_wave_retry_on_resume(tmp_path, monkeypatch, wave):
    fake = ReviewFake()
    question = (QUESTIONS if wave == 1 else REVIEW_QUESTIONS)[0]["question"]
    original = FastResearch.research
    failed = []
    def research(child):
        if child.question == question and not failed:
            failed.append(True)
            raise RuntimeError("temporary fixture failure")
        return original(child)
    monkeypatch.setattr(FastResearch, "research", research)
    fr = controller(tmp_path, fake, run_id="deepest-retry")
    first = fr.run()
    assert first["status"] == "completed", first["error"]
    rows = first["branches"] if wave == 1 else first["review_round"]["branches"]
    assert any(row["status"] == "failed" for row in rows)
    archived = fr.path("round1", "answer.md").read_bytes()
    calls = {kind: fake.count(kind) for kind in ("followups", "plan", "synthesis", "review")}
    resumed = controller(tmp_path, fake, run_id=None)
    assert resumed.run_id == fr.run_id
    rec = resumed.run()
    assert rec["status"] == "completed", rec["error"]
    assert all(row["status"] == "completed" for row in rec["branches"] + rec["review_round"]["branches"])
    assert fake.count("followups") == calls["followups"]
    assert fake.count("plan") == calls["plan"]
    assert fake.count("synthesis") == calls["synthesis"] + 1
    assert fake.count("review") == calls["review"] + 1
    assert fr.path("round1", "answer.md").read_bytes() == archived


@pytest.mark.parametrize("wave", [1, 2])
@pytest.mark.parametrize("moved", ["answer", "answer.md", "review.json"])
def test_resume_partial_archive_with_failed_branch_keeps_first_files(tmp_path, monkeypatch, wave, moved):
    from pathlib import Path
    fake = ReviewFake()
    question = (QUESTIONS if wave == 1 else REVIEW_QUESTIONS)[0]["question"]
    original_research = FastResearch.research
    failed = []
    def research(child):
        if child.question == question and not failed:
            failed.append(True)
            raise RuntimeError("temporary fixture failure")
        return original_research(child)
    monkeypatch.setattr(FastResearch, "research", research)
    original_replace = Path.replace
    crashed = []
    snapshot = {}
    def replace(source, target):
        if target.parent.name == "round1" and not snapshot:
            snapshot.update({name: (target.parent.parent / name).read_bytes() for name in ARCHIVED})
        result = original_replace(source, target)
        if target.parent.name == "round1" and target.name == moved and not crashed:
            crashed.append(True)
            raise KeyboardInterrupt("fixture interrupted archive")
        return result
    monkeypatch.setattr(Path, "replace", replace)
    fr = controller(tmp_path, fake)
    with pytest.raises(KeyboardInterrupt):
        fr.run()
    resumed = controller(tmp_path, fake)
    rec = resumed.run()
    assert rec["status"] == "completed", rec["error"]
    assert snapshot == {name: resumed.path("round1", name).read_bytes() for name in ARCHIVED}
    assert all(row["status"] == "completed" for row in rec["branches"] + rec["review_round"]["branches"])
    assert fake.count("followups") == fake.count("synthesis") == fake.count("review") == 2


@pytest.mark.parametrize("skip", ["accepted", "no_missing_parts", "no_questions"])
def test_first_wave_retry_reconsiders_skipped_round(tmp_path, monkeypatch, skip):
    fake = ReviewFake("accept" if skip == "accepted" else "reject",
                      missing=[] if skip == "no_missing_parts" else None,
                      review_questions=[] if skip == "no_questions" else None)
    original = FastResearch.research
    failed = []
    def research(child):
        if child.question == QUESTIONS[0]["question"] and not failed:
            failed.append(True)
            raise RuntimeError("temporary fixture failure")
        return original(child)
    monkeypatch.setattr(FastResearch, "research", research)
    first = controller(tmp_path, fake)
    rec = first.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_round"]["skipped"] == skip
    fake.missing = MISSING
    fake.review_questions = REVIEW_QUESTIONS
    rec = controller(tmp_path, fake).run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["review_round"]["ran"] is True
    assert rec["review_round"]["first_readiness"] == "Answers part of the question. Still missing:"
    assert rec["review_round"]["first_review_issue_counts"]["missing_parts"] == 2
    assert fake.count("synthesis") == fake.count("review") == 3


def test_fast_mode_never_runs_review_round(tmp_path):
    fake = ReviewFake()
    fr = FastResearch(tmp_path, "Question?", run_id="fast-only", config=small_config(), **fake.hooks())
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert "review_round" not in rec
    assert fake.count("followups") == fake.count("synthesis") == 0
    assert fake.count("review") == fake.count("writer") == 1
    assert not fr.path("round1").exists()


def test_disagreement_rules_require_conflicting_facts_and_both_citations():
    assert "conflicting facts about the same thing" in P.SYNTHESIS
    assert "cites both findings" in P.SYNTHESIS
    assert "Never call agreement, a gap or a single source a disagreement" in P.SYNTHESIS
    assert "sources disagree or conflict" in P.SENTENCE_CHECK
    assert "quotes do conflict" in P.SENTENCE_CHECK
    assert "Otherwise it is unsupported" in P.SENTENCE_CHECK


def test_cli_summary_includes_review_round(tmp_path, monkeypatch, capsys):
    fake = ReviewFake()
    original = dr.DeepestResearch
    monkeypatch.setattr(dr, "DeepestResearch", lambda *args, **kwargs: original(*args, **kwargs, config=small_config(), **fake.hooks()))
    assert dr.main(["run", "--question", "Question?", "--root", str(tmp_path), "--run-id", "cli-round"]) == 0
    output = capsys.readouterr().out
    assert '"review_round"' in output and '"ran": true' in output
