# Two Outputs and Independent QA Contract

Deep research produces two outputs in layout `brief-and-reference-v2`:

1. **The plain brief**: `internal/plain-brief.md`. The owner reads it first. See `plain-brief-output.md`.
2. **The reference pack**: `internal/reference-pack.md`. It holds the evidence, sources, tables and detail behind the brief.

Process records stay out of both. They live in sidecars: `qa/`, `receipts/`, `synthesis/action-register.md` and `manifest.json`.

The manifest field `output_layout` selects the layout. The value `brief-and-reference-v2` is this layout. A missing or other value is the legacy layout (see the end of this file).

## Execution graph

```text
research lanes (no Context Brief, no private context)
internal retrieval ──→ Context Brief          (read by the controller only after all lanes finish)
                                  ↓
            controller synthesis ──→ internal/reference-pack.md
                                  ↓
   independent evidence/privacy QA + visual QA + taxonomy QA
                                  ↓
              Librarian packaging repair only, if needed
                                  ↓
                    independent deterministic final assurance
                                  ↓
   plain_brief_rewrite.py --campaign-root ──→ internal/plain-brief.md
```

Internal retrieval may run while the lanes run. The Context Brief is never given to a worker. The controller reads it only after every research lane has finished. `campaign_controller.record_context_release(root)` records that time and refuses until all lanes are complete.

## Output boundaries

### Reference pack

`internal/reference-pack.md` is the evidence behind the brief. It has:

- frontmatter with `title`, `created`, `type`, `stage`, `source`, `author`, `captured_via`, `tags`, `related`;
- an evidence ledger, current-state facts, analysis, breakpoints, exhibits, option comparison, what would change the call, gaps, and sources;
- citation codes such as `[S12]`, which are allowed here.

It has no provenance-layer headings, reconciliation table or word floor. Depth is demonstrated by lanes, distinct sources, saved quotes, relevant exhibits and semantic QA. The 8,000-word floor belongs only to the legacy layout.

It must not contain process records. The verifier fails it as `reference_pack_process_records` when it finds any of these:

- Action Register rows or headings;
- QA, PASS or HOLD receipts, freeze or verification receipts;
- provenance labels such as `controller_inference`, `internal_context` or `external_evidence`;
- Context Brief or reconciliation-table wording;
- 40 to 64 character hex hashes (links are ignored, so a commit URL is fine).

Internal context may change relevance, terminology, sequencing, fit and recommendation. It may not prove an external fact or erase a contradictory finding. Say plainly which claims come from sources and which are the analyst's judgement.

### Plain brief

`internal/plain-brief.md` answers the owner's question in plain English. It links the reference pack once, in a footer line. It carries no exhibits, codes, hashes or process words.

### Sidecars (never exported)

| File | What it holds |
|---|---|
| `synthesis/action-register.md` | Every useful idea with state, next move, owner or gate, and proof or trigger. Rows start `| AR`. |
| `synthesis/visual-exhibits.json` | The exhibit ledger |
| `qa/` and `receipts/` | QA receipts, the plain-brief review, the completion record |
| `manifest.json` | Paths, hashes, export readback, controller state |

## Required campaign artifacts

`init_campaign.py` scaffolds:

```text
campaign_state.json
internal/reference-pack.md
synthesis/action-register.md
synthesis/visual-exhibits.json
context/context-brief.md
context/context-brief.json
qa/packets/evidence-privacy.md
qa/packets/visual.md
qa/packets/taxonomy.md
qa/packets/librarian-repair.md
qa/context-handoff-review.json
qa/evidence-privacy-review.json
qa/visual-review.json
qa/taxonomy-review.json
qa/librarian-repair-receipt.json
qa/final-assurance.json
```

The `taxonomy` filenames above are the scaffold’s compatibility names; the role is simply read-only taxonomy/routing QA. Do not rename receipt paths independently of the manifest and consumers.

The plain brief is not scaffolded. The generator writes it. The manifest declares it in `plain_brief` and lists it in `deliverable_exports.allowlist`.

The manifest includes `output_layout`, `internal_outputs` (the reference pack), `plain_brief`, `context` (with `release`), `qa`, `deliverable_exports.allowlist`, `deliverable_exports.denylist`, `synthesis_sidecars` and `controller_state`. Phase order is `intake, lanes, internal_synthesis, qa, plain_brief, export_closeout`.

