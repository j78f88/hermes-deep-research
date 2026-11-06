# QA repair and re-freeze protocol

**Legacy layout only.** This applies to campaigns with a frozen external pack (no `output_layout: brief-and-reference-v2`). The two-output layout has no external pack and no freeze. There, repair the reference pack directly, rerun the evidence/privacy QA so it binds the new `reference_pack_sha256`, and rerun the plain brief.

Use when independent evidence/privacy or visual QA returns FAIL/HOLD on the already-frozen external pack.

## Rules

1. Preserve v1 byte-for-byte (e.g. `receipts/external-pack-v1-frozen.md`) and assert its hash before overwriting.
2. Edit only locations named in QA findings/requested repairs. The controller has usually read the Context Brief by now, so any unrequested edit is a contamination risk.
3. Re-assemble from part files, re-run the external privacy scan, re-freeze as v2, and append a `versions[]` entry to `receipts/external-freeze.json` with reason, scope and contamination control.
4. Write `receipts/external-v2-repair-log.json` mapping each change to a QA finding.
5. Update `manifest.external_output` (frozen_sha256, sha256, version, v1_sha256) and any Context Brief text that quotes the frozen hash.
6. Re-dispatch independent re-reviews: evidence/privacy (verify every prior finding RESOLVED + fresh sample of unsampled claims), visual, taxonomy reviewer, and a **context-handoff delta review** that diffs v1 vs v2 and maps every hunk to a QA finding with no private context.

## Failure patterns evidence QA reliably catches

- Architecture/design prescriptions written inside `[fact]` or `[vendor_claim]` bullets. Split each bullet: sourced fact first, then `**[inference]**` for the design rule.
- Synthesis-derived numbers (blinded scores, sensitivity, proof-plan counts) carrying external citation tokens. Attribute them to the scoring record or pack section instead.
- Negative claims ("no public API located") cited to an unrelated source. Label as `**[gap]**` search result, not proof of absence.
- Survey populations mislabelled (e.g. "directors" for business leaders) and calculated aggregates presented as quoted figures. State components and mark the aggregate as calculated.
- Study-specific error rates generalised. Scope to dataset, date and configuration.
- Claims about the scorer's model family without an on-disk record. Write `receipts/scorer-provenance.json` (scorer model/vendor from the delegation completion record, synthesis model, scoring.json hash) at scoring time.
- Brief omitting a full-pack caveat. Copy the full caveat list, then shorten wording only.

## Failure patterns visual QA reliably catches

- Decision-use lines that contradict row values in the same table.
- Ordinal ratings (Low-medium, High-critical) without an ordered scale and a stated residual rule.
- Human gates drawn as side-links off completed outcomes. Put gates inline as prerequisites with a legend.
- State machines with dead-end states (e.g. Amended). Loop amendments back through review, adoption and signature, and show legal hold, inspection export and privilege side paths.
- Diagrams mixing build-time promotion and runtime traffic with the same arrow style.
- Brief exhibits that "simplify" a full-pack table by dropping conditions. Keep phase-for-phase equivalence.
- Mermaid strips wider than about 3:1. Use `flowchart TB` and invisible links (`A ~~~ B`) inside subgraphs to stack nodes. Record dimensions in `synthesis/diagrams/render-index.json`.

## Rendering without a vision backend

Mermaid CLI renders headlessly via `npx -y @mermaid-js/mermaid-cli@11 -p pp.json` with `PUPPETEER_EXECUTABLE_PATH` pointed at the Playwright headless shell (`~/.cache/ms-playwright/chromium_headless_shell-*/.../chrome-headless-shell`) and `pp.json` = `{"args":["--no-sandbox"]}`. Parse success plus aspect ratio is the fallback when vision returns 404.
