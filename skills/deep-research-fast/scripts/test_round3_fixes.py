import json
from fast_research import FastResearch
from fast_test_support import FakeHermes, small_config
from test_fast_sentence_check import prepared, sentence_reply, SEARCH


def test_kind_unknown_does_not_vote(tmp_path):
    fr, fake = prepared(tmp_path)
    other = FastResearch(tmp_path, "Another question?", run_id="other", skip_preflight=True,
                         config=small_config(), **FakeHermes(SEARCH, checker_garbage=True).hooks())
    other.run(stop_after="research")
    fr.oneshot = sentence_reply(fake, {})
    fr.check()
    assert {g["code"] for g in fr._read_json("grades.json")["sentences"]} == {"B2"}


def test_code_index_kept(tmp_path):
    fr, fake = prepared(tmp_path)
    a, b = fr._admitted()[:2]
    text, cites, _ = fr.render(f"Use `argv[2]` here.[C{a['id']}] Fact B.[C{b['id']}]", {c["id"]: c for c in fr._admitted()})
    fr.path("answer.md").write_text(text); fr.write_json("citations.json", cites)
    seen = []
    reply = sentence_reply(fake, {})
    fr.oneshot = lambda m, p, **k: (seen.append(p) if p.startswith("You check sentences") else None) or reply(m, p, **k)
    fr.check()
    ans = fr.path("answer.md").read_text()
    assert "Use `argv[2]` here.[1] Fact B.[2]" in ans, ans
    assert "Claim: Use `argv[2]` here." in seen[0]
    assert fr._read_json("quote_check.json")["verdict"] == "pass"


def test_code_index_kept_with_removal(tmp_path):
    fr, fake = prepared(tmp_path)
    a, b = fr._admitted()[:2]
    text, cites, _ = fr.render(f"Fact B.[C{b['id']}] Fact A.[C{a['id']}] Use `x[1]` here.[C{a['id']}]", {c["id"]: c for c in fr._admitted()})
    fr.path("answer.md").write_text(text); fr.write_json("citations.json", cites)
    fr.oneshot = sentence_reply(fake, {"Fact B.": "unsupported"})
    fr.check()
    ans = fr.path("answer.md").read_text()
    assert "Fact A.[1] Use `x[1]` here.[1]" in ans, ans
    assert len(fr._read_json("citations.json")) == 1
    assert fr._read_json("quote_check.json")["verdict"] == "pass"


def test_unpunctuated_narrowing(tmp_path):
    fr, fake = prepared(tmp_path)
    claims = fr._admitted()
    text, cites, _ = fr.render(f"Broad fact.[C{claims[0]['id']}] Set OPENROUTER_API_KEY.", {c["id"]: c for c in claims})
    fr.path("answer.md").write_text(text); fr.write_json("citations.json", cites)
    original = fake.oneshot
    def reply(model, prompt, **kwargs):
        res = original(model, prompt, **kwargs)
        if prompt.startswith("You check sentences"):
            res.stdout = json.dumps({"checks": [{"n": 1, "verdict": "partly", "supported_claim": "The page describes a tree of sub-questions"}]})
        return res
    fr.oneshot = reply
    fr.check()
    ans = fr.path("answer.md").read_text()
    assert "sub-questions.[1] (grade B3: usually reliable source, supported only in part) Set OPENROUTER_API_KEY. (Not verified)" in ans, ans
