"""Round-three quality regressions. All worker calls use the offline fixture."""
import itertools
import json
from collections import Counter
from pathlib import Path

import pytest

import fast_prompts as P
from deepest_research import DeepestResearch, pick_questions
from fast_research import FastResearch
from fast_quote_check import check_answer_files
from fast_test_support import FakeHermes, small_config

SEARCH = {"q1": ["https://example.invalid/shared"], "q3": ["https://example.invalid/linux"]}
QUESTIONS = [{"question": "Which exact setting is needed?", "gap": "setting", "impact": 5},
             {"question": "What is the current price?", "gap": "price", "impact": 4}]


def prepared_fast(tmp_path, fake):
    fr = FastResearch(tmp_path, "Question?", run_id="fast", config=small_config(), **fake.hooks())
    assert fr.run(stop_after="research")["status"] == "completed"
    return fr


def test_sources_list_each_page_once_and_keep_quotes_and_removed_claims_in_evidence(tmp_path):
    fake = FakeHermes(SEARCH, verdicts={"Delete this claim.": ("unsupported", None)})
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    same_page = [c for c in claims if c["page_url"] == claims[0]["page_url"]]
    assert len(same_page) >= 2
    fr._save_answer(f"Keep this claim.[C{same_page[0]['id']}]\n\n"
                    f"Delete this claim.[C{same_page[1]['id']}]\n\n"
                    f"Keep another claim.[C{same_page[1]['id']}]", claims)
    fr.check()
    answer = fr.path("answer.md").read_text()
    sources = answer.split("## Sources", 1)[1]
    citations = fr._read_json("citations.json")
    assert len(citations) == 2
    source_rows = [row for row in sources.splitlines() if row.strip()]
    assert len(source_rows) == 1 and source_rows[0].endswith(citations[0]["source_url"])
    assert "[1][2]" in sources
    assert "Quote:" not in answer and "Saved page:" not in answer
    assert "Removed because" not in answer and "Delete this claim." not in answer
    evidence = fr.path("evidence.md").read_text()
    for cit in citations:
        assert f"## [{cit['n']}]" in evidence
        assert f'Quote: "{cit["quote"]}"' in evidence
        assert cit["source_url"] in evidence and cit["page_path"] in evidence
    assert "## Removed because the sources did not support them" in evidence
    assert "Delete this claim." in evidence
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


PART = "Answers part of the question. Still missing:"
ANSWERED = "Answers the question. Notes are at the end."
NOT_REVIEWED = "Not reviewed. Check the sources before relying on it."
MISSING = ["First missing part.[999]", "Second missing part.", "Third missing part."]


# The verdict never sets the opening line. Only a usable review and its missing parts do.
@pytest.mark.parametrize("verdict,independent,missing,line", [
    ("accept", True, MISSING, PART),
    ("reject", True, MISSING, PART),
    ("accept_with_notes", True, [], ANSWERED),
    ("reject", True, [], ANSWERED),
    ("reject", True, ["", "   "], ANSWERED),
    ("accept", False, MISSING, NOT_REVIEWED),
    (None, True, MISSING, NOT_REVIEWED),
])
def test_readiness_leads_answer_and_cli_exposes_readiness_and_review_path(
        tmp_path, monkeypatch, capsys, verdict, independent, missing, line):
    import fast_research
    import deepest_research
    fake = FakeHermes(SEARCH)
    fr = prepared_fast(tmp_path, fake)
    fr.answer()
    fr.check()
    if verdict:
        fr.write_json("review.json", {"verdict": verdict, "different_vendor": independent,
                                     "review": {"verdict": verdict, "missing_parts": missing}})
    fr.finish()
    text = fr.path("answer.md").read_text()
    assert text.splitlines()[2] == line
    pre_checks = text.split("## Checks", 1)[0]
    assert "Sentence check:" not in pre_checks and "Citation recall:" not in pre_checks
    if line == PART:
        # Every missing part is listed, not just the first two.
        assert r"- First missing part.\[999\]" in pre_checks and "- Second missing part." in pre_checks
        assert "- Third missing part." in pre_checks
    else:
        assert "missing part." not in pre_checks
    assert text.index("## Checks") < text.index("## Sources")
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"
    fr.finish()
    assert fr.path("answer.md").read_text() == text
    rec = fr.record()
    assert rec["readiness"] == line and rec["review_path"] == str(fr.path("review.json"))
    monkeypatch.setattr(fast_research.FastResearch, "run", lambda self: rec)
    review_round = {"ran": False, "skipped": "fixture", "questions": [], "branches": []}
    monkeypatch.setattr(deepest_research.DeepestResearch, "run", lambda self: {**rec, "review_round": review_round})
    for cli in (fast_research.main, deepest_research.main):
        cli(["run", "--question", fr.question, "--root", str(tmp_path), "--run-id", fr.run_id])
        summary = json.loads(capsys.readouterr().out)
        assert summary["readiness"] == line and summary["review_path"] == rec["review_path"]
        if cli is deepest_research.main:
            assert summary["review_round"] == review_round


