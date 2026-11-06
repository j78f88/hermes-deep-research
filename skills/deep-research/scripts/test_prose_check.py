"""Public, offline regressions for the local mechanical prose checker."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent


def load(name="prose_check"):
    path = SCRIPTS / f"{name}.py"
    assert path.is_file(), f"local checker is missing: {path.name}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sentence(words):
    return " ".join(["word"] * words) + "."


def test_plain_text_passes_with_compatible_boolean_and_output(capsys):
    assert load().check("sample", "The trial worked well. We need more evidence.") is True
    output = capsys.readouterr().out
    assert "sample" in output
    assert "PASS" in output
    assert "FAIL:" not in output


@pytest.mark.parametrize("words, passed", [(16, True), (17, False)])
def test_average_sentence_limit(words, passed, capsys):
    assert load().check("average", sentence(words)) is passed
    output = capsys.readouterr().out
    if not passed:
        assert "average sentence too long" in output


@pytest.mark.parametrize("words, passed", [(25, True), (26, True), (35, True), (36, False)])
def test_long_sentence_advice_and_hard_limit(words, passed, capsys):
    # Short sentences keep the average below its limit, isolating the hard cap.
    text = sentence(words) + "\n" + "\n".join([sentence(3)] * 4)
    assert load().check("length", text) is passed
    output = capsys.readouterr().out
    assert (f"[{words}w] " in output) is (words > 25)
    assert ("sentence(s) over 35 words" in output) is (words > 35)


@pytest.mark.parametrize("raw, reason", [
    ("The trial worked; more evidence is needed.", "semicolons"),
    ("The trial worked — more evidence is needed.", "em dashes"),
    ("- The trial worked.\n  - More evidence is needed.", "nested bullets"),
    ("- The trial worked.\n\t- More evidence is needed.", "nested bullets"),
    ("The trial used red · blue · green · yellow labels.", "middle-dot chains in prose"),
])
def test_forbidden_density_markers_fail(raw, reason, capsys):
    assert load().check("markers", raw) is False
    assert reason in capsys.readouterr().out


def test_multiple_failures_remain_semicolon_delimited(capsys):
    assert not load().check("combined", "The trial worked; evidence — remains thin.")
    fail = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("FAIL: "))
    assert "semicolons" in fail[6:].split("; ")
    assert "em dashes" in fail[6:].split("; ")


def test_two_middle_dots_are_allowed():
    assert load().check("dots", "The trial used red · blue · green labels.")


@pytest.mark.parametrize("label", ["Why it matters", "If you do nothing", "Suggested", "Urgency", "Sources"])
def test_fixed_c1_fields_can_be_indented(label):
    assert load().check("decision", f"1. **Should the trial wait?**\n   - {label}: More evidence is needed.")


def test_c1_exemption_does_not_skip_its_prose(capsys):
    assert not load().check("decision", "   - Suggested: Yes; more evidence is needed.")
    assert "semicolons" in capsys.readouterr().out


@pytest.mark.parametrize("label", ["Other", "Suggested elsewhere", "Sources and notes"])
def test_other_indented_fields_still_fail(label, capsys):
    assert not load().check("nested", f"   - {label}: More evidence is needed.")
    assert "nested bullets" in capsys.readouterr().out


@pytest.mark.parametrize("length, count, passed", [(11, 2, True), (12, 1, True), (12, 2, False)])
def test_parenthetical_length_and_default_allowance(length, count, passed, capsys):
    text = "\n".join([f"The trial worked ({'x' * length})."] * count)
    assert load().check("asides", text) is passed
    if not passed:
        assert "long parentheticals (max 1)" in capsys.readouterr().out


@pytest.mark.parametrize("word_total, passed", [(499, False), (500, True)])
def test_parenthetical_allowance_scales_per_250_words(word_total, passed, capsys):
    rows = ["The trial worked (extra context here)."] * 2
    used = 12  # Six words per parenthetical sentence.
    remaining = word_total - used
    # All sentences have at least three words and stay below the average limit.
    while remaining > 16:
        rows.append(sentence(10))
        remaining -= 10
    rows.append(sentence(remaining))
    assert load().check("asides", "\n".join(rows)) is passed
    output = capsys.readouterr().out
    if not passed:
        assert "long parentheticals (max 1)" in output


@pytest.mark.parametrize("packed", [
    "The trial used colours: red, blue, green.",
    "The trial used 1 and 2 and 3 and 4.",
    "We checked ABC-1 and ABC-2 and ABC-3 and ABC-4.",
])
@pytest.mark.parametrize("count, total, passed", [(1, 1, True), (2, 19, False), (2, 20, True), (3, 20, False)])
def test_packed_sentence_allowance(packed, count, total, passed, capsys):
    text = "\n".join([packed] * count + ["The trial worked."] * (total - count))
    assert load().check("packed", text) is passed
    if not passed:
        assert "packed sentences (max" in capsys.readouterr().out


BAD = "The trial worked; evidence — remains thin · red · blue · green."


@pytest.mark.parametrize("excluded", [
    f"---\ntitle: {BAD}\n---\n",
    f"```python\n{BAD}\n```\n",
    f"~~~text\n{BAD}\n~~~\n",
    f"```text\n{BAD}\n",  # An unclosed fence still contains no reader prose.
    f"| Notes |\n| --- |\n| {BAD} |\n",
    f"Notes | Detail\n--- | ---\nTrial | {BAD}\n",
    f"> {BAD}\n>   - Nested quotation.\n",
    f"`{BAD}`\n",
    f"``{BAD} ` literal``\n",
    "https://example.invalid/a;b—c·d·e·f\n",
    "[Read the source](https://example.invalid/a(b);c—d·e·f)\n",
    "[Read the source][trial]\n\n[trial]: https://example.invalid/a;b—c·d·e·f\n",
    f"<script>{BAD}</script>\n<style>{BAD}</style>\n",
])
def test_markdown_non_prose_is_excluded(excluded):
    prose = "The trial worked.\n\n"
    raw = excluded + prose if excluded.startswith("---\n") else prose + excluded
    assert load().check("markdown", raw)


@pytest.mark.parametrize("visible", [
    f"[{BAD}](https://example.invalid)",
    f"<span>{BAD}</span>",
    f"[[archive-2024-2025-2026-2027|{BAD}]]",
    f"**{BAD}**",
])
def test_visible_labels_and_html_text_are_still_checked(visible, capsys):
    assert not load().check("visible", visible)
    assert "semicolons" in capsys.readouterr().out


def test_wikilink_targets_do_not_create_packed_sentences():
    link = "[[archive-2024-2025-2026-2027|the trial notes]]"
    assert load().check("wikilinks", f"We read {link}.\nWe checked {link}.")


def test_mechanical_checker_never_reads_a_word_list(monkeypatch):
    module = load()
    monkeypatch.setenv("PLAIN_WORDS_PATH", "/nonexistent/word-list.md")
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: pytest.fail("unexpected file read"))
    assert module.check("local", "The trial worked well.")
    # A lexical policy is deliberately not bundled. Never silently claim to run one.
    with pytest.raises(ValueError, match="jargon"):
        module.check("local", "The trial worked well.", jargon_fail=True)


def test_plain_brief_uses_local_checker_and_preserves_result_shape():
    module = load("plain_brief_rewrite")
    assert module.PROSE_CHECK == SCRIPTS / "prose_check.py"
    assert module.prose_result("The trial worked.") == {"ok": True, "fails": [], "long_sentences": []}
    result = module.prose_result(sentence(36))
    assert result["ok"] is False
    assert any("35 words" in reason for reason in result["fails"])
    assert result["long_sentences"][0].startswith("[36w] ")


@pytest.mark.parametrize("raw, status", [("The trial worked.", 0), (BAD, 1)])
def test_strict_cli_accepts_stdin(raw, status):
    path = SCRIPTS / "prose_check.py"
    assert path.is_file(), "local checker is missing"
    result = subprocess.run([sys.executable, "-B", str(path), "--strict", "-"],
                            input=raw, capture_output=True, text=True)
    assert result.returncode == status, result.stderr
    assert ("FAIL: " in result.stdout) is bool(status)
