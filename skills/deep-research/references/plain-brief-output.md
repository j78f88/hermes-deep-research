# Plain Brief: the owner's First Read

The plain brief is the canonical human-facing output of a `true_deep_research` campaign.
The owner reads it first and should understand it in ten minutes. The current layout has two
outputs: this brief and the reference pack (`internal/reference-pack.md`), which holds the
evidence, sources and detail. The Context Brief, lanes, sidecars and QA files stay in the campaign root.

## Why

Packs gated only on process artifacts (hashes, provenance layers, ActionRegister rows, QA receipts,
word floors) came out long and unreadable: producers wrote for the checker. The plain brief moves
the reader contract into the gate: plain words, fixed structure, a length cap, no process vocabulary,
and an independent editor who checks claims against the reference pack.

## Where it sits in the campaign

```text
research lanes → internal retrieval → Context Brief (read after all lanes finish)
      → reference pack (internal/reference-pack.md)
      → independent QA (evidence/privacy, visual, taxonomy reviewer, Librarian, final assurance)
      → plain brief  (scripts/plain_brief_rewrite.py --campaign-root ROOT)   ← phase "plain_brief"
      → export_deliverables.py (reference pack, plain brief)                     ← phase "export_closeout"
```

Write it only after the reference pack is final. The review record stores the reference-pack SHA-256
(in the field `full_pack_sha256`, kept for compatibility). If the reference pack changes later, the
verifier and exporter treat the brief as stale and it must be rerun.

## Files

| File | What it holds | Human-facing |
|---|---|---|
| `internal/plain-brief.md` | The brief: frontmatter, reader text, one "Detail and sources" footer line that links the reference pack | Yes, read first |
| `qa/plain-brief-review.json` | `deep-research-plain-brief-review-v1`: status, writer and editor models, lint result, editor blocking/minor notes, brief and reference-pack SHA-256 | No (sidecar) |
| `qa/editor-note-decisions.json` | Current and retained note decisions, generated from the actual editor response. Missing judgements stay pending. | No (sidecar) |
| `qa/plain-brief-rounds/` | Each draft round and the full writer/editor log | No (sidecar) |
| `internal/reference-pack.md` | Evidence ledger, sources, tables, exhibits, detail | Reference |
| `synthesis/action-register.md` | Action register rows | No (sidecar) |

Manifest block written by `init_campaign.py` (and updated by the generator):

```json
"plain_brief": {"required": true, "path": "internal/plain-brief.md", "review": "qa/plain-brief-review.json", "status": "not_started"}
```

`deliverable_exports.allowlist` contains the reference pack and the plain brief, with
`plain_brief_staging` and `plain_brief_intended_export_path` next to `reference_pack_staging` and
`reference_pack_intended_export_path`. Destinations are directly under the resolved
`DELIVERABLES_EXPORT_PATH` (default `~/research-deliverables`):
`source-research-<slug>-plain-brief-<date>.md` and
`source-research-<slug>-reference-pack-<date>.md`. There is no fixed import subdirectory.
In the two-output layout (`manifest.output_layout` is `brief-and-reference-v2`) the plain brief is
required: the verifier HOLDs with `plain_brief_required_in_v2_layout` when the block is missing or
`required` is false.

## Reader contract

Enforced by `plain_brief_rewrite.lint()` (also `--check FILE`):

Use the shared [reader structure and style](reader-pair-style.md). New manifests declare `report_purpose`.

- `factual`: answer and limitation first, then `## What was asked`, `## What we found` and `## What is still missing`. No commercial or owner-decision chapters are required.
- `decision_assessment`: requested technical judgement adds `## Technical judgement` and `## Alternatives and trade-offs`. Genuine research and normal checks still apply.
- `business_assessment` and evidence-reuse-only assessments remain unsupported. A topic never selects assessment purpose.
- Historical purpose-less campaigns require the frozen identity and hash inventory, not merely an older layout.
- Length: cap 2,000 words. There is no floor or target. Keep enough why and how to explain the answer.
- Average sentence length 22 words or fewer.
- No process vocabulary or citation codes. `BANNED` covers consultant words and core process terms
  (controller, external/internal context labels, ActionRegister, clean-room, frozen, verifier, owner
  gate, Phase 0, Exhibit, SHA-256, G0–G6). `PROCESS_TERMS` adds source/lane codes such as `[S12]`,
  `[L3.S4]`, `[L03-S2 @00:12:34]`, `AR3`, `controller_inference`, Context Brief, reconciliation
  table, QA/verification/freeze receipts, research lanes, lane outputs/packets, worker packets,
  provenance layers, evidence bundle, and 40–64 character hex hashes. Plain topic words such as
  "receipt" or "provenance" are allowed. Link targets are ignored; link text is checked.
