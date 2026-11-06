#!/usr/bin/env python3
"""Local, stdlib-only mechanical checks for deep-research reader prose.

This implementation is independent of companion skills and lexical policies.
It checks density, not factual accuracy, tone or an external banned-word list.

A sentence over 25 words is reported. Over 35 words fails, as does an average
above 16. Semicolons, em dashes and nested bullets fail. The fixed C1 decision
fields may be indented, but their prose is still checked. More than two middle
dots fail. Parenthetical asides of at least 12 characters have an allowance of
max(1, words // 250). Packed sentences have an allowance of max(1, sentences //
10). A colon plus two commas, or four numbers/ticket ids, makes a sentence packed.

Markdown frontmatter, fenced/inline code, tables, block quotes, URLs and link
destinations are excluded. Visible link labels remain subject to every check.
Sentence statistics include headings and list items with at least three words.
Line breaks delimit prose units, so fixed fields are measured independently.

Usage: python prose_check.py [--strict] FILE [FILE ...]
Use '-' or omit FILE to read stdin. --strict returns 1 on a failed check.
"""
from __future__ import annotations

# Bind the actual module code for completion-check dependency attestation.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import re
import sys
from pathlib import Path

AVERAGE_LIMIT = 16
ADVICE_LIMIT = 25
SENTENCE_LIMIT = 35
DECISION_FIELDS = {"Why it matters", "If you do nothing", "Suggested", "Urgency", "Sources"}
WORD = re.compile(r"[A-Za-z0-9][\w/'’\-]*")
QUANTITY = re.compile(r"\b(?:[A-Z]{2,5}-\d+|\d[\d.,%]*)\b")


def _links_to_labels(text: str) -> str:
    """Keep reader labels, consuming balanced parentheses in link destinations."""
    text = re.sub(r"\[\[([^\]\n]+)\]\]", lambda m: m[1].split("|", 1)[-1], text)
    result = []
    position = 0
    label_pattern = re.compile(r"!?\[([^\[\]\n]*)\]")
    while position < len(text):
        label = label_pattern.search(text, position)
        if label is None:
            result.append(text[position:])
            break
        result.append(text[position:label.start()])
        end = label.end()
        if text[end:end + 1] == "(":
            depth = 1
            cursor = end + 1
            while cursor < len(text) and depth:
                if text[cursor] == "\\":
                    cursor += 2
                    continue
                if text[cursor] == "(":
                    depth += 1
                elif text[cursor] == ")":
                    depth -= 1
                cursor += 1
            if depth == 0:
                end = cursor
        elif text[end:end + 1] == "[":
            target_end = text.find("]", end + 1)
            if target_end != -1:
                end = target_end + 1
        result.append(label[1])
        position = end
    return "".join(result)


def _reader_text(raw: str) -> str:
    """Project common reader Markdown to prose without loading any other file."""
    raw = raw.replace("\r\n", "\n")
    raw = re.sub(r"\A\ufeff?---[ \t]*\n.*?\n(?:---|\.\.\.)[ \t]*(?:\n|$)", "", raw, flags=re.S)
    # Preserve separation when removing blocks, rather than joining their neighbours.
    raw = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "\n", raw, flags=re.I | re.S)
    raw = re.sub(r"<[^>\n]+>", " ", raw)
    rows = raw.splitlines()
    kept = []
    fence = None
    table = False
    separator = re.compile(r"\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
    for index, row in enumerate(rows):
        stripped = row.strip()
        if fence is not None:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(fence[1]) + r",}\s*", stripped):
                fence = None
            kept.append("")
            continue
        opening = re.match(r"^\s{0,3}(`{3,}|~{3,})", row)
        if opening:
            fence = (opening[1][0], len(opening[1]))
            kept.append("")
            continue
        if table and "|" not in row:
            table = False
        starts_table = ("|" in row and index + 1 < len(rows)
                        and separator.fullmatch(rows[index + 1]) is not None)
        if starts_table:
            table = True
        if table or stripped.startswith(("|", ">", "MEDIA:", "::")):
            kept.append("")
            continue
        if re.match(r"^\s{0,3}\[[^\]]+\]:\s*\S", row):
            kept.append("")
            continue
        kept.append(row)
    text = "\n".join(kept)
    # Matching delimiter lengths also allow literal single backticks inside a span.
    text = re.sub(r"(?<!`)(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)", "CODE", text)
    text = _links_to_labels(text)
    text = re.sub(r"https?://[^\s<>]+", "URL", text)
    text = re.sub(r"(?<=[.!?])(?:\[\d+\])+", "", text)
    return text


