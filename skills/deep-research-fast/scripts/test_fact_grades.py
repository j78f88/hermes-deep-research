"""Offline checks for the documented Admiralty mapping and markers."""
import pytest

import fact_grades as fg


@pytest.mark.parametrize("kind,verdict,code", [
    ("self", "supported", "B2"), ("primary", "supported", "B2"),
    ("independent", "supported", "C2"), ("discovery", "supported", "D2"),
    ("unknown", "supported", "F2"), ("self", "partly", "B3"),
])
def test_grade_mapping(kind, verdict, code):
    g = fg.grade([{"kind": kind, "verdict": verdict, "origin": "one"}])
    assert g["code"] == code
    assert g["letter"] != "A" and g["number"] not in (4, 5)


def test_two_reposts_are_one_origin():
    citations = [{"n": i, "page_url": f"https://site{i}.invalid/post", "quote": "the original quote"}
                 for i in (1, 2)]
    origins = fg.origins(citations, lambda quote, url: True)
    assert len(set(origins.values())) == 1
    assert fg.grade([{"kind": "independent", "verdict": "supported", "origin": o}
                     for o in origins.values()])["code"] == "C2"


def test_two_independent_sites_agree():
    citations = [{"n": i, "page_url": f"https://site{i}.invalid/post", "quote": str(i)} for i in (1, 2)]
    origins = fg.origins(citations, lambda quote, url: False)
    assert fg.grade([{"kind": "independent", "verdict": "supported", "origin": o}
                     for o in origins.values()])["code"] == "C1"


def test_partial_second_origin_is_not_full_confirmation():
    assert fg.grade([{"kind": "self", "verdict": "supported", "origin": "one"},
                     {"kind": "independent", "verdict": "partly", "origin": "two"}])["code"] == "B2"


@pytest.mark.parametrize("text,marked", [
    ("Set OPENROUTER_API_KEY.", True), ("This is a plain sentence.", False),
    ("Set OPENROUTER_API_KEY.[1]", False), ("Use `python run.py`.", True),
    ("Use --root to choose a folder.", True), ("Use version 3.11.", True),
    ("Read https://example.invalid/docs.", True),
])
def test_not_verified(text, marked):
    out, items = fg.mark_not_verified(text)
    assert (fg.NOT_VERIFIED in out) is marked
    assert bool(items) is marked
    assert fg.mark_not_verified(out)[0] == out


@pytest.mark.parametrize("text", ["First fact.[1] Second fact.[2]", "First fact. [1] Second fact. [2]"])
def test_sentence_markers_stay_with_sentence(text):
    rows = fg.split_sentences(text)
    assert len(rows) == 2
    assert [s["cites"] for s in rows] == [[1], [2]]


def test_recall_uses_all_cited_sentences_before_repairs():
    assert fg.citation_recall(["supported", "partly", "unsupported", "unchecked"]) == 0.25
    assert fg.citation_recall([]) is None


def test_low_grade_and_summary():
    rows = [{**fg.grade([{"kind": "discovery", "verdict": "supported", "origin": "one"}])}]
    assert fg.is_low(rows[0]) and rows[0]["code"] == "D2"
    assert fg.summary_line(fg.grade_counts(rows)) == "D2 1"
    assert fg.grade([])["code"] == "F6"
