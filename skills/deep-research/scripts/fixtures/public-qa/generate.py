#!/usr/bin/env python3
"""Rebuild deterministic synthetic regression inputs, never research evidence.

No network, credentials, provider outputs or historical campaign data are used.
Only this fixture directory and the adjacent legacy-named decisions fixture
are written. Test collection reads checked-in files; it does not run this script.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CAMPAIGN = ROOT / "synthetic-campaign"
LABEL = {
    "fixture_kind": "synthetic_public_regression",
    "provider_evidence": False,
    "publication_approval": False,
}
HEADER = (
    "> SYNTHETIC REGRESSION DATA. Not historical records, provider responses, "
    "or publication evidence.\n\n"
)


def put(rel: str, value: str | dict) -> Path:
    path = CAMPAIGN / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=False) + "\n" if isinstance(value, dict) else value
    path.write_text(text, encoding="utf-8")
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review(brief: Path, notes: list[str], *, blocked: bool = False) -> dict:
    return {
        **LABEL,
        "status": "FAIL" if blocked else "PASS",
        "brief_path": "internal/plain-brief.md",
        "brief_sha256": sha(brief),
        "full_pack_path": "internal/reference-pack.md",
        "full_pack_sha256": sha(CAMPAIGN / "internal/reference-pack.md"),
        "editor_minor": [] if blocked else notes,
        "editor_blocking": notes if blocked else [],
    }


def generate() -> None:
    put("internal/reference-pack.md", HEADER + "# Synthetic reference pack\n\n"
        "Round 28 is planned to open in 2027. The example laboratory has not confirmed a launch date.\n\n"
        "## Sources\n\nAll sources below are invented test inputs, not external research.\n")
    defects = [
        "Retain the source's planned qualifier rather than promising an opening date.",
        "Do not turn a vendor assertion into an independently verified result.",
        "Keep download counts distinct from paying customer counts.",
        "A project duration limit does not define the programme's closing date.",
        "The editor blocks release because the supporting quotation is absent.",
        "Do not claim all advertised pages were attempted when only one was checked.",
        "Do not silently resolve conflicting eligibility rules without an owner warning.",
    ]
    for generation, defect in enumerate(defects, 1):
        rel = f"receipts/qa-history/plain-brief-generation{generation}"
        text = HEADER + f"# Synthetic defective draft {generation}\n\n{defect}\n"
        if generation == 6:
            text += ("\n## Funding\nRound 28 opens in 2027.\n\n"
                     "## Access summary\nNo result was found despite attempts across its advertised pages.\n\n"
                     "## Access detail\nStill unavailable despite attempts across its advertised pages.\n")
        brief = put(f"{rel}/plain-brief.md", text)
        put(f"{rel}/plain-brief-review.json", review(brief, [defect], blocked=generation == 5))

    # MC-2 is a parser compatibility marker, not a real company, site or claim.
    pass1 = "orchestration/pass1-backup"
    brief = put(f"{pass1}/internal/plain-brief.md", HEADER + "# Synthetic qualified draft\n\n"
        "Round 28 is planned to open in 2027. Only the example overview page was checked.\n"
        "The invented test facility MC-2 belongs to Example Laboratory.\n")
    put(f"{pass1}/qa/plain-brief-review.json", review(brief, [
        "MC-2 belongs to Example Laboratory; the synthetic reviewer proposes assigning it to another operator.",
        "Split the funding and access summaries into separate paragraphs.",
    ]))
    put(f"{pass1}/qa/packaging-review-findings.md", HEADER + "# Synthetic adjudication inputs\n\n"
        "The proposed operator correction is unsupported. Keep the planned opening qualifier.\n"
        "These hand-authored inputs are not independent served editor judgements.\n")

    notes = [
        "Split the example organisations paragraph into shorter paragraphs.",
        "Explain the example site identifier when it first appears.",
        "Use plain words for the example model identity test.",
        "Explain the example adapter training term.",
        "Define downloadable weights at first mention.",
    ]
    facts = [
        "Example Laboratory builds test models. Example Cloud hosts test services. Downloads are not customers.",
        "Example Site One is the first test inference site at Example Data Centre.",
        "The example model identity test asks a model to state its base family.",
        "The example adapter updates are merged into the base weights.",
        "Downloadable weights are model parameters available for local adaptation.",
    ]
    brief = put("internal/plain-brief.md", HEADER + "# Synthetic refreshed reader\n\n"
        "Round 28 is planned to open in 2027.\n\n" + "\n\n".join(facts) + "\n")
    current_review = review(brief, notes)
    put("qa/plain-brief-review.json", current_review)
    decisions = []
    index_rows = []
    for number, (note, fact) in enumerate(zip(notes, facts), 1):
        source_rel = f"workspace/sources/synthetic/source-{number}.md"
        source = put(source_rel, HEADER + f"# Synthetic source {number}\n\n{fact}\n")
        url = f"https://example.invalid/synthetic/source-{number}"
        index_rows.append({**LABEL, "file": source_rel, "sha256": sha(source), "url": url})
        refs = ["internal/plain-brief.md", "internal/reference-pack.md", source_rel]
        bindings = [{"path": rel, "sha256": sha(CAMPAIGN / rel)} for rel in refs]
        bindings[-1]["url"] = url
        decisions.append({
            "id": f"EN-{number:02d}", "note": note, "classification": "optional_style",
            "disposition": "deferred_style_preference",
            "rationale": "The invented source and reader agree. The note only requests an explanation or grouping change.",
            "evidence_refs": refs, "evidence_bindings": bindings, "evidence_verdict": "supported",
        })
    index_bytes = b"".join((json.dumps(row, sort_keys=True) + "\n").encode() for row in index_rows)
    put("workspace/sources/synthetic/index.jsonl", index_bytes.decode())
    # Distinct original and appended records exercise exact byte-prefix preservation.
    original = (json.dumps(index_rows[0], sort_keys=True) + "\n").encode()
    put("workspace/sources/02-model-lineage/index.jsonl", index_bytes.decode())
    put("receipts/source-index-reconciliation/append-only-proof.json", {
        **LABEL, "original_prefix_bytes": len(original),
        "original_index_sha256": hashlib.sha256(original).hexdigest(),
        "current_index_sha256": hashlib.sha256(index_bytes).hexdigest(),
    })
    original_decisions = {
        **LABEL, "schema_version": "research-editor-note-decisions-v1", "status": "complete",
        "brief_sha256": current_review["brief_sha256"],
        "full_pack_sha256": current_review["full_pack_sha256"],
        "decisions": [dict(item, classification="style_only_non_factual") for item in decisions],
    }
    original_path = put("qa/editor-note-decisions.json", original_decisions)
    repaired = {
        **LABEL, "schema_version": "research-editor-note-decisions-v1", "status": "complete",
        "review_path": "qa/plain-brief-review.json",
        "brief_sha256": current_review["brief_sha256"],
        "full_pack_sha256": current_review["full_pack_sha256"], "decisions": decisions,
        "repair_provenance": {
            "scope": "Synthetic format-only repair. It is intentionally missing independent served classification judgements.",
            "source_decisions_sha256": sha(original_path),
            "classification_change": "style_only_non_factual -> optional_style",
            "publication_approval": False,
        },
    }
    # Preserve the compatibility filename, never its former historical contents.
    (ROOT.parent / "sovereign-decisions-repaired.json").write_text(
        json.dumps(repaired, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    generate()
