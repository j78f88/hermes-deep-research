<!-- phase: review; name: ${PHASE_NAME} -->
${REVIEW_COMMON}
REVIEW: evidence, exhibits and privacy of the reference pack (round ${ROUND}).
FILE UNDER REVIEW: internal/reference-pack.md (current sha256 ${PACK_SHA256}).
EVIDENCE: lanes/*.json (findings with verbatim quotes), citations.json (codes to lanes, URLs and saved files), and
workspace/sources/<lane>/*.md with index.jsonl (saved copies and hashes). Also read synthesis/visual-exhibits.json,
receipts/quote-check.json, receipts/tool-preflight.json and context/owned-assets.md. Full checklist: qa/packets/evidence-privacy.md
(ignore its references/ path; qa/packets/reader-pair-style.md is the local copy).

CHECKS
1. Read the whole pack. Sample at least 20 material factual sentences across all sections and every exhibit, weighting
   numbers, versions, dates, names of implementations and absence statements. For each, open the cited saved source and
   judge meaning, not word overlap. Record a verbatim supporting quote of 40 words or fewer.
2. Quantities keep subject, unit, period and qualification.
3. Absence claims are bounded to the sources retrieved. Gaps are labelled as gaps, not cited facts.
4. Judgement is plainly labelled as judgement. No invented recommendation or decision.
5. Privacy: no credentials, secrets, keys, session ids or private paths. The pack goes to a private vault.
6. Process records: no Action Register rows, QA or PASS/HOLD wording, provenance labels, or 40 to 64 character hex hashes.
   The frontmatter block (title, created, type, stage, source, author, credibility, captured_via: agent-deep-research,
   tags such as source/adapted, related, verified_count) is required vault metadata set by the scaffold. It is not a
   process record; do not ask to change it.
7. Exhibits: the pack has at least three exhibits matching synthesis/visual-exhibits.json. Each ledger entry's anchor
   appears in the pack, its type matches what the pack shows, its cells are supported by the cited codes, and it has
   clear column headings or legend. No decorative or placeholder exhibit.
PASS only when blocking is empty, no unsupported material claim remains, privacy_scan_findings is empty, all three
exhibits pass, and all four checks pass. Otherwise HOLD, with exact sentence replacements in requested_repairs.

RECEIPT: write qa/evidence-privacy-review.json with exactly this shape:
{"schema_version": "deep-research-qa-receipt-v1", "name": "evidence_privacy", "round": "r${ROUND}", "status": "PASS|HOLD|FAIL",
 "reviewer": "<model id>", "producers": ${PRODUCERS_JSON}, "controller": "${CONTROLLER}",
 "reference_pack_sha256": "<64 hex of the pack you read>", "blocking": [],
 "checks": {"factual": {"verdict": "PASS|FAIL", "basis": "..."}, "reasoning": {"verdict": "PASS|FAIL", "basis": "..."},
            "editorial": {"verdict": "PASS|FAIL", "basis": "..."}, "presentation": {"verdict": "PASS|FAIL", "basis": "..."}},
 "important_quantity_qualification_review": {"verdict": "PASS|FAIL", "basis": "..."},
 "output_classes": {"reference_pack": "PASS|HOLD"},
 "exhibits": [{"id": "E1", "verdict": "PASS|HOLD", "basis": "..."}],
 "citations": "PASS|HOLD", "privacy": "PASS|HOLD", "process_records": "PASS|HOLD",
 "sampled_claims": [{"sentence": "...", "codes": ["L01-S01"], "verdict": "supported|partly|unsupported", "quote": "...",
                     "unsupported_part": "", "fix": ""}],
 "findings": [], "requested_repairs": [], "privacy_scan_findings": [], "reviewed_at": "<ISO time>"}
Final reply under 150 words.
