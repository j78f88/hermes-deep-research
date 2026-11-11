# Automatic editor-note judgements

The existing brief editor judges every current and retained finding. There is no third model stage. The editor must read the current reader text and admitted source evidence. Its developer must differ from every contributing writer. A comments-only editor is not a writer.

## Normal factual workflow

1. Finalise the reference pack and its existing evidence/privacy review.
2. List exact source paths in that review's `evidence_inputs` array. Paths must stay inside the campaign.
3. Run the canonical brief command below. It writes the brief, review and supported note decisions automatically.
4. Repair any unresolved factual finding. Preserve the rejected files and rerun the existing workflow.
5. Run publication review, verification, completion and export. An offline completion result remains HOLD.

```bash
S=/absolute/path/to/deep-research/scripts  # installed skill, not a private shared checkout
ROOT=/absolute/path/to/campaign
python3 "$S/plain_brief_rewrite.py" --campaign-root "$ROOT"
python3 "$S/run_campaign.py" resume --root "$ROOT" --from final_reviews --stop-after final_reviews
python3 "$S/verify_campaign_depth.py" --campaign-root "$ROOT" --mode true_deep_research --json
python3 "$S/complete_campaign.py" --campaign-root "$ROOT"
python3 "$S/export_deliverables.py" --campaign-root "$ROOT" --dry-run
python3 "$S/export_deliverables.py" --campaign-root "$ROOT"
```

The existing publication command still accepts `--command` for the campaign's publication checks. Preflight alone does not perform those checks. If `plain_brief.review` names a custom path, give that path to publication review through `--review`. Generation, verification, completion and export use the manifest path.

Do not write a PASS decision by hand. Missing or invalid judgements stop acceptance. A parse or schema failure retains the raw response and stops without another paid parsing attempt. Model settings, credential handling and transport retries stay unchanged.

## Editor response

The editor receives `REVIEW_BINDING` and `RETAINED_FINDINGS` in its ordinary prompt. Its existing response keeps `verdict`, `blocking`, `minor`, the four separate checks and the quantity/qualification review. It also returns `review_binding` and `note_dispositions`.

`review_binding` echoes the supplied object exactly. It contains the current reader-body hash, reference-pack hash and exact evidence path/hash rows. The editor sees those evidence files in full. Evidence is an explicit list, not a directory search.

Every current blocking or minor note needs one disposition. Every retained note needs one too, including notes from earlier correction rounds or archived reviews. Each disposition has exactly these fields.

| Field | Required value |
|---|---|
| `note` | Exact finding text |
| `classification` | `apply_correction`, `already_resolved`, `mistaken_reviewer` or `optional_style` |
| `classification_verdict` | `supported`, `partial` or `unsupported` |
| `evidence_verdict` | A separate `supported`, `partial` or `unsupported` judgement |
| `factual_impact` | `resolved`, `reviewer_error`, `none` or `unresolved` |
| `correction_applied` | JSON boolean |
| `rationale` | Non-empty explanation of the evidence comparison |
| `evidence_bindings` | Non-empty subset of the supplied evidence path/hash rows |

Both verdicts must be `supported` for acceptance. An applied correction also needs `correction_applied: true` and `factual_impact: resolved`. A proposed but unapplied correction stays open. Optional style requires `factual_impact: none`. A mistaken reviewer needs `reviewer_error`. Already resolved needs `resolved`.

These are model judgements, not keyword rules. A lost forecast qualifier cannot become style through a local label change.

## Saved files and checks

The generator keeps each raw provider reply under its unique `qa/plain-brief-rounds/` run. It writes each returned judgement once under `qa/editor-judgements/<run>/`. A judgement records the actual final editor generation and served model. It also records both reader hashes, the raw-response path and review bindings.

`qa/editor-note-decisions.json` uses `research-editor-note-decisions-v2`. Its `status` is `reviewed` only after all retained findings have returned dispositions. Otherwise it stays `pending`. It always binds both readers, the saved review, semantic manifest, admitted evidence and retained history through `review_inputs`.

The shared readiness check reopens the raw provider reply. The exact typed judgement must occur there once. The generation, served identity and current reader/evidence bindings must match. Re-labelling the saved decision or replacing its reviewer string cannot establish acceptance.

Provider authentication remains a separate required check. Completion and export check every recorded generation against the provider afresh. This includes the final comments editor that supplied the judgements. A writable saved PASS is not proof. A dry-run export also refuses unknown provider evidence.

## Corrected drafts and history

`--corrected-draft` preserves a draft instead of paying a writer to rewrite it. Current campaigns also need `--corrected-draft-authors`. This campaign-confined packet has `draft_sha256` and `writer_calls`. Keep every actual contributing writer and rewriting editor, including served fallbacks. Their generations remain subject to normal provider checks. Unknown authors cannot establish independent acceptance.

Previous reviews, decisions, readers and admitted evidence are archived before replacement. Old findings remain open until checked against the current reader bytes. Do not edit archived history or copy an old judgement onto a new reader.

Only the frozen inventory in `report_purpose.py` permits historical convenience. Removing purpose, changing layout or setting `plain_brief.required: false` cannot create that exception. New factual and technical-assessment campaigns require the current brief and served identities.

## Offline verification

Use synthetic transports and credentials for tests. Deny socket and urllib access before starting pytest. Confine every export path to test directories.

The historical tests accept `DEEP_RESEARCH_TEST_FIXTURE_ROOT` as an explicit external fixture directory. Run those historical modules separately. The settings resolver deliberately rejects unknown `DEEP_RESEARCH_*` keys, so do not pass that test-only variable into model-workflow tests. Keep real historical content outside the skill source.

Diagram review checks supplied writer generations against the provider before buying image reviews.
It uses the same provider lookup, not a new model stage.
Unknown or mismatched identities stop acceptance.
Every declared writer and rewriting editor counts, including served fallbacks.
See [diagram review](diagram-native-review.md#writer-identity-checks) for the input format and saved results.
This check cannot prove that the supplied author list is complete.
It also cannot prove which image bytes a declared call contributed.
Diagrams remain optional for factual work.
Relation tables can carry the factual proof.
