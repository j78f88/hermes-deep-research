"""Deepest mode runs the whole pipeline on the existing offline fixture."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config

SEARCH = {"q1": ["https://example.invalid/shared"], "q3": ["https://example.invalid/linux"]}
QUESTIONS = [{"question": "Which exact settings are needed?", "gap": "settings", "impact": 5},
             {"question": "How are research costs recorded?", "gap": "costs", "impact": 3}]


def controller(tmp_path, fake, run_id="deep", **kwargs):
    from deepest_research import DeepestResearch
    return DeepestResearch(tmp_path, "How does deep research work?", run_id=run_id,
                           config=kwargs.pop("config", small_config()), **fake.hooks(), **kwargs)


def test_deepest_end_to_end(tmp_path):
    fake = FakeHermes(SEARCH)
    fake.followups = QUESTIONS
    fr = controller(tmp_path, fake)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["quote_check"] == "pass"
    assert fake.count("followups") == 1 and fake.count("synthesis") == 1
    assert fake.count("section") == fake.count("opening") == 0
    assert fake.count("sentence_check") >= 1 and fake.count("review") == 1
    assert fake.count("writer") == 0 and fake.count("preflight") == 4
    assert rec["questions"] == QUESTIONS
    assert len(rec["branches"]) == 2
    for branch in rec["branches"]:
        assert branch["jobs"] > 0 and branch["admitted_findings"] > 0
        assert branch["new_pages"] >= 0
        assert branch["run_id"] == f"deep-q{hashlib.sha1(branch['question'].encode()).hexdigest()[:10]}"
    children = fr.store.child_runs("deep")
    assert len(children) == 3
    assert set(fr.source_run_ids()) == {r["id"] for r in children}
    for c in children:
        assert c["parent_run_id"] == "deep"
        assert fr.store.phase(c["id"], "research")["status"] == "done"
        assert fr.store.phase(c["id"], "answer") is None
    for url in SEARCH["q1"] + SEARCH["q3"]:
        assert fake.count("fetch", url) == 1
    text = fr.path("answer.md").read_text()
    assert "Fact grades (NATO Admiralty Code):" in text
    assert text.index("Deep research branches") < text.index("## What is still missing")
    assert {"base", "questions", "branches", "answer", "check", "review", "finish", "record"} <= rec["phases"].keys()
    assert rec["grade_counts"] and rec["sentence_check_counts"] and rec["citation_recall"] == 1
    assert json.loads(fr.path("run.json").read_text()) == rec
    # Resuming is free of repeat model calls and header/marker changes.
    n = len(fake.sessions)
    assert fr.run()["status"] == "completed"
    assert len(fake.sessions) == n and fr.path("answer.md").read_text() == text


@pytest.mark.parametrize("questions", [[], QUESTIONS[:1]])
def test_fewer_than_two_questions_is_allowed_and_recorded(tmp_path, questions):
    fake = FakeHermes(SEARCH)
    fake.followups = questions
    fr = controller(tmp_path, fake)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert rec["questions"] == questions
    assert len(rec["branches"]) == len(questions)
    assert json.loads(fr.path("questions.json").read_text())["fewer_than_two"] is True


def test_failed_branch_does_not_stop_parent(tmp_path, monkeypatch):
    fake = FakeHermes(SEARCH)
    fake.followups = QUESTIONS
    original = FastResearch.research
    def research(self):
        if self.question == QUESTIONS[0]["question"]:
            raise RuntimeError("fixture branch failure")
        return original(self)
    monkeypatch.setattr(FastResearch, "research", research)
    fr = controller(tmp_path, fake)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert [b["status"] for b in rec["branches"]] == ["failed", "completed"]
    assert "fixture branch failure" in rec["branches"][0]["error"]
    assert fake.count("synthesis") == 1


def test_question_dedup_impact_and_hard_caps(tmp_path):
    from deepest_research import pick_questions
    raw = QUESTIONS + [None, {}, {"question": "bad", "impact": 10}, {"question": "bad boolean", "impact": True},
                       {"question": "MAIN?", "impact": 5}, {"question": QUESTIONS[1]["question"].upper(), "impact": 5}]
    chosen = pick_questions(raw, "Main?", 50)
    assert len(chosen) == 2
    assert chosen[0]["impact"] == 5 and chosen[1]["impact"] == 5
    fake = FakeHermes(SEARCH)
    cfg = small_config()
    cfg["deepest"] = {"max_questions": 50, "parallel_branches": 20}
    fr = controller(tmp_path, fake, config=cfg)
    assert fr.dcfg["max_questions"] == 10 and fr.dcfg["parallel_branches"] == 4
    assert len(pick_questions([{"question": f"Unique issue {i}", "impact": 3} for i in range(50)], "Main?", 50)) == 10
    near = pick_questions([{"question": "How does the exact source setting work?", "impact": 2},
                           {"question": "How does the exact source setting work now?", "impact": 5}], "Main?")
    assert len(near) == 1 and near[0]["impact"] == 5
    assert fr.child("Branch?").cfg["limits"]["concurrency"] == 4
    assert fr.child("Branch?").cfg["limits"]["max_search_fetch_jobs"] == 16
    assert fr.child("Branch?").cfg["limits"]["research_deadline_seconds"] == 720


def test_resume_after_branches_does_not_repeat_child_work(tmp_path):
    fake = FakeHermes(SEARCH)
    fake.followups = QUESTIONS
    fr = controller(tmp_path, fake)
    assert fr.run(stop_after="branches")["status"] == "completed"
    n = len(fake.sessions)
    prior = {k: fake.count(k) for k in ("followups", "plan", "search", "fetch", "read", "check")}
    fr = controller(tmp_path, fake)
    assert fr.run()["status"] == "completed"
    assert len(fake.sessions) > n
    assert prior == {k: fake.count(k) for k in prior}


def test_two_deepest_runs_have_separate_children_and_fetch_once(tmp_path):
    fake = FakeHermes(SEARCH)
    fake.followups = QUESTIONS[:1]
    a, b = (controller(tmp_path, fake, run_id=name) for name in ("deep-a", "deep-b"))
    a.lease_poll = b.lease_poll = 0.01
    with ThreadPoolExecutor(max_workers=2) as pool:
        records = list(pool.map(lambda fr: fr.run(), (a, b)))
    assert all(r["status"] == "completed" for r in records), [r["error"] for r in records]
    assert set(a.source_run_ids()).isdisjoint(b.source_run_ids())
    assert all(c["parent_run_id"] == a.run_id for c in a.store.child_runs(a.run_id))
    assert all(c["parent_run_id"] == b.run_id for c in b.store.child_runs(b.run_id))
    assert fake.count("fetch", SEARCH["q1"][0]) == fake.count("fetch", SEARCH["q3"][0]) == 1


def test_unknown_synthesis_id_stops_with_draft(tmp_path):
    fake = FakeHermes(SEARCH, invent_finding=True)
    fake.followups = []
    fr = controller(tmp_path, fake)
    rec = fr.run()
    assert rec["status"] == "stopped" and "finding ids" in rec["error"]
    assert "[C9999]" in fr.path("answer_draft.md").read_text()
    assert fake.count("review") == 0


def test_deepest_cli_exit_codes(tmp_path, monkeypatch, capsys):
    import deepest_research as dr
    original = dr.DeepestResearch
    fake = FakeHermes(SEARCH)
    fake.followups = []
    monkeypatch.setattr(dr, "DeepestResearch", lambda *a, **kw: original(*a, **kw, **fake.hooks()))
    args = ["run", "--question", "Question?", "--root", str(tmp_path), "--run-id", "cli"]
    assert dr.main(args) == 0
    assert json.loads(fr_json(tmp_path))["status"] == "completed"
    assert dr.main(["run", "--question", "Q?", "--root", str(tmp_path), "--run-id", "../bad"]) == 2


def test_implicit_resume_never_attaches_to_a_child(tmp_path):
    fake = FakeHermes(SEARCH)
    fr = controller(tmp_path, fake, run_id="deepest-parent")
    fr.store.set_run(fr.run_id, status="failed")
    base = fr.child(fr.question, base=True)
    fr.store.set_run(base.run_id, status="running")
    resumed = controller(tmp_path, fake, run_id=None)
    assert resumed.run_id == fr.run_id


def test_actual_question_and_branch_caps(tmp_path, monkeypatch):
    import threading
    import time
    fake = FakeHermes(SEARCH)
    fake.followups = [{"question": f"Which unique issue {i} matters?", "impact": 3} for i in range(50)]
    config = small_config()
    config["deepest"] = {"max_questions": 50, "parallel_branches": 20}
    gauge, lock = {"live": 0, "peak": 0}, threading.Lock()
    research = FastResearch.research
    def gauged(self):
        if self.run_id.endswith("-base"):
            return research(self)
        with lock:
            gauge["live"] += 1
            gauge["peak"] = max(gauge["peak"], gauge["live"])
        try:
            time.sleep(0.03)
            return research(self)
        finally:
            with lock:
                gauge["live"] -= 1
    monkeypatch.setattr(FastResearch, "research", gauged)
    fr = controller(tmp_path, fake, config=config)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert len(rec["questions"]) == len(rec["branches"]) == 10
    assert gauge["peak"] == 4
    assert rec["jobs"]["total"] == sum(sum(sum(counts.values()) for counts in fr.store.job_counts(rid).values())
                                        for rid in fr.source_run_ids())
    assert abs(rec["cost_usd"] - len(fake.sessions) * 0.01) < 1e-9


def fr_json(root):
    return (root / "runs" / "cli" / "run.json").read_text()