@pytest.mark.parametrize("bullet", ["-", "1.", "1)"])
def test_inference_sentences_use_paragraph_or_bullet_citations_and_keep_prefix_when_narrowed(tmp_path, bullet):
    full = "My inference: tree research needs a bounded plan."
    partial = "My inference: setup needs care."
    bad = "My inference: a competing product owns this."
    alone = "My inference: this has no nearby evidence."
    separate = "My inference: a different bullet cannot borrow evidence."
    fake = FakeHermes(SEARCH, verdicts={
        full: ("supported", None), partial: ("partly", "Setup merits care."),
        bad: ("unsupported", None), alone: ("supported", None), separate: ("supported", None)})
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    cid = claims[0]["id"]
    body = (f"Tree research branches.[C{cid}] {full}\n\n"
            f"{bullet} Setup needs planning.[C{cid}]\n  {partial}\n\n"
            f"Attribution lacks support.[C{cid}] {bad}\n\n{alone}\n\n"
            f"{bullet} Only this bullet cites evidence.[C{cid}]\n{bullet} {separate}")
    fr._save_answer(body, claims)
    fr.check()
    rows = fr._read_json("sentence_check.json")["sentences"]
    inferences = [s for s in rows if s["text"].startswith("My inference:")]
    assert len(inferences) == 5
    assert [s["verdict"] for s in inferences] == ["supported", "partly", "unsupported", "unsupported", "unsupported"]
    prompt = fake.last_prompt["sentence_check"]
    assert full in prompt and partial in prompt and alone not in prompt
    # The indented continuation belongs to its bullet. The next bullet does not.
    assert inferences[1]["cites"] and not inferences[-1]["cites"]
    answer = fr.path("answer.md").read_text()
    assert full in answer and "My inference: Setup merits care." in answer
    assert bad not in answer and alone not in answer and separate not in answer
    assert bullet not in [line.strip() for line in answer.splitlines()]
    evidence = fr.path("evidence.md").read_text()
    assert bad in evidence and alone in evidence and separate in evidence
    counts = fr.record()["sentence_check_counts"]
    assert counts["inference_checked"] == 5
    assert counts["inference_narrowed"] == 1 and counts["inference_removed"] == 3
    assert counts["inference_unchecked"] == 0
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


def controller(tmp_path, fake, **kwargs):
    return DeepestResearch(tmp_path, "How does deep research work?", run_id=kwargs.pop("run_id", "quality"),
                           config=kwargs.pop("config", small_config()), **fake.hooks(), **kwargs)


