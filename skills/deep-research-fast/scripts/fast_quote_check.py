#!/usr/bin/env python3
"""Quote check for deep research fast mode.

A citation passes only when its quote appears in the saved page it points at, and that page's
text still has the SHA-256 recorded when it was saved. Anything else is rejected.

``norm`` and ``MIN_QUOTE_CHARS`` are copied from ``research/deep-research/scripts/quote_check.py``
so both modes agree on what "verbatim" means: case,
whitespace, Markdown markup and curly quotes are ignored, nothing else is.

Usage: python3 fast_quote_check.py --root DIR --run-id ID [--write]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fast_store import Store, contained, norm_url, run_dir, sha256_text  # noqa: E402

MIN_QUOTE_CHARS = 8  # short structured quotes such as "license=MIT" are real; "yes" is not
CITE_RE = re.compile(r"\[(\d+)\]")

_norm_cache: dict[str, str] = {}
_norm_lock = threading.Lock()


def norm(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # Markdown links -> link text
    text = re.sub(r"[*_`>#|\\]", " ", text)                          # Markdown markup
    for a, b in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"'), ("\u2014", "-"), ("\u2013", "-"), ("\u00a0", " ")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip().lower()


def _norm_page(sha: str, text: str) -> str:
    with _norm_lock:
        cached = _norm_cache.get(sha)
    if cached is None:
        cached = norm(text)
        with _norm_lock:
            _norm_cache[sha] = cached
    return cached


def check_quote(page: dict | None, quote: str, expected_sha: str | None = None, root: Path | None = None) -> tuple[bool, str]:
    """Return (ok, reason). ``page`` is a ``pages`` row."""
    if not page:
        return False, "no_saved_page"
    text = page["text"]
    actual = sha256_text(text)
    if actual != page["sha256"]:
        return False, "page_changed"
    if expected_sha and expected_sha != page["sha256"]:
        return False, "page_sha_mismatch"
    if root is not None and page.get("path"):
        f = root / page["path"]
        if f.is_symlink() or not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != page["sha256"]:
            return False, "page_file_changed"
    q = norm(quote or "")
    if len(q) < MIN_QUOTE_CHARS:
        return False, "quote_too_short"
    if q not in _norm_page(page["sha256"], text):
        return False, "quote_not_found"
    return True, "ok"


def check_claim(store: Store, claim_id: int, candidate_pages: list[dict] | None = None,
                pass_status: str = "pass", pass_reason: str = "ok") -> tuple[bool, str]:
    """Check one stored claim and record the verdict.

    A found quote is recorded as ``pass_status``. The controller passes ``quote_ok`` so a finding
    reaches the writer only after its meaning check promotes it to ``pass``.

    When the claim has no page yet, ``candidate_pages`` (the pages its job saved) are tried in
    turn and the claim is bound to the first page that holds the quote.
    """
    claim = store.claim(claim_id)
    if claim is None:
        return False, "unknown_claim"
    root = store.path.parent
    pages = []
    if claim["page_url"]:
        pages.append(store.get_page(claim["page_url"]))
    for p in candidate_pages or []:
        if p and all(p["url"] != q["url"] for q in pages if q):
            pages.append(p)
    reason = "no_saved_page"
    for page in pages:
        ok, reason = check_quote(page, claim["quote"], None, root)
        if ok:
            if claim["page_url"] != page["url"] or claim["page_sha256"] != page["sha256"]:
                with store.tx() as c:
                    c.execute("UPDATE claims SET page_url=?, page_sha256=? WHERE id=?", (page["url"], page["sha256"], claim_id))
            store.set_claim_check(claim_id, pass_status, pass_reason)
            return True, "ok"
    store.set_claim_check(claim_id, "fail", reason)
    return False, reason


def check_answer(store: Store, run_id: str, answer_text: str, citations: list[dict], run_ids=None) -> dict:
    """Check every numbered citation in a rendered answer against the store.

    ``citations`` rows carry ``n``, ``claim_id``, ``page_url``, ``page_sha256`` and ``quote``.
    Fails on: a marker with no citation row, a row the text never uses, a claim from another run
    or one that failed its check, a quote or page that differs from the stored claim, and any
    quote that is not in its saved page.
    """
    root = store.path.parent
    problems: list[dict] = []
    by_n = {int(c["n"]): c for c in citations}
    used = {int(m) for m in CITE_RE.findall(re.sub(r"```.*?```|`[^`\n]*`", "", answer_text.split("\n## Sources", 1)[0], flags=re.S))}
    for n in sorted(used - set(by_n)):
        problems.append({"n": n, "kind": "marker_without_source"})
    for n in sorted(set(by_n) - used):
        problems.append({"n": n, "kind": "source_never_cited"})
    passed = 0
    for n, cit in sorted(by_n.items()):
        claim = store.claim(int(cit.get("claim_id") or 0))
        if claim is None or claim["run_id"] not in (set(run_ids) if run_ids is not None else {run_id}):
            problems.append({"n": n, "kind": "unknown_claim"})
            continue
        if claim["check_status"] != "pass":
            problems.append({"n": n, "kind": "claim_not_passed", "detail": claim["check_reason"]})
            continue
        if cit.get("quote") != claim["quote"] or norm_url(cit.get("page_url") or "") != claim["page_url"] \
                or cit.get("page_sha256") != claim["page_sha256"]:
            problems.append({"n": n, "kind": "citation_differs_from_claim"})
            continue
        ok, reason = check_quote(store.get_page(claim["page_url"]), claim["quote"], claim["page_sha256"], root)
        if not ok:
            problems.append({"n": n, "kind": reason})
            continue
        passed += 1
    verdict = "pass" if citations and not problems else "fail"
    if not citations:
        problems.append({"n": None, "kind": "no_citations"})
    return {"verdict": verdict, "citations": len(by_n), "markers_used": len(used), "passed": passed, "problems": problems}


def check_answer_files(root: Path, run_id: str) -> dict:
    root = Path(root).expanduser().resolve()
    folder = run_dir(root, run_id)  # a bad id is refused before the store is opened
    store = Store(root / "fast.db")
    answer = contained(folder, folder / "answer.md").read_text(encoding="utf-8")
    citations = json.loads(contained(folder, folder / "citations.json").read_text(encoding="utf-8"))
    run_ids = [run_id]
    if store.phase(run_id, "base"):
        run_ids.extend(r["id"] for r in store.child_runs(run_id))
    return check_answer(store, run_id, answer, citations, run_ids=run_ids)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Fast mode quote check: every citation's quote must be in its saved page")
    ap.add_argument("--root", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--write", action="store_true", help="also write runs/<id>/quote_check.json")
    args = ap.parse_args(argv)
    try:
        result = check_answer_files(Path(args.root), args.run_id)
        if args.write:
            folder = run_dir(Path(args.root), args.run_id)
            out = contained(folder, folder / "quote_check.json")
            tmp = contained(folder, folder / "quote_check.json.tmp")
            tmp.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            tmp.replace(out)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["verdict"] == "pass" else 3


if __name__ == "__main__":
    raise SystemExit(main())
