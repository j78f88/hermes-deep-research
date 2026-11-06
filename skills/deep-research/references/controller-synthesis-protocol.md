# Controller Synthesis Protocol

The controller synthesis is the core of deep research. Do not concatenate worker summaries. A campaign produces two outputs: the reference pack and the plain brief. The controller writes the reference pack. The plain brief generator writes the brief.

## Inputs

Research inputs:

- Steering packet;
- research lane outputs;
- citation ledger;
- source/corpus manifest;
- source acquisition receipts.

Internal inputs, read only after all research lanes finish:

- `context/context-brief.md` and `.json`;
- relevant-history scan artifact;
- internal source/corpus manifest entries;
- Linear parent/child state if used.

Do not provide Context Brief material, private knowledge-base context, Linear/private context, prior-session excerpts, internal source paths, or owner-specific assumptions to research workers. Record the time the controller first reads the Context Brief with `campaign_controller.record_context_release(root)`.

## Steps

1. **Inventory lane outputs.** List each lane, status, artifact path, PASS/HOLD/FAIL, gaps, contradictions, source refs, and visual candidates.
2. **Extract claims.** Pull material claims into a table with source/citation, evidence class, confidence, and lane origin. Internal context cannot satisfy a citation requirement.
3. **Find contradictions.** Compare lanes for conflicts: product truth vs repo truth, vendor claims vs customer reports, public issue reports vs release notes, stale docs vs current docs.
4. **Resolve or preserve contradictions.** Resolve with higher-authority/current evidence where possible. If not resolvable, preserve dissent explicitly.
5. **Inventory the Context Brief.** After every lane has finished, read `context/context-brief.md`/`.json`. Check source path, freshness, authority, privacy classification, exclusions, and conflict markers.
6. **Write the reference pack.** Write `internal/reference-pack.md`: question-led evidence, current state, analysis, relevant exhibits, limitations and sources. Add options and what would change the call only when judgement was requested. Cite claims with source codes. Internal context may change relevance, terminology, fit, sequencing and the recommendation. It may not prove an external fact, and it may not blend away a contradiction. Say in plain words which claims are sourced and which are your judgement. Do not use provenance-layer headings or labels such as `controller_inference`. Do not put hashes, receipts or Action Register rows in the pack.
7. **Write the action register sidecar.** Every useful idea gets a state, next move, owner/gate, and proof/trigger in `synthesis/action-register.md` (rows start `| AR`).
8. **Choose visual exhibits.** Write/update `synthesis/visual-exhibits.json` before final closeout. Include output class `reference_pack`, source data refs, caption, encoding, scale/legend, anchors, render method, artifacts, validation receipt and reviewer. For structural topics, include at least one decision-supporting diagram.
9. **Run independent QA.** Evidence/privacy QA, visual QA, taxonomy/routing QA, Librarian repair/no-change, and final assurance are not optional self-review. Worker self-report is not proof. Evidence/privacy and final assurance bind the reference pack hash.
10. **Write the plain brief.** After QA accepts the final reference pack, run `scripts/plain_brief_rewrite.py --campaign-root <root>`. The plain brief is what the owner reads first; keep source codes, hashes and receipts out of it (they stay in the reference pack and the sidecars).
11. **Verify.** Run citation, plain-brief, manifest, cleanup, required-section, visual-exhibits, privacy, deliverables export/readback, Linear/export/Git checks, then `scripts/complete_campaign.py`.

## Contradiction table

Use this format in the reference pack when material, with plain column names:

| Topic | What the sources say | What the owner's context says | Implication | Confidence / caveat |
|---|---|---|---|---:|

## Evidence class hierarchy

Prefer, in order:

1. Live/readback source of truth for current internal/system facts.
2. Primary official docs/product/security/pricing/release notes.
3. Repository metadata/source at pinned refs for repo truth only.
4. Reputable independent reporting/studies.
5. Vendor case studies and customer pages.
6. Community reports/issues/forums.
7. Analyst inference from above.

Lower-tier evidence can identify risks and hypotheses, not prove high-confidence conclusions alone. Internal context can guide applicability and prioritization, but it cannot upgrade an external fact's confidence.

## Final recommendation rules (when requested)

Factual work does not require a recommendation. When technical judgement is requested, a final recommendation must include:

- answer first;
- why;
- major caveats;
- what to do next;
- what not to do;
- what evidence would change it;
- exact owner gates.

Never imply execution approval from research approval.

## Quantitative retention and capability gaps

1. Put high-signal commercial, technical, and benchmark numbers into decision tables in the reference pack body. Do not drop them. Do not append a raw `# Detailed evidence` ledger dump. `verify_campaign_depth.py` holds `brief-and-reference-v2` campaigns when `lanes/*.json` hold five or more quantitative claims that vanish from the pack body.
2. Check every `[N]` or `[S12]` citation against its bibliography URL. Run `validate_reference_sources_section()` to confirm the pack has a single `## Sources` heading. Do both before calling `plain_brief_rewrite.py`.
3. Add a `## Capability gaps worth considering` section to the reference pack for market, ecosystem, product, and policy campaigns.