def test_one_synthesis_receives_every_admitted_finding_with_original_question(tmp_path):
    fake = FakeHermes(SEARCH)
    fake.followups = QUESTIONS
    fr = controller(tmp_path, fake)
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    assert fake.count("synthesis") == 1
    assert fake.count("section") == fake.count("opening") == fake.count("writer") == 0
    prompt = fake.last_prompt["synthesis"]
    assert f"Question: {fr.question}" in prompt
    claims = fr._admitted()
    assert {c["run_id"] for c in claims} == set(fr.source_run_ids())
    for claim in claims:
        question = fr.store.get_run(claim["run_id"])["question"]
        assert f"Original question: {question}\n{fr._findings([claim])}" in prompt
    assert "parts of the main question, not the follow-ups" in prompt
    assert "conflicting facts about the same thing" in prompt and "cites both findings" in prompt
    assert "Never call agreement, a gap or a single source a disagreement" in prompt
    assert not hasattr(P, "SECTION") and not hasattr(P, "OPENING")


def test_followups_target_one_blocking_gap_and_default_to_one_wave(tmp_path):
    fake = FakeHermes(SEARCH)
    fake.followups = [{"question": f"Which unique setting {i} matters?", "impact": 3} for i in range(6)]
    fr = controller(tmp_path, fake)
    assert fr.run(stop_after="questions")["status"] == "completed"
    assert fr.dcfg["max_questions"] == fr.dcfg["parallel_branches"] == 4
    assert len(fr._read_json("questions.json")["questions"]) == 4
    assert len(pick_questions(fake.followups, fr.question)) == 4
    prompt = fake.last_prompt["followups"]
    for rule in ("one thing, not a bundle", "stop a direct answer", "settings, commands, versions, prices, dates",
                 "products, people or candidates", "Do not widen the topic", "up to 4"):
        assert rule in prompt
    cfg = small_config()
    cfg.pop("deepest")
    fallback = controller(tmp_path / "fallback", fake, config=cfg)
    assert fallback.dcfg["max_questions"] == fallback.dcfg["parallel_branches"] == 4


@pytest.mark.parametrize("run_id", ["quality", None], ids=["explicit-id", "same-command"])
@pytest.mark.parametrize("invent_finding", [False, True], ids=["completed-parent", "stopped-parent"])
def test_resume_retries_only_failed_followups_and_rebuilds_cached_answer_stages(
        tmp_path, monkeypatch, run_id, invent_finding):
    import fast_research
    ids = itertools.count()
    monkeypatch.setattr(fast_research, "default_run_id", lambda *args: f"deepest-quality-{next(ids)}")
    fake = FakeHermes(SEARCH, invent_finding=invent_finding)
    fake.followups = QUESTIONS
    attempts = Counter()
    research = FastResearch.research

    def fail_after_saved_pages(self):
        attempts[self.question] += 1
        result = research(self)
        if self.question == QUESTIONS[0]["question"] and attempts[self.question] == 1:
            raise RuntimeError("temporary branch failure")
        return result

    monkeypatch.setattr(FastResearch, "research", fail_after_saved_pages)
    fr = controller(tmp_path, fake, run_id=run_id)
    original = fr.oneshot
    cache = {}

    def cached_worker(model, prompt, **kwargs):
        workdir = Path(kwargs["workdir"])
        marker = workdir / "fixture-cache"
        if marker.exists():
            return cache[str(workdir)]
        result = original(model, prompt, **kwargs)
        marker.write_text("saved")
        cache[str(workdir)] = result
        return result

    fr.oneshot = cached_worker
    first = fr.run()
    assert first["status"] == ("stopped" if invent_finding else "completed"), first["error"]
    if invent_finding:
        assert "C9999" in first["error"]
        assert "[C9999]" in fr.path("answer_draft.md").read_text()
        assert not fr.path("answer.md").exists()
        assert fake.count("sentence_check") == fake.count("review") == 0
    assert [b["status"] for b in first["branches"]] == ["failed", "completed"]
    assert attempts[QUESTIONS[0]["question"]] == 1  # no automatic retry
    completed = first["branches"][1]
    pages = [dict(p) for p in fr.store.conn().execute("SELECT * FROM pages ORDER BY url")]
    counts = {kind: fake.count(kind) for kind in ("followups", "plan", "search", "fetch", "read", "check")}
    output_counts = {kind: fake.count(kind) for kind in ("synthesis", "sentence_check", "review")}
    phases = {name: fr.store.phase(fr.run_id, name) for name in ("answer", "check", "review", "finish")}
    previous_id = fr.run_id
    fake.invent_finding = False
    fr = controller(tmp_path, fake, run_id=run_id)
    assert fr.run_id == previous_id
    fr.oneshot = cached_worker
    second = fr.run()
    assert second["status"] == "completed", second["error"]
    assert [b["status"] for b in second["branches"]] == ["completed", "completed"]
    assert attempts == {fr.question: 1, QUESTIONS[0]["question"]: 2, QUESTIONS[1]["question"]: 1}
    assert second["branches"][1] == completed
    assert pages == [dict(p) for p in fr.store.conn().execute("SELECT * FROM pages ORDER BY url")]
    assert counts == {kind: fake.count(kind) for kind in counts}
    assert {kind: fake.count(kind) for kind in output_counts} == {kind: n + 1 for kind, n in output_counts.items()}
    for name, prior in phases.items():
        current = fr.store.phase(fr.run_id, name)
        assert current is not None
        assert current["status"] == "done"
        if prior:
            assert current["ended_at"] > prior["ended_at"]
    sessions = len(fake.sessions)
    assert fr.run()["status"] == "completed"
    assert len(fake.sessions) == sessions


