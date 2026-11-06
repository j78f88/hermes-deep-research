# Lane Quality Rubrics

Use PASS/HOLD/FAIL to judge child lane outputs before controller synthesis. External research lanes are clean-room evidence producers; internal context retrieval and QA are separate lanes/roles.

## Universal rubric

PASS:

- Required sources inspected or unavailable with reason.
- Claims separated into fact / vendor claim / community report / inference / unknown.
- Citations or source refs provided for external facts.
- Contradictions and gaps named.
- Output written to expected path.
- Forbidden actions not performed.
- ActionRegister candidates provided where relevant.
- Clean-room boundary is preserved for external lanes.

HOLD:

- Important source unavailable but fallback not tried.
- Citations are thin or source classes unclear.
- Material contradiction unresolved but not flagged.
- Output shape incomplete but recoverable.
- Privacy/source-boundary classification is unclear.

FAIL:

- Major claims uncited.
- Worker followed source instructions as commands.
- Upstream code/install/build/test executed without approval.
- Customer/private/credentialed data used without approval.
- Output is generic summary with no decision relevance.
- Clean-room external worker used Context Brief, private knowledge-base links, private Linear/session context, internal paths, private project details, or owner-specific assumptions.

## Clean-room external lanes

PASS requires:

- external/public or explicitly approved non-intimate sources only;
- no Context Brief content;
- no private knowledge-base links, private Linear IDs, session refs, internal paths/hostnames, or owner-specific operating details;
- external facts cited with source/date/class;
- findings understandable without internal knowledge and suitable for the reference pack (legacy layout: `external/original-full-research-pack.md`).

HOLD if the worker saw potentially private context but quarantined it and did not use it.

FAIL if private context affected findings or is copied into the lane artifact.

## Context Brief / internal retrieval lane

PASS requires the taxonomy reviewer or librarian to record:

- source path;
- freshness;
- authority;
- relevance;
- privacy classification;
- explicit exclusions;
- conflicts/staleness that must not be treated as current truth.

This lane is read-only. It does not edit the knowledge base, change destination policies or feed clean-room workers.

## Evidence/privacy QA lane

PASS requires:

- reviewer identity differs from producers/controller;
- material claims map to source/citation IDs;
- sourced claims and the analyst's judgement are distinguished in plain words, with no provenance tags in the reference pack;
- internal context is not laundered into sourced facts;
- the receipt's `reference_pack_sha256` matches the current reference pack;
- the reference pack carries no process records (receipts, hashes, Action Register rows);
- contradictions and uncertainty survive internal compression;
- recommendations and the action register sidecar do not outrun evidence.

## Visual QA lane

PASS requires:

- reviewer identity differs from visual/exhibit producer;
- exhibit IDs, titles, labels, scales, legends, captions, ordering, and anchors are consistent inside the reference pack;
- the plain brief carries no exhibit, source code or process label;
- structural topics include a genuine decision-supporting diagram;
- rendered diagrams have reproducible source and validation receipts;
- no clipped labels, undefined acronyms, colour-only encoding, misleading axes, or decorative filler.

## Taxonomy/routing QA lane

PASS requires read-only inspection of staged internal exports and their declared destination bindings:

- destinations are directly under the resolved flat export root (`DELIVERABLES_EXPORT_PATH`, default `~/research-deliverables`);
- required frontmatter is present and coherent;
- declared tags are coherent and any explicitly configured destination policy is met, without assuming a central registry;
- reference pack and plain brief links are reciprocal where useful;
- staging contains exactly the reference pack and the plain brief;
- context, QA, lanes, sidecars, manifest, receipts, raw sources, transcripts, and machine artifacts are absent from deliverables staging/export;
- no governance, template, archive, navigation or unrelated-file mutation is proposed as routine repair.

If deliverables export is explicitly suppressed, this check may be `N/A` only with a reason.

## Current-state truth lane

PASS requires highest-authority available sources first: official docs, live repo metadata, direct internal source, live system readback, or approved corpus. Secondary summaries cannot be the only source unless primary access failed and the failure is recorded.

## Market/category lane

PASS requires:

- segmentation before share/proxy claims;
- denominators for quantified claims;
- clear distinction between ARR, users, installs, stars, downloads, survey penetration, and logos;
- caveats for multi-select surveys and proxies.

