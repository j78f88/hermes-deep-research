import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest  # noqa: E402

import fast_quote_check as qc  # noqa: E402
from fast_store import Store  # noqa: E402

PAGE = ("# GPT Researcher\n\nDeep Research uses a **tree-like exploration** pattern, with configurable "
        "depth and breadth.\n\nIt runs on \u201cany LLM\u201d supported by LiteLLM.\n")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "fast.db")
    s.create_run("r1", "Q?")
    return s


def _claim(store, quote, url="https://example.com/p"):
    page = store.get_page(url) or store.save_page(url, PAGE, title="P")
    return store.add_claim("r1", "j1", "a claim", quote, page["url"], page["sha256"])


def test_verbatim_quote_passes_ignoring_case_space_markup_and_curly_quotes(store):
    cid = _claim(store, 'uses a tree-like   exploration pattern, with configurable depth')
    assert qc.check_claim(store, cid) == (True, "ok")
    cid2 = _claim(store, 'It runs on "any LLM" supported by LiteLLM.')
    assert qc.check_claim(store, cid2)[0]
    assert store.claim(cid)["check_status"] == "pass"


def test_quote_not_in_page_is_rejected(store):
    cid = _claim(store, "uses a graph-like exploration pattern")
    assert qc.check_claim(store, cid) == (False, "quote_not_found")
    assert store.claim(cid)["check_status"] == "fail"


def test_short_quote_rejected(store):
    assert qc.check_claim(store, _claim(store, "depth")) == (False, "quote_too_short")


def test_changed_page_text_rejected(store):
    cid = _claim(store, "tree-like exploration pattern")
    with store.tx() as c:
        c.execute("UPDATE pages SET text = text || ' extra'")
    assert qc.check_claim(store, cid) == (False, "page_changed")


def test_changed_page_file_rejected(store, tmp_path):
    cid = _claim(store, "tree-like exploration pattern")
    page = store.get_page("https://example.com/p")
    (tmp_path / page["path"]).write_text("tampered")
    assert qc.check_claim(store, cid) == (False, "page_file_changed")


def test_claim_without_page_binds_to_the_candidate_that_holds_the_quote(store):
    other = store.save_page("https://example.com/other", "nothing relevant here at all")
    right = store.save_page("https://example.com/right", PAGE)
    cid = store.add_claim("r1", "j2", "a claim", "configurable depth and breadth", None, None)
    assert qc.check_claim(store, cid, [other, right])[0]
    assert store.claim(cid)["page_url"] == right["url"]


def _citation(store, cid, n):
    c = store.claim(cid)
    return {"n": n, "claim_id": cid, "quote": c["quote"], "page_url": c["page_url"], "page_sha256": c["page_sha256"]}


def test_answer_check_passes_and_fails_for_each_rule(store):
    good = _claim(store, "tree-like exploration pattern")
    bad = _claim(store, "this sentence is not on the page")
    qc.check_claim(store, good)
    qc.check_claim(store, bad)
    text = "It explores like a tree.[1]\n\n## Sources\n\n1. P [1]"
    ok = qc.check_answer(store, "r1", text, [_citation(store, good, 1)])
    assert ok["verdict"] == "pass" and ok["passed"] == 1

    assert qc.check_answer(store, "r1", "A.[1] B.[2]", [_citation(store, good, 1)])["problems"][0]["kind"] == "marker_without_source"
    assert qc.check_answer(store, "r1", "A.[1]", [_citation(store, good, 1), _citation(store, good, 2)])["problems"][0]["kind"] == "source_never_cited"
    assert qc.check_answer(store, "r1", "A.[1]", [_citation(store, bad, 1)])["problems"][0]["kind"] == "claim_not_passed"
    altered = dict(_citation(store, good, 1), quote="tree-like exploration")
    assert qc.check_answer(store, "r1", "A.[1]", [altered])["problems"][0]["kind"] == "citation_differs_from_claim"
    assert qc.check_answer(store, "r1", "A.", [])["verdict"] == "fail"
    store.create_run("r2", "Other?")
    assert qc.check_answer(store, "r2", "A.[1]", [_citation(store, good, 1)])["problems"][0]["kind"] == "unknown_claim"


def test_answer_check_catches_a_page_edited_after_the_claim_passed(store):
    good = _claim(store, "tree-like exploration pattern")
    qc.check_claim(store, good)
    with store.tx() as c:
        c.execute("UPDATE pages SET text = 'gone', sha256 = ?", ("0" * 64,))
    result = qc.check_answer(store, "r1", "A.[1]", [_citation(store, good, 1)])
    assert result["verdict"] == "fail"


def test_cli_reads_run_files(store, tmp_path):
    good = _claim(store, "tree-like exploration pattern")
    qc.check_claim(store, good)
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)
    (run_dir / "answer.md").write_text("It explores like a tree.[1]\n")
    (run_dir / "citations.json").write_text(json.dumps([_citation(store, good, 1)]))
    assert qc.main(["--root", str(tmp_path), "--run-id", "r1", "--write"]) == 0
    assert json.loads((run_dir / "quote_check.json").read_text())["verdict"] == "pass"
    (run_dir / "answer.md").write_text("It explores like a tree.[1] Also this.[2]\n")
    assert qc.main(["--root", str(tmp_path), "--run-id", "r1"]) == 3