@pytest.mark.parametrize("controller_class", [FastResearch, DeepestResearch], ids=["fast", "deepest"])
def test_stopped_run_without_failed_followups_is_not_implicitly_resumed(tmp_path, monkeypatch, controller_class):
    import fast_research
    ids = itertools.count()
    monkeypatch.setattr(fast_research, "default_run_id", lambda question, prefix: f"{prefix}-quality-{next(ids)}")
    fake = FakeHermes(SEARCH, invent_finding=True)
    fr = controller_class(tmp_path, "Question?", config=small_config(), **fake.hooks())
    first = fr.run()
    assert first["status"] == "stopped" and "C9999" in first["error"]
    assert not first.get("branches")
    resumed = controller_class(tmp_path, fr.question, config=small_config(), **fake.hooks())
    assert resumed.run_id != fr.run_id


@pytest.mark.parametrize("deepest", [False, True], ids=["fast", "deepest"])
def test_writer_prompts_bound_length_and_evidence_based_inferences(tmp_path, deepest):
    fake = FakeHermes(SEARCH)
    fr = controller(tmp_path, fake) if deepest else FastResearch(
        tmp_path, "Question?", run_id="fast", config=small_config(), **fake.hooks())
    assert fr.run()["status"] == "completed"
    prompt = fake.last_prompt["synthesis" if deepest else "writer"]
    assert "1,500 to 2,500 words" in prompt
    assert 'at most three "My inference:" sentences' in prompt
    assert "never guess a name, setting, price, version or who said or owns something" in prompt
    assert "same paragraph or bullet" in prompt


@pytest.mark.parametrize("deepest", [False, True], ids=["fast", "deepest"])
def test_date_rules_reach_reading_checking_and_writing_workers(tmp_path, deepest):
    # A second run reads saved pages, so both extraction prompts are exercised.
    warm = FakeHermes(SEARCH)
    assert FastResearch(tmp_path, "Warm pages?", run_id="warm", config=small_config(),
                        **warm.hooks()).run(stop_after="research")["status"] == "completed"
    fake = FakeHermes({**SEARCH, "q2": ["https://example.invalid/new"]})
    fr = controller(tmp_path, fake) if deepest else FastResearch(
        tmp_path, "Question?", run_id="fast", config=small_config(), **fake.hooks())
    assert fr.run()["status"] == "completed"
    for kind in ("fetch", "read", "check", "sentence_check", "synthesis" if deepest else "writer"):
        prompt = fake.last_prompt[kind]
        assert "date" in prompt, kind
        if kind in ("fetch", "read"):
            assert "available, live, supported, released or priced" in prompt
            assert "choose a quote that includes the date" in prompt
            assert "quote gives no date" in prompt
        elif kind in ("check", "sentence_check"):
            assert 'only "partly" supported' in prompt
            assert "current status as fact" in prompt
            assert "quote gives no date" in prompt
        else:
            assert "current availability, support or price" in prompt
            assert "source date" in prompt and "an undated page says" in prompt