def _sentences(text: str) -> list[tuple[str, int]]:
    """Measure text units, retaining original punctuation for density checks."""
    measured = []
    for row in text.splitlines():
        row = re.sub(r"^\s*(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", row)
        row = row.replace("**", "").replace("__", "")
        for part in re.split(r"(?<=[.!?])\s+", row.strip()):
            count = len(WORD.findall(part))
            if count >= 3:
                measured.append((part, count))
    return measured


def check(name: str, raw: str, jargon_fail: bool = False) -> bool:
    """Print compatible FAIL/long-sentence lines and return the mechanical result.

    jargon_fail is reserved for caller compatibility. Explicit lexical enforcement
    is unsupported, rather than silently claiming a word-list check was performed.
    """
    if jargon_fail:
        raise ValueError("jargon checks require a separate, explicit lexical policy")
    text = _reader_text(raw)
    measured = _sentences(text)
    sentence_count = len(measured)
    word_count = sum(count for _, count in measured)
    average = word_count / max(1, sentence_count)
    hard_count = sum(count > SENTENCE_LIMIT for _, count in measured)
    nested = False
    for row in text.expandtabs(4).splitlines():
        bullet = re.match(r"^ {2,}[-*+]\s+(.*)", row)
        if bullet and bullet[1].split(":", 1)[0] not in DECISION_FIELDS:
            nested = True
    aside_count = len(re.findall(r"\([^)]{12,}\)", text))
    aside_limit = max(1, word_count // 250)
    packed_count = sum((":" in part and part.count(",") >= 2) or len(QUANTITY.findall(part)) >= 4
                       for part, _ in measured)
    packed_limit = max(1, sentence_count // 10)
    failures = []
    if average > AVERAGE_LIMIT:
        failures.append("average sentence too long")
    if hard_count:
        failures.append(f"{hard_count} sentence(s) over {SENTENCE_LIMIT} words")
    for marker, reason in ((";", "semicolons"), ("—", "em dashes")):
        if marker in text:
            failures.append(reason)
    if nested:
        failures.append("nested bullets")
    if text.count("·") > 2:
        failures.append("middle-dot chains in prose")
    if aside_count > aside_limit:
        failures.append(f"{aside_count} long parentheticals (max {aside_limit})")
    if packed_count > packed_limit:
        failures.append(f"{packed_count} packed sentences (max {packed_limit})")
    print(f"== {name}")
    print(f"words {word_count}, sentences {sentence_count}, average {average:.1f} (max {AVERAGE_LIMIT})")
    for part, count in sorted(measured, key=lambda item: item[1], reverse=True):
        if count > ADVICE_LIMIT:
            print(f"  [{count}w] {part}")
    print("FAIL: " + "; ".join(failures) if failures else "PASS")
    return not failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit 1 if any mechanical check fails")
    parser.add_argument("paths", nargs="*", default=["-"], help="Markdown files or '-' for stdin")
    args = parser.parse_args(argv)
    passed = True
    for name in args.paths:
        raw = sys.stdin.read() if name == "-" else Path(name).read_text(encoding="utf-8")
        passed = check(name, raw) and passed
    return 0 if passed or not args.strict else 1


if __name__ == "__main__":
    raise SystemExit(main())
