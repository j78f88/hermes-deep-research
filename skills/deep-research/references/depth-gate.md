# Deep Research Depth Gate

Use this before a deep-research campaign is allowed to call its output complete deep research.

## Classification

Every run must declare exactly one mode:

| Mode | Meaning | Worker requirement | Output allowed |
|---|---|---:|---|
| `brief` | Quick answer or compact memo | 0–2 lanes | memo / short brief only |
| `true_deep_research` | Multi-lane evidence campaign | 6+ lanes | plain brief (the owner reads first) + reference pack |
| `board_brief_only` | the owner explicitly asks only for a short decision brief | 0–3 lanes | short brief only |
| `refresh_delta` | Update an existing pack | 2–5 delta lanes | delta brief + updated ActionRegister |

If the owner says **deep research**, default to `true_deep_research`.

## Minimum thresholds for `true_deep_research`

These are not word-count theater; they are guardrails against memo-depth output being mislabeled as a full pack. Depth floors apply to evidence and the reference pack only. The plain brief has a cap, never a floor.

Default PASS thresholds:

- at least 6 lane artifacts completed;
- at least 6 worker packets generated or explicitly marked N/A with reason;
- at least 5 lane outputs have PASS or accepted HOLD status;
- at least 15 sources for narrow/internal topics, 25+ for product/market/category topics;
- reference pack present, with no word floor (depth comes from lanes, sources and saved quotes);
- plain brief at `internal/plain-brief.md`, written after the reference pack is final, at most 2,000 words, with no floor or target, passing `plain_brief_rewrite.py` lint and an independent editor review recorded in `qa/plain-brief-review.json`;
- no word floor on the plain brief;
- reference pack includes at least 3 meaningful evidence-backed exhibits (the plain brief carries none);
- at least 1 decision-supporting diagram when the target has product, market, workflow, architecture, repo/tool, internal-system, venture, or operating-model structure, or a disclosed omission of one;
- `synthesis/visual-exhibits.json` exists and records complete/pass/verified visual gate status;
- when action tracking applies, the sidecar (`synthesis/action-register.md`) has at least 5 rows or an explicit no-material-action rationale; factual-purpose reports do not require action rows;
- relevant-history scan complete or explicitly external-only/skipped with reason;
- contradictions/gaps section present;
- what-would-change section present;
- deliverables export of the reference pack and plain brief completed or explicitly suppressed;
- controller state records lane claims/completions, restart selection, retry/fallback counts, reused/redone work, and zero poll-loop API calls.

## HOLD states

Return `HOLD memo_depth` when:

- the reference pack is below threshold;
- child lanes were not actually run;
- lane artifacts are missing;
- source count/source-class coverage is too thin;
- a required action register sidecar is missing or too vague;
- visual exhibit ledger is missing, malformed, placeholder-only, or not complete/pass/verified;
- reference-pack exhibit counts are below threshold;
- a structural topic has no decision-supporting diagram and no disclosed omission;
- exhibits are decorative or lack evidence refs/basis and decision use;
- relevant-history scan is not done;
- only one of the two outputs was exported;
- the plain brief or its review record is missing, the reviewer’s developer did not differ from every actual writer, or the brief/reference pack changed after review.

A HOLD output may be useful, but must be labeled as memo-depth or interim, not complete deep research.

## FAIL states

Return `FAIL` when:

- upstream target code/install/build/test ran without approval;
- private/customer data used without permission;
- a public/client deliverable includes internal history without boundary approval;
- citations are fabricated or source IDs do not map to retrieved sources;
- the reference pack claims conclusions unsupported by lane evidence;
- the controller never read lane artifacts;
- the plain brief contains process vocabulary or citation codes (`[S12]`, `[L3.S4]`, ActionRegister, controller_inference, Context Brief, QA receipts, hashes), exceeds the cap, fails lint, or failed editor review (`FAIL human_output_unreadable`). See `plain-brief-output.md`;
- the reference pack carries process records: Action Register rows, QA receipts, provenance labels or hashes (`FAIL reference_pack_process_records`). Citation codes are allowed there.

## Controller refusal rule

The controller must not close the campaign as full deep research until the verifier returns `PASS true_deep_research` or the owner explicitly accepts a downgraded `brief` / `HOLD memo_depth` output.

If the verifier returns HOLD, the controller should spawn missing lanes or expand the synthesis before deliverables export/closeout.

## Legacy layout

A campaign without `output_layout: brief-and-reference-v2` keeps the older gate: the 8,000-word floor applies to the internal full pack, 2 exhibits are needed in the executive brief as well, and the other legacy output checks stay. It is verified as before and not migrated.