# Round-four review regressions.
def test_date_narrowing_never_adds_a_date_missing_from_the_quote():
    from fast_research import parse_checks
    assert "choose a quote that includes the date" in P._CLAIM_RULES
    assert "If the quote cannot include it, say the quote gives no date" in P._CLAIM_RULES
    for prompt in (P.CHECK, P.SENTENCE_CHECK):
        assert "If the quote gives no date, the narrowed claim says the quote gives no date" in prompt
        assert "Never add a date the quote lacks" in prompt
        assert "names the source date" not in prompt
    quote = "Feature X is now available to all paying users in every region"
    claims = {1: {"quote": quote}, 2: {"quote": quote}}
    parsed = {"checks": [
        {"n": 1, "verdict": "partly", "supported_claim": "A May 2026 vendor page says Feature X is available."},
        {"n": 2, "verdict": "partly", "supported_claim": "The vendor quote gives no date for Feature X being available."}]}
    results = parse_checks(parsed, claims, [1, 2])
    assert results[0][0] == "unsupported"  # Keep the existing number guard.
    assert results[1][0] == "partly"


@pytest.mark.parametrize("verdict", [["accept"], {"overall": "accept"}])
@pytest.mark.parametrize("deepest", [False, True], ids=["fast", "deepest"])
def test_unhashable_review_verdict_completes_as_not_reviewed(tmp_path, verdict, deepest):
    fake = FakeHermes(SEARCH)
    original = fake.oneshot

    def oneshot(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if "independent reviewer" in prompt:
            res.stdout = json.dumps({"verdict": verdict, "missing_parts": ["Do not present this as usable."]})
        return res

    fake.oneshot = oneshot
    fr = controller(tmp_path, fake) if deepest else FastResearch(
        tmp_path, "Question?", run_id="fast", config=small_config(), **fake.hooks())
    rec = fr.run()
    assert rec["status"] == "completed", rec["error"]
    run = fr.store.get_run(fr.run_id)
    assert run is not None and run["status"] == "completed"
    assert rec["readiness"] == NOT_REVIEWED
    assert fr.record()["readiness"] == rec["readiness"]
    assert fr.path("answer.md").read_text().splitlines()[2] == rec["readiness"]
    assert "Do not present this as usable." not in fr.path("answer.md").read_text()


@pytest.mark.parametrize("prompt", [P.ANSWER, P.SYNTHESIS], ids=["fast", "deepest"])
def test_writer_must_label_reasoning_and_omit_it_beyond_the_cap(prompt):
    assert 'Start any sentence that reasons beyond the findings with "My inference:"' in prompt
    assert "Leave out any further reasoning" in prompt
    assert 'at most three "My inference:" sentences' in prompt


@pytest.mark.parametrize("empty_replies", [1, 2])
def test_synthesis_reuses_two_attempt_writer_loop(tmp_path, empty_replies):
    fake = FakeHermes(SEARCH)
    fake.followups = QUESTIONS
    original = fake.oneshot
    folders = []

    def oneshot(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if prompt.startswith("You write one combined"):
            folders.append(Path(kwargs["workdir"]).name)
            if len(folders) <= empty_replies:
                res.stdout = "" if len(folders) == 1 else "An answer without finding ids."
        return res

    fake.oneshot = oneshot
    fr = controller(tmp_path, fake)
    rec = fr.run()
    assert folders == ["attempt-1", "attempt-2"]
    assert fake.count("synthesis") == 2
    assert fake.count("plan") == 1 + len(QUESTIONS)
    assert rec["status"] == ("completed" if empty_replies == 1 else "stopped")
    if empty_replies == 2:
        assert "no answer with finding ids" in rec["error"]
    else:
        assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


@pytest.mark.parametrize("prefix", ["**My inference:**", "my inference:", "**Recovery.** My inference:",
                                    "***mY inference:***", "**Recovery.** **my inference:**"])
def test_inference_prefix_variants_are_checked_and_removed(tmp_path, prefix):
    inference = f"{prefix} a rival vendor owns this product."
    fake = FakeHermes(SEARCH, verdicts={inference: ("unsupported", None)})
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    fr._save_answer(f"Fact.[C{claims[0]['id']}] {inference}", claims)
    fr.check()
    checked = fr._read_json("sentence_check.json")
    assert checked is not None
    rows = checked["sentences"]
    assert any(s["inference"] and s["verdict"] == "unsupported" and s["text"] == inference for s in rows)
    assert inference in fake.last_prompt["sentence_check"]
    assert inference not in fr.path("answer.md").read_text()
    assert inference in fr.path("evidence.md").read_text()
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


@pytest.mark.parametrize("prefix", ["my inference:", "My inference:", "**MY INFERENCE:**"])
def test_narrowed_inference_has_one_prefix_and_no_added_citations(tmp_path, prefix):
    inference = "My inference: setup needs care."
    fake = FakeHermes(SEARCH, verdicts={inference: ("partly", f"{prefix} Setup merits care.")})
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    fr._save_answer(f"Fact.[C{claims[0]['id']}] {inference}", claims)
    fr.check()
    first = fr.path("answer.md").read_text()
    assert "My inference: Setup merits care." in first
    assert first.count("inference:") == 1
    assert "My inference: Setup merits care.[" not in first
    fr.check()
    assert fr.path("answer.md").read_text() == first
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


def test_citation_recall_and_main_counts_exclude_inferences(tmp_path):
    inference = "My inference: a rival owns the product."
    fake = FakeHermes(SEARCH, verdicts={inference: ("unsupported", None)})
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    fr._save_answer(f"Fact.[C{claims[0]['id']}] {inference}", claims)
    fr.check()
    grades = fr._read_json("grades.json")
    assert grades is not None
    assert grades["citation_recall"] == 1.0
    assert grades["sentence_check_counts"] == {
        "checked": 1, "narrowed": 0, "removed": 0, "unchecked": 0,
        "inference_checked": 1, "inference_narrowed": 0, "inference_removed": 1, "inference_unchecked": 0}


def test_kept_inference_does_not_gain_markers_when_its_context_fact_is_removed(tmp_path):
    fact = "Tree research branches."
    inference = "My inference: tree research needs a bounded plan."
    fake = FakeHermes(SEARCH, verdicts={fact: ("unsupported", None), inference: ("supported", None)})
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    first, second = claims[:2]
    fr._save_answer(f"{fact}[C{first['id']}] {inference}\n\nAnother fact.[C{second['id']}]", claims)
    fr.check()
    answer = fr.path("answer.md").read_text()
    assert inference in answer and inference + "[" not in answer
    assert fact not in answer
    citations = fr._read_json("citations.json")
    assert citations is not None
    assert [c["claim_id"] for c in citations] == [second["id"]]
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


def test_source_pages_are_markdown_list_items(tmp_path):
    fake = FakeHermes(SEARCH)
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    fr._save_answer("\n\n".join(f"Fact.[C{c['id']}]" for c in claims), claims)
    sources = fr.path("answer.md").read_text().split("\n## Sources\n", 1)[1]
    rows = [line for line in sources.splitlines() if line.strip()]
    citations = fr._read_json("citations.json")
    assert citations is not None
    assert len(rows) == len({c["source_url"] for c in citations}) >= 2
    assert all(line.startswith("- [") for line in rows)
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


def test_checks_link_evidence_by_name_and_remain_idempotent(tmp_path):
    fake = FakeHermes(SEARCH)
    fr = FastResearch(tmp_path, "Question?", run_id="fast", config=small_config(), **fake.hooks())
    assert fr.run()["status"] == "completed"
    text = fr.path("answer.md").read_text()
    checks = text.split("\n## Checks\n", 1)[1].split("\n## Sources", 1)[0]
    assert "[evidence.md](evidence.md)" in checks
    fr.finish()
    assert fr.path("answer.md").read_text() == text
    assert check_answer_files(tmp_path, fr.run_id)["verdict"] == "pass"


def test_retry_archives_delivered_files_before_a_stopped_rebuild(tmp_path, monkeypatch):
    fake = FakeHermes(SEARCH)
    fake.followups = QUESTIONS
    research = FastResearch.research
    failed = []

    def fail_once(self):
        result = research(self)
        if self.question == QUESTIONS[0]["question"] and not failed:
            failed.append(True)
            raise RuntimeError("temporary branch failure")
        return result

    monkeypatch.setattr(FastResearch, "research", fail_once)
    fr = controller(tmp_path, fake)
    assert fr.run()["status"] == "completed"
    names = ("answer.md", "evidence.md", "review.json", "citations.json", "answer.pre-check.md",
             "citations.pre-check.json", "sentence_check.json", "grades.json", "not_verified.json", "quote_check.json")
    delivered = {name: fr.path(name).read_bytes() for name in names}
    fake.invent_finding = True
    assert controller(tmp_path, fake).run()["status"] == "stopped"
    for name, content in delivered.items():
        archived = list(fr.dir.glob(f"{name}.legacy-*"))
        assert len(archived) == 1 and archived[0].read_bytes() == content
    assert not fr.path("answer.md").exists()
    assert "C9999" in fr.path("answer_draft.md").read_text()
    assert list(fr.dir.glob("answer.legacy-*"))


def test_retry_docs_distinguish_failed_from_stopped_and_explain_starting_fresh():
    doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text()
    retry = doc.split("### Retry failed follow-ups", 1)[1].split("## Checks", 1)[0]
    assert "failed, not stopped" in retry
    assert "new `--run-id` to start fresh" in retry


@pytest.mark.parametrize("with_fact", [False, True], ids=["no-batch", "mixed-batch"])
def test_inference_without_nearby_citation_is_removed_without_checker_input(tmp_path, with_fact):
    alone = "My inference: this has no nearby evidence."
    fake = FakeHermes(SEARCH)
    fr = prepared_fast(tmp_path, fake)
    claims = fr._admitted()
    body = (f"Fact.[C{claims[0]['id']}]\n\n" if with_fact else "") + alone
    fr._save_answer(body, claims)
    fr.check()
    assert alone not in fake.last_prompt.get("sentence_check", "")
    assert fake.count("sentence_check") == int(with_fact)
    checked = fr._read_json("sentence_check.json")
    assert checked is not None
    rows = checked["sentences"]
    assert rows[-1]["text"] == alone and rows[-1]["verdict"] == "unsupported"
    assert rows[-1]["reason"] == "no citations in the same paragraph or bullet"
    assert alone not in fr.path("answer.md").read_text()
    assert alone in fr.path("evidence.md").read_text()


def test_synthesis_prompt_has_no_fast_only_placeholders_and_renderer_docstring_is_current():
    assert "{topics}" not in P.SYNTHESIS and "{gaps}" not in P.SYNTHESIS
    docstring = FastResearch._save_answer.__doc__
    assert docstring is not None
    assert "combined sections" not in docstring
    assert "combined answers" in docstring