## QA receipt requirements

The reviewer must be distinct from the producers wherever independence is required. Acceptance reviewers must differ in developer from every actual writer, including rewriting editors and served fallbacks. All independent QA receipts must be PASS before `true_deep_research` can pass. The reviewer identity must differ from the producer or controller identity where independence is required.

- `qa/context-handoff-review.json`: isolation, provenance, freshness, privacy classification, and no promotion of internal assertions into the reference pack as source facts.
- `qa/evidence-privacy-review.json`: citations, hash stability, privacy scanning, contradictions, evidence confidence, recommendation discipline. It carries `reference_pack_sha256`, which must equal the current reference pack.
- `qa/visual-review.json`: `output_classes` has `reference_pack` and `plain_brief`, both PASS. It also covers rendered diagram validation. A diagram omitted with its note in the pack is not a reason to hold. Judge the note and the rest of the pack.
- `qa/taxonomy-review.json`: read-only deliverables routing, required frontmatter, declared tags, links and duplicate checks. `reviewed_paths` is exactly `[internal/reference-pack.md, internal/plain-brief.md]`.
- `qa/librarian-repair-receipt.json`: exact scoped packaging repairs, or PASS with no change.
- `qa/final-assurance.json`: final deterministic readback across all receipts. It carries `reference_pack_sha256`.

If deliverables export is explicitly suppressed, taxonomy review and Librarian repair may be `N/A` only with a non-empty reason. All other checks still run.

Hand-written QA or review records fail as `receipt_fabricated` in both layouts.

## Deterministic verifier gates

`verify_campaign_depth.py --mode true_deep_research` holds when:

- the reference pack, Context Brief or a QA receipt is missing, malformed, non-PASS or non-independent;
- a receipt's `reference_pack_sha256` does not match the current reference pack;
- the plain brief is not declared, missing, stale or unreviewed;
- the action register sidecar has too few rows;
- visual exhibits fall below threshold or lack anchors in the reference pack;
- a rendered structural diagram lacks a validation receipt;
- deliverable exports lack required frontmatter, have inconsistent declared tags, or fail routing and link checks;
- the allowlist is not exactly `[reference pack, plain brief]`, or names an executive brief or external pack;
- deliverables export is enabled but the exporter has not copied and read back both files.

It fails when the reference pack carries process records, the plain brief fails its gate, or a record looks hand-written.

## Strict deliverables routing

Resolve `DELIVERABLES_EXPORT_PATH` (default `~/research-deliverables`) or pass `--export-root` when scaffolding. Export directly into that flat root. Use `--no-deliverable-export` for an explicitly suppressed export. No central tag registry or navigation page is required.

```yaml
deliverable_exports:
  allowlist:
    - internal/reference-pack.md
    - internal/plain-brief.md
  denylist:
    - context/**
    - qa/**
    - lanes/**
    - synthesis/**
    - manifest.json
    - receipts/**
    - raw sources
    - transcripts
    - machine artifacts
```

`export_deliverables.py` hard-fails if any requested export falls outside the allowlist, if the allowlist names an executive brief or external pack, or if the plain-brief gate is not clean. It copies only the two files and records source and readback SHA-256 hashes. It never commits or pushes.

## Legacy layout

A campaign without `output_layout: brief-and-reference-v2` keeps four outputs: `external/original-full-research-pack.md` (clean-room, frozen by SHA-256, never exported), `internal/full-research-pack.md` (three provenance layers, reconciliation table, Action Register), `internal/executive-brief.md` and the plain brief. Its allowlist is the full pack, the executive brief and the plain brief. Its QA receipts bind `external_pack_sha256`, its visual receipt names `external_full_pack`, `internal_full_pack` and `internal_executive_brief`, and the taxonomy reviewer reviews the full pack and the executive brief. Legacy campaigns are verified, exported and completion-checked as before. They are not migrated. `init_campaign.py --output-layout legacy` scaffolds one. The legacy QA repair and re-freeze protocol was removed with the retired six-reviewer files. Recover it from git history if a legacy campaign needs it.
