import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fast_research import FastResearch, refresh_record  # noqa: E402
from fast_test_support import FakeHermes, small_config  # noqa: E402

U1, U2, U3, U4, U5 = (f"https://example.com/doc{i}" for i in range(1, 6))
SEARCH = {"q1": [U1, U2], "q2": [U2, U3], "q3": [U4, U1], "q4": [U5]}


def run(tmp_path, fake, question="How does it work?", **kw):
    fr = FastResearch(tmp_path, question, config=kw.pop("config", None) or small_config(), **fake.hooks(), **kw)
    return fr, fr.run()


def test_full_run_with_stub_workers(tmp_path):
    fake = FakeHermes(SEARCH, bad_quote_urls={U3})
    fr, rec = run(tmp_path, fake)
    assert rec["status"] == "completed", rec["error"]
    # every page fetched exactly once even though searches overlap
    for url in (U1, U2, U3, U4, U5):
        assert fake.count("fetch", url) == 1
    assert rec["jobs"]["by_kind"] == {"search": {"done": 4}, "fetch": {"done": 5}}
    # the bad quotes failed the check and never reach the answer
    assert rec["claims"]["fail"] == 1 and rec["claims"]["pass"] == 9
    citations = json.loads((fr.dir / "citations.json").read_text())
    assert citations and all(c["quote"] != "this quote is not anywhere on the page at all" for c in citations)
    answer = (fr.dir / "answer.md").read_text()
    assert "[C" not in answer and "## Sources" in answer and "[1]" in answer
    qc = json.loads((fr.dir / "quote_check.json").read_text())
    assert qc["verdict"] == "pass" and qc["dropped_markers"] == []
    review = json.loads((fr.dir / "review.json").read_text())
    assert review["verdict"] == "accept" and review["different_vendor"] is True
    assert review["writer_vendors"] == ["anthropic"] and review["reviewer_vendor"] == "openai"
    # run record
    saved = json.loads((fr.dir / "run.json").read_text())
    assert saved["wall_seconds"] >= 0 and saved["jobs"]["total"] == 9 and saved["jobs"]["search_fetch"] == 9
    n_calls = len(fake.sessions)
    assert abs(saved["cost_usd"] - 0.01 * n_calls) < 1e-9
    assert saved["quote_check"] == "pass" and saved["review_verdict"] == "accept"
    assert {"preflight", "plan", "research", "answer", "review"} <= set(saved["phases"])
    # each model was preflighted once before any worker ran
    assert fake.log[: 4] and all(k == "preflight" for k, _ in fake.log[:4])
    assert fake.count("preflight") == 4
    # one meaning check per page with findings whose quotes were found
    assert fake.count("check") == 5 and saved["meaning_check"] == {"supported": 9}


def test_job_limits_cap_fetches(tmp_path):
    fake = FakeHermes(SEARCH)
    fr, rec = run(tmp_path, fake, config=small_config(max_fetch_jobs=2, max_search_fetch_jobs=5))
    assert rec["status"] == "completed"
    assert rec["jobs"]["search_fetch"] <= 5
    assert sum(rec["jobs"]["by_kind"]["fetch"].values()) <= 2
    assert "over_limit" in (fr.dir / "events.log").read_text()


def test_later_run_reuses_saved_pages(tmp_path):
    first = FakeHermes(SEARCH)
    run(tmp_path, first, question="First question?")
    second = FakeHermes({"q1": [U1], "q2": [], "q3": [], "q4": []})
    fr, rec = run(tmp_path, second, question="Follow-up question?")
    assert rec["status"] == "completed"
    assert second.count("fetch") == 0 and second.count("read", U1) == 1
    assert rec["pages"]["reused_from_earlier_runs"] == 1
    assert rec["quote_check"] == "pass"


def test_same_vendor_reviewer_stops_the_run(tmp_path):
    cfg = small_config()
    cfg["models"]["reviewer"] = dict(cfg["models"]["writer"])
    fake = FakeHermes(SEARCH)
    fr, rec = run(tmp_path, fake, config=cfg)
    assert rec["status"] == "stopped" and "vendor" in rec["error"]
    assert json.loads((fr.dir / "review.json").read_text())["different_vendor"] is False


def test_no_passing_findings_stops_honestly(tmp_path, monkeypatch):
    import fast_test_support as fts
    monkeypatch.setattr(fts, "page_text", lambda url: "nothing useful on this page")
    fake = FakeHermes({"q1": [U1], "q2": [], "q3": [], "q4": []})
    fr, rec = run(tmp_path, fake)
    assert rec["status"] == "stopped" and "quote and meaning checks" in rec["error"]
    assert rec["claims"]["pass"] == 0 and rec["claims"]["fail"] == 2
    assert rec["answer_path"] is None


def test_record_refresh_keeps_the_original_wall_time(tmp_path):
    import time
    fake = FakeHermes(SEARCH)
    fr, rec = run(tmp_path, fake)
    time.sleep(0.3)
    again = refresh_record(fr)
    assert again["wall_seconds"] == rec["wall_seconds"]
    assert again["cost_usd"] == rec["cost_usd"]