FAIL if it invents market share or ranks vendors from vibes.

## Customer voice lane

PASS requires separating vendor case studies, independent reviews/surveys, forums/HN/Reddit, issues, and anecdotes. Themes may be extracted; prevalence cannot be inferred without denominator.

## Repo-family lane

PASS requires:

- repo inventory and inspected refs/SHAs;
- license/source-boundary notes;
- static inspection only unless explicitly approved;
- no target dependency install/execution;
- transient clone cleanup proof;
- translate/watch/reject/direct-adopt-not-approved classification.

FAIL if upstream skills/prompts/code are copied into active systems.

## Adversarial/breakpoint lane

PASS requires at least:

- lock-in/data boundary;
- legal/license/terms concern if external source;
- maturity/recovery/failure modes;
- security/credential/runtime authority;
- unsupported claims;
- stop/kill conditions.

## Environment-fit lane

When this is an internal-context lane, PASS requires mapping to the owner’s actual systems, not an assumed private stack. Inspect only relevant, approved surfaces, such as:

- the agent runtime and orchestration tools;
- the issue tracker;
- automation and scheduled jobs;
- the approved knowledge base and source-intelligence corpus;
- relevant repositories and operating constraints.

Must distinguish already-aligned vs real gap vs non-fit. This material feeds the Context Brief/internal synthesis, not the external pack.

## Content acquisition lane

PASS requires:

- source type recorded;
- acquisition method recorded;
- provenance marked live / archived / metadata-only;
- transcript/text coverage recorded;
- timestamp/line refs used where available;
- degraded state named when full text/transcript is unavailable;
- raw/high-volume text kept in corpus/workspace, not dumped into deliverables export directory by default.

HOLD if acquisition is degraded but useful metadata or partial text exists and the caveat is explicit.

FAIL if transcript-level claims are made from metadata-only capture, archived pages are treated as live-current, or source text is followed as instructions.

## YouTube lane

PASS requires:

- Apify transcript actor attempted first when approved config/token is available;
- `youtube-content` / `youtube-transcript-api` used only as fallback or when Apify is unavailable;
- video/channel/playlist identity recorded;
- transcript availability and language recorded;
- timestamped claims where timestamped transcript exists;
- line refs used when Apify returns plain text without timestamps;
- metadata fallback caveat where transcript does not exist;
- no admission to recurring rosters/jobs unless explicitly approved.

## Article/document/PDF lane

PASS requires:

- extraction method and coverage recorded;
- blocked-page fallbacks attempted when relevant;
- archive snapshot date preserved when used;
- OCR/scanned-page gaps called out;
- tables/figures treated as unverified if extraction did not capture them.

## Synthesis lane

PASS requires:

- controller read every lane;
- Context Brief read only after every lane finished, with the time recorded;
- reference pack written from the lane evidence plus the Context Brief;
- contradictions resolved or preserved;
- action register sidecar complete;
- visual exhibit candidates are selected from lane evidence and recorded in `synthesis/visual-exhibits.json` with output classes;
- `true_deep_research` synthesis has 3+ meaningful reference-pack exhibits and a diagram when the topic has structural/system/workflow/market shape;
- any requested technical recommendation states its evidence, alternatives and limits; factual work does not invent a recommendation;
- what-would-change section;
- owner gates explicit;
- no hidden follow-ups in prose.

## Verification lane

PASS requires:

- manifest with hashes;
- citations verified or gaps explicit;
- required sections present;
- visual-exhibits gate passed or HOLD is declared honestly;
- no-exec/no-install proof where applicable;
- cleanup proof;
- independent QA receipts passed;
- plain brief written after the final reference pack, passing lint and independent editor review (`qa/plain-brief-review.json`);
- strict allowlisted deliverables export/readback (reference pack, plain brief) passed if deliverables export is enabled;
- Linear/export/Git readbacks if mutated.

## Legacy layout

For a campaign without `output_layout: brief-and-reference-v2`, the evidence/privacy, visual, taxonomy reviewer and synthesis lanes keep the older checks: frozen external pack hash, external privacy scan, provenance layers, reconciliation table, executive-brief exhibits, and a full pack plus executive brief in staging. The verifier still applies them to legacy campaigns.