- Sources are cited by linking real URLs from the pack on descriptive words. `citations.json` is
  passed to the writer so it can map codes to URLs.
- The footer links only the reference pack: `Detail and sources: [[<note>|reference pack]].` The
  frontmatter `related` list identifies the reference pack and any explicitly admitted related deliverables.
  No navigation-page link or central registry is required.
- No wikilink alias pipes inside tables.

## Review

Review the full pack first in the existing evidence/privacy QA. Bind its current bytes and record distinct factual, reasoning, editorial and presentation results. Compare important quantities and qualifications against saved evidence, not number counts.

The brief editor then reads both readers and the saved evidence. Factual corrections are blocking, even when labelled style. Up to two repair rounds change only affected content. Raw responses survive local parsing errors, which do not trigger paid retries.

Use the shared settings roles for model selection. Acceptance requires a reviewer from a different developer from the actual writers, including rewriting editors. Different model names or hosts alone are not independent.

The same comments-only editor supplies typed judgements for current and retained notes. No third model stage is required. See [automatic editor-note judgements](editor-note-judgements.md) for source bindings, saved files, corrected drafts and commands.

## Gates

`verify_campaign_depth.py` (any mode, when the block is declared; mandatory in the two-output layout):

- FAIL `human_output_unreadable`: process vocabulary or citation codes, over the cap, other lint
  failures, or an editor verdict other than PASS.
- FAIL `plain_brief_prose_check_failed` (two-output layout only): the brief fails the installed plain-prose
  checker (`productivity/plain-prose/scripts/prose_check.py` in a compatible companion skill library). Confirm that dependency is installed; it is not bundled here. The writer gets the checker's rules and
  its failures each round. Legacy briefs are not held to it.
- HOLD: brief or review record missing, review schema invalid, writer equals editor, brief edited
  after review, reference pack changed after the brief was written, plain brief absent from the knowledge base
  allowlist/staging/readback.

`export_deliverables.py` refuses to copy anything while any of those apply. `campaign_controller.record_plain_brief(root)`
marks the `plain_brief` phase complete only when the same shared gate is clean.

## Commands

```bash
S=/absolute/path/to/deep-research/scripts
python3 $S/plain_brief_rewrite.py --campaign-root <root>     # writes brief + review; exit 1 if not PASS
python3 $S/plain_brief_rewrite.py --check <root>/internal/plain-brief.md
python3 $S/verify_campaign_depth.py --campaign-root <root> --mode true_deep_research --json
python3 $S/export_deliverables.py --campaign-root <root> --dry-run
```

Record actual usage and served identities separately from frozen non-secret settings. Price and speed measurements must be dated. Unknown or stale measurements remain unknown. Check that each repair returns the whole brief.

## Legacy layout

A campaign without `output_layout: brief-and-reference-v2` keeps the four outputs. Its brief is
written from the internal full pack, reads the executive brief and `citations.json` as extra sources,
and ends with `Detail and sources: [[<full pack note>|full research pack]], [[<executive brief note>|executive brief]].`
Only the frozen historical identity/hash inventory permits an optional brief or old identity format.
Unknown campaigns cannot remove purpose or make the brief optional to claim that exception.
Export checks provider evidence before either dry-run approval or copying.

## Automation boundary

This skill owns the reader and acceptance contract. Other automation may call the generator but must preserve the same evidence, privacy, independence and delivery gates. Replacing the canonical engine requires explicit approval, not a routing workaround.
