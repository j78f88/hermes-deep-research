<!-- phase: repair; name: ${PHASE_NAME} -->
You repair a deep-research reference pack after independent review. Round ${ROUND} of at most ${MAX_ROUNDS}.

CAMPAIGN ROOT: ${ROOT}
REPORT PURPOSE: ${PURPOSE}. THE QUESTION: ${QUESTION}
TOOL-CALL BUDGET: at most ${BUDGET} tool calls.

READ: ${REPAIR_INPUTS}
plus internal/reference-pack.md, synthesis/visual-exhibits.json, citations.json, lanes/*.json and the saved
sources under workspace/sources/ to confirm a fix. Do not read anything outside the root.

FIX ONLY WHAT THE REVIEWS AND CHECKS ASK FOR. Keep healthy, supported prose unchanged. For each requested repair, change
the affected sentence, citation, exhibit or ledger entry so it matches the saved evidence. If a request is wrong, leave the
text and say why in the repair log. Do not add new claims without a saved source. Keep every rule the pack already follows:
frontmatter unchanged, the six headings, one "## Sources" heading, [CODE] citations from citations.json, three exhibits whose
"### Exhibit En — <title>" headings match the ledger anchors, and no process records (no Action Register rows, QA or PASS/HOLD
wording, receipts, file paths, lane or worker names, hashes).

Do not edit any qa/ receipt, lanes/, workspace/ or manifest.json.

WRITE: qa/repairs/round-${ROUND}-log.json as {"round": ${ROUND}, "repairs": [{"request": "...", "change": "...",
"status": "fixed|declined", "reason": "..."}]}. Final reply under 100 words.
