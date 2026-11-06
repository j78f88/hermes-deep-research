#!/usr/bin/env python3
"""Fact grades and citation measures shared by fast mode, deepest research and full deep research.

One scoring vocabulary for every research mode:

* Each cited fact gets a NATO Admiralty Code grade: a source letter and an information number,
  judged separately. Source: the Admiralty code as described in UK JWP 2-00 and US Army
  FM 2-22.3, summarised at https://en.wikipedia.org/wiki/Admiralty_code.
* Citation recall follows ALCE (Gao et al., EMNLP 2023): the share of cited sentences that
  their citations support. The sentence check is an attribution check in the sense of AIS
  (Rashkin et al., 2021).

How web sources map onto the code is this project's own reading. It is written down here so it
can be challenged:

    source kind   letter   meaning in the code
    self          B        usually reliable: a project or company speaking about itself
    primary       B        usually reliable: a standards body, registry, paper or official record
    independent   C        fairly reliable: an independent practitioner write-up or news report
    discovery     D        not usually reliable: forum post, issue comment, repost, aggregator
    unknown       F        reliability cannot be judged

    information   number   when
    confirmed     1        a second independent origin also supports the fact
    supported     2        one origin fully supports it
    narrowed      3        supported only after the claim was cut back to the quote
    no source     6        nothing supports it

Grade A is never given automatically: the code reserves it for a source with a history of
complete reliability, and these runs keep no such history. Grades 4 and 5 need a check for
contradicting sources, which does not exist yet, so they are never given.

Each page gets one source kind: the kind the checker gave most often for the run's own findings on it,
with ties going to the lower letter. Reposts never count as confirmation. Pages on one site are one origin, and a page holding the
same quote as another saved page is a repost of it. This follows the lineage rule in
``select_sources`` in ``research/deep-research/scripts/research_qa_repair.py`` reimplemented here because that function works on campaign source records.
All self sources count as one origin: a project's docs and its own repo are not independent.
Subdomains merge into the last two host labels, or the last three when the second-last label
is com, net, org, gov, edu, co or ac and the last is a two-letter country code. This is a simple
registrable-domain rule, not a full public suffix list. Shared-author sites still separate authors.

Standard library only. No model calls.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Callable, Iterable
from urllib.parse import urlsplit

KIND_LETTER = {"self": "B", "primary": "B", "independent": "C", "discovery": "D"}
LETTER_ORDER = "ABCDEF"
LETTER_WORDS = {"B": "usually reliable source", "C": "fairly reliable source", "D": "not usually reliable source",
                "F": "source reliability unknown"}
NUMBER_WORDS = {1: "confirmed by another independent source", 2: "one source", 3: "supported only in part",
                6: "no source"}
KINDS = tuple(KIND_LETTER) + ("unknown",)
LOW_LETTERS = {"D", "F"}
LOW_NUMBERS = {3, 6}
SCHEME = "NATO Admiralty Code"

# Sites where the first path part names a separate author, so each author is its own origin.
_PER_AUTHOR_HOSTS = {"github.com", "gitlab.com", "medium.com", "huggingface.co", "x.com", "twitter.com",
                     "linkedin.com", "youtube.com", "reddit.com", "dev.to"}

CITE_RE = re.compile(r"\[(\d+)\]")
_SENTENCE_END = re.compile(r"""(?<=[.!?])(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\betc\.)(?<!\bvs\.)(?<!\bcf\.)(["'”’)]*(?:[ \t]*\[\d+\])*(?:[ \t]*\((?:Not verified|Not checked|grade [^)]*)\))*)[ \t]+(?![ \t]|\[\d+\]|\((?:Not verified|Not checked|grade ))""", re.I)
_TECH = re.compile(r"`[^`]+`|\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b|(?<![\w-])--?[a-z][\w-]*|\bv?\d+\.\d+(?:\.\d+)?\b|https?://\S+")
NOT_VERIFIED = "(Not verified)"
_CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def sub_cites(repl, text: str) -> str:
    """``CITE_RE.sub`` outside code spans and fences, so ``argv[1]`` is never read as a citation."""
    out, pos = [], 0
    for m in _CODE.finditer(text):
        out += [CITE_RE.sub(repl, text[pos:m.start()]), m.group(0)]
        pos = m.end()
    return "".join(out + [CITE_RE.sub(repl, text[pos:])])


def normalise_kind(value) -> str:
    """A source kind from a model reply. Anything unexpected counts as unknown."""
    return value if isinstance(value, str) and value in KIND_LETTER else "unknown"


def site_origin(url: str) -> str:
    """A simple registrable domain, plus the author on shared-author sites."""
    parts = urlsplit(url or "")
    host = parts.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    if host in _PER_AUTHOR_HOSTS:
        first = next((p for p in parts.path.split("/") if p), "")
        return f"{host}/{first.lower()}" if first else host
    labels = host.split(".")
    count = 3 if len(labels) >= 3 and labels[-2] in {"com", "net", "org", "gov", "edu", "co", "ac"} and len(labels[-1]) == 2 else 2
    host = ".".join(labels[-count:])
    return host or (url or "unknown")


def origins(citations: list[dict], quote_in_page: Callable[[str, str], bool]) -> dict[int, str]:
    """Map each citation number to an origin id. Reposts join the origin they copied.

    ``citations`` rows carry ``n``, ``page_url`` and ``quote``. ``quote_in_page(quote, page_url)``
    says whether a quote appears in another saved page. When citation A's quote appears on
    citation B's page and the two sites differ, both join one origin.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    site = {c["n"]: site_origin(c.get("source_url") or c.get("page_url") or "") for c in citations}
    pages = {c["page_url"]: site[c["n"]] for c in citations if c.get("page_url")}
    for c in citations:
        for page_url, other in pages.items():
            if other != site[c["n"]] and quote_in_page(c["quote"], page_url):
                union(site[c["n"]], other)
    return {n: find(s) for n, s in site.items()}


def grade(supports: list[dict]) -> dict:
    """The Admiralty grade of one fact from the citations that back it.

    Each support row carries ``verdict`` (``supported`` or ``partly``), ``kind`` and ``origin``.
    """
    rows = [s for s in supports if s.get("verdict") in ("supported", "partly")]
    if not rows:
        return {"letter": "F", "number": 6, "code": "F6", "words": f"{LETTER_WORDS['F']}, {NUMBER_WORDS[6]}"}
    full = [s for s in rows if s["verdict"] == "supported"]
    self_origins = {s.get("origin") for s in full if s.get("kind") == "self"}
    best_pool = full or rows
    letter = min((KIND_LETTER.get(str(s.get("kind")), "F") for s in best_pool), key=LETTER_ORDER.index)
    if not full:
        number = 3
    elif len({"self" if s.get("origin") in self_origins else s.get("origin")
              for s in full if s.get("origin")}) >= 2:
        number = 1
    else:
        number = 2
    return {"letter": letter, "number": number, "code": f"{letter}{number}",
            "words": f"{LETTER_WORDS[letter]}, {NUMBER_WORDS[number]}"}


def is_low(g: dict) -> bool:
    return g["letter"] in LOW_LETTERS or g["number"] in LOW_NUMBERS


def split_sentences(body: str) -> list[dict]:
    """Split Markdown prose into sentences, keeping citation markers with their sentence.

    Returns rows with ``line``, ``start`` and ``end`` offsets on that line, ``text`` (with
    markers) and ``cites`` (citation numbers). Headings and code fences are skipped.
    Table cells and block quotes are checked even though writer prompts forbid them.
    """
    out, fenced = [], False
    for i, line in enumerate(body.split("\n")):
        stripped = line.strip()
        if stripped.startswith("```"):
            fenced = not fenced
            continue
        if fenced or not stripped or stripped.startswith("#"):
            continue
        masked = _CODE.sub(lambda m: "x" * len(m.group()), line)
        prefix = re.match(r"\s*(?:(?:>\s*)+)?(?:(?:[-*+]|\d+[.)])\s+)?", masked)
        begin = prefix.end() if prefix else 0
        cells = [(begin, len(line))]
        if stripped.startswith("|"):
            if re.fullmatch(r"[|:\s-]+", line):
                continue
            cells = [(m.start(), m.end()) for m in re.finditer(r"[^|]+", masked)]
        for begin, end in cells:
            text = line[begin:end]
            ranges, pos = [], 0
            for m in _SENTENCE_END.finditer(masked[begin:end]):
                ranges.append((pos, m.end(1)))
                pos = m.end()
            ranges.append((pos, len(text)))
            for start, stop in ranges:
                piece = text[start:stop]
                s = piece.strip()
                if s:
                    left = begin + start + len(piece) - len(piece.lstrip())
                    out.append({"line": i, "start": left, "end": left + len(s), "text": s,
                                "cites": [int(n) for n in CITE_RE.findall(_CODE.sub("", s))]})
    return out


def mark_not_verified(body: str) -> tuple[str, list[str]]:
    """Mark uncited sentences that state a command, setting or exact name as not verified.

    The signs are a code span, an all-caps name with an underscore, a command-line flag, a
    version number or a web address. Cited sentences are never marked.
    """
    lines, items = body.split("\n"), []
    marks = []
    for row in split_sentences(body):
        if row["cites"] or NOT_VERIFIED in row["text"] or not _TECH.search(row["text"]):
            continue
        marks.append(row)
        items.append(row["text"])
    for row in reversed(marks):
        line, end = lines[row["line"]], row["end"]
        lines[row["line"]] = line[:end] + f" {NOT_VERIFIED}" + line[end:]
    return "\n".join(lines), items


def grade_counts(graded: Iterable[dict]) -> dict[str, int]:
    return dict(sorted(Counter(g["code"] for g in graded).items()))


def citation_recall(verdicts: list[str]) -> float | None:
    """ALCE citation recall: the share of checked cited sentences that their citations fully support."""
    return round(sum(v == "supported" for v in verdicts) / len(verdicts), 3) if verdicts else None


def summary_line(counts: dict[str, int]) -> str:
    """A one-line count of grades for an answer header, for example ``B1 3, B2 10, C2 4``."""
    return ", ".join(f"{code} {n}" for code, n in counts.items()) or "none"
