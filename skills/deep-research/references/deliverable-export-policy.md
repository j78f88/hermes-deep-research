# Deliverables export policy

Deep research produces two reader outputs: the plain brief and the reference pack with evidence and sources. Only these reviewed Markdown files are eligible for export from a new campaign. Export does not authorise publication, private-context disclosure or unrelated changes.

## Hard allowlist

The manifest records the boundary in `deliverable_exports`:

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

The Context Brief, QA receipts, lane files, action register and other sidecars stay in the campaign root. An executive brief or external pack in a two-output allowlist is refused by the verifier and exporter.

## Flat export root

Resolve `DELIVERABLES_EXPORT_PATH` from the environment, defaulting to `~/research-deliverables`. Expand `~` and declare the absolute root and both exact destinations in `deliverable_exports`. The scaffold accepts `--export-root`. The exporter must use the same manifest-bound root; a different override is not permission to redirect reviewed outputs.

`export_deliverables.py` copies directly into that root:

```text
${DELIVERABLES_EXPORT_PATH}/source-research-<slug>-reference-pack-<date>.md   # reference_pack_intended_export_path
${DELIVERABLES_EXPORT_PATH}/source-research-<slug>-plain-brief-<date>.md      # plain_brief_intended_export_path
```

There is no fixed import subdirectory, central tag registry or navigation page. The export root is separate from the campaign root. Do not use symlinks or path traversal to escape either boundary.

Exactly these two files may be copied. The plain brief is eligible only when `plain_brief_rewrite.verify_campaign_plain_brief` is clean: lint PASS, independent editor PASS, current brief and reference-pack hashes, and resolved factual editor findings. Otherwise nothing is copied.

## Suppression and privacy

Use `--no-deliverable-export` when scaffolding an explicitly suppressed export. Record the reason in the manifest. Suppress export when:

- the owner requests corpus-only, external-only or no deliverables export;
- the outputs contain customer, private or confidential data outside the approved destination's audience;
- public or client delivery requires excluding internal context;
- a degraded or no-material-delta scan should not create durable deliverables;
- a governed project corpus is the approved destination instead.

When suppressed, taxonomy/routing QA and packaging repair may be `N/A` only with explicit reasons. Evidence/privacy, visual, context-handoff and final-assurance checks still run. An export switch or configured path never grants permission to disclose private context.

## Reader requirements

`internal/reference-pack.md` must include:

- required frontmatter: `title`, `created`, `type`, `stage`, `source`, `author`, `captured_via`, `tags` and `related`;
- the research question, purpose and evidence boundary;
- evidence-led findings, relevant analysis, uncertainty, contradictions, gaps and sources;
- the applicable exhibits and adversarial or breakpoint analysis;
- technical judgement, alternatives and what would change the recommendation only when requested.

Citation codes such as `[S12]` are allowed in the reference pack. Process records are not: Action Register rows or headings, QA/review receipts, provenance labels such as `controller_inference`, Context Brief wording or 40 to 64 character hashes cause `reference_pack_process_records`. Local machine paths are not public evidence.

`internal/plain-brief.md` must pass `plain_brief_rewrite.py --check`, carry required frontmatter and link the reference pack in its footer. Its PASS review must bind the current brief and reference-pack hashes. The editor's developer differs from every actual writer, including rewriting editors and served fallbacks. See `plain-brief-output.md`.

Keep declared tags coherent without requiring a global vocabulary. An optional `deliverable_exports.allowed_tags` list binds a campaign-specific vocabulary into the reviewed manifest; if supplied, it must be nonempty and off-vocabulary tags are refused. Resolve related-file links against actual deliverables, not mandatory navigation documents. Set metadata, destination filenames and links before review; later edits invalidate hash-bound acceptance.

## Read-only destination preflight

Before copying, taxonomy/routing QA checks:

- only the two allowlisted Markdown files are staged;
- required frontmatter, declared tags and related-file links are valid;
- both intended destinations are directly under the admitted export root;
- the exact destination files have no collision or unapproved concurrent change;
- context, QA, lanes, raw sources, transcripts and other sidecars remain excluded.

`export_deliverables.py` fails closed when a destination already exists. Replacement is a destructive, owner-gated action, not routine export. An approved refresh uses `--overwrite-approved`, archives prior exported deliverables under `orchestration/prior-deliverable-exports/`, and records `manifest.deliverable_exports.prior_exports`. Review gates still apply.

Packaging repair is limited to scoped, reversible defects in the reviewed files and their staged copies. It must not alter unrelated files, templates, governance, navigation or destination policies. A repair affecting accepted bytes requires fresh hash-bound review.

## Export procedure

Run from the installed skill directory after final synthesis and QA:

```bash
export DELIVERABLES_EXPORT_PATH="${DELIVERABLES_EXPORT_PATH:-$HOME/research-deliverables}"
export RESEARCH_ARCHIVE_ROOT="${RESEARCH_ARCHIVE_ROOT:-$HOME/.hermes/research-archive}"
python3 scripts/export_deliverables.py \
  --campaign-root "$RESEARCH_ARCHIVE_ROOT/runs/<YYYY-MM>/<run-id>" \
  --export-root "$DELIVERABLES_EXPORT_PATH"
```

Use the current absolute campaign root if the run is still in local staging. The archive path is an example for a retained run. The export root must match the manifest's reviewed destination bindings.

The exporter hard-fails on an out-of-allowlist path, unclean plain-brief gate, forged or stale QA receipt, unverified provider evidence or unapproved existing destination. After copying, it reads back the exact files, compares SHA-256 hashes, updates `manifest.json` and writes a receipt. It does not commit, push, index or synchronise files.

If separately configured remote delivery is in scope, verify that exact remote destination independently. Local copy is not remote publication proof. Do not mutate navigation pages or invoke another service merely because an export directory exists.

## Verification

Before closeout:

- [ ] `verify_campaign_depth.py --campaign-root <root> --mode true_deep_research` returns PASS, or the actual HOLD/FAIL and any owner-approved downgrade are reported.
- [ ] Both reader files exist and Context Brief material remains excluded from export.
- [ ] Evidence/privacy, visual, taxonomy/routing, packaging-repair/no-change and final-assurance receipts are PASS, or valid export-only N/A with reasons. Evidence/privacy and final assurance bind the current reference-pack hash.
- [ ] Exactly the two allowlisted files were copied directly into the configured root, or export is explicitly suppressed with a reason.
- [ ] Frontmatter and link checks pass without a central registry or required navigation document.
- [ ] Source and exact destination readback hashes match and are recorded in `deliverable_exports`.
- [ ] No context, QA, lanes, sidecars, manifest, receipts, raw sources, transcripts or machine artifacts were exported.
- [ ] `complete_campaign.py --campaign-root <root>` returns its real verdict. Only PASS establishes completion.

## Legacy layout

A campaign without `output_layout: brief-and-reference-v2` keeps the older allowlist: `internal/full-research-pack.md`, `internal/executive-brief.md` and, when declared, `internal/plain-brief.md`. The external pack remains corpus-only. The taxonomy reviewer checks the full pack and executive brief. Destinations remain directly under the configured flat export root, with exact filenames declared in `deliverable_exports`. These campaigns retain their applicable evidence and review checks; changing layout does not admit an unknown purpose-less campaign or waive acceptance.
