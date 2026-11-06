# Target-Type Router

Use this to select child lanes for a deep-research campaign. Always include universal lanes, then add target-specific lanes.

## Pre-dispatch and cross-cutting routing

Apply `references/owned-assets-coverage-and-build-now.md` to BOTH `brief-and-reference-v2` and legacy campaigns. Assign the work below to the existing lanes; do not add a new research engine or bypass the pre-seed gate.

1. **Steering + source map / Current-state truth, before research dispatch:** the controller gathers facts and writes `context/owned-assets.md` and `.json`: paid accounts/plans and capabilities allowed under standing policy; relevant recipes, skills, scripts, apps and running jobs; available tools and their evidence levels. Search your installed skills, local apps, `hermes cron list` and closed tracker issues read-only. For an AI-news/Apify-monitoring ask, explicitly check the existing Apify monitor recipe and `competitor-news-monitor`; verify their relevance and evidence, do not assume they work. Record inaccessible sources as gaps, not absence of assets. Register each relevant asset in the pre-seed candidate universe as `current_state_do_nothing` or `product`. A user-named owned asset gets at least any alternative's evidence budget plus a platform-features check, and an answer on its fastest, best use.
2. **Steering + source map, before research dispatch:** preflight each needed web-search, page-extract, crawl and platform plugin/MCP tool in this runtime using safe, authorised checks; check API tokens by presence only. Write actual working/failed/unavailable results to `receipts/tool-preflight.json`. Fix only within approved scope, or record the gap and controller-known fallback in steering and the brief; never silently weaken the method. Attach this receipt and both factual inventory files to every worker packet before dispatch. The factual exception never permits passing the Context Brief, opinions, preferences or private history to workers. Keep private IDs/paths in internal sidecars, out of legacy external outputs; provide exact shareable asset names to those outputs, or record the naming gap internally.
3. **Product truth / Market + competitors / prior-art lanes, when surveying a catalogue:** plan at least 15 distinct goal/synonym/job-to-be-done queries plus category browse. Exhaust all pages of each query and browse; retain offsets/cursors and actual `len(items)` per response. Report advertised available, actual returned, actually read, read/available coverage and shortfall/reason per query/category in a coverage table for the reference pack (legacy: external pack, with private details excluded). Keep duplicate handling explicit. A requested limit, advertised total, capped offset-0 page or unknown/changing denominator is not full coverage; explain every ratio below 100% and mark unknown ratios honestly. The verification lane must HOLD a catalogue lane missing coverage numbers.
4. **Product truth / architecture/API / Evidence / claims map:** check every named platform's saved tasks, schedules, webhooks/integrations, monitoring/alerting, MCP/agent interfaces, storage and dataset dedupe, including features that remove custom work. Steering maintains a checklist of every named product surface/concept; the responsible lane supplies a supported finding or explicit `not researched` gap for each. Full catalogue coverage does not substitute for a distinct named surface.
5. **Current-state truth / environment-fit implications → Controller synthesis:** return one disposition row per relevant owned asset and consolidate in `synthesis/owned-asset-dispositions.json`: exact name, evidence level (`name only`, `contents read`, `proven working`), decision (`reuse`, `compare`, `unsuitable`, `unknown`) and findings section or omission reason. Carry every decision-relevant exact name into reader-facing findings with a supported claim/source or explicit omission reason, respecting the legacy external privacy boundary above; a ledger-only mention or generic 'tool' does not count. A folder or name is not working proof.
6. **Steering / environment-fit implications → Synthesis + board/plain brief:** if the ask includes build, have ready, set up, pathway to use or a named delivery (for example Discord), lead with a build-now section: exact steps using owned assets/standing approvals, estimated hours explicitly labelled as estimates with prerequisites, and schedule/spend/config owner gates. Ask those questions early, before synthesis; do not activate gated work. If reuse or approval is missing, say what is missing rather than promise hours. Put longer staged additions separately; a shadow period or multi-week roadmap needs evidence that the fast path is unsafe.
7. **Board brief + verification:** manually check inventory/preflight timing and worker attachments, named assets/dispositions in findings, coverage tables, platform features, separate named surfaces and conditional build-now output. Record gaps honestly. Existing checker/verifier passes do not prove these coverage requirements were met; static instructions are not deterministic coverage or acceptance of an earlier campaign.

## Universal lane set

Use for every non-trivial campaign:

1. Steering + source map
2. Current-state truth
3. Evidence / claims map
4. Adversarial / breakpoints
5. Environment-fit implications
6. Controller synthesis + ActionRegister
7. Board brief + verification, when decision-facing or long

## Product or company

Add:

- product/docs/pricing/security/trust truth;
- content doctrine / founder essays / talks;
- market/adoption proxies;
- competitor/vendor matrix;
- customer voice and public failure modes;
- repo-family no-exec scout if public repos exist;
- pilot/procurement gate.

Default children:

1. Steering + source map
2. Product truth
3. Content doctrine
4. Market + competitors
5. Customer voice + proof
6. Repo-family no-exec scout
7. Adversarial/procurement breakpoints
8. Environment fit
9. Synthesis + board brief + verification

## Market or category

Add:

- segmentation;
- adoption/share proxies;
- vendor matrix;
- customer evidence;
- independent studies;
- economics/moats/consolidation;
- scenarios.

Skip repo-family unless specific vendors/repos matter.

## Repo, tool, or library

Add:

- repo no-exec scout;
- architecture/API map;
- license/security/supply-chain review;
- install/runtime risk;
- translation candidates;
- sandbox trial gate if hands-on work may later be approved.

Do not run target package managers, builds, tests, installers, CLIs, or hooks unless separately approved.

## Technical idea or architecture

Add:

- prior art / alternatives;
- current internal state;
- design trade-offs;
- implementation seams;
- verification strategy;
- failure modes.

Board brief may be replaced by an ADR-style decision packet if that is the audience.

## Internal system or process

Add:

- source-of-truth map;
- current workflow and pain evidence;
- stakeholder/user journey;
- failure modes;
- improvement options;
- rollout/rollback and metrics.

Avoid external market lanes unless buying/benchmarking is part of the decision.

## Customer workflow / service opportunity

Add:

- current workflow and systems-of-record map;
- jobs-to-be-done;
- operational obligations;
- service blueprint;
- risk/compliance gates;
- proof-of-value plan.

Treat live customer data, credentials, outreach, or production mutations as owner-gated.

## Policy / governance

Add:

- authority map;
- existing rules and precedents;
- risk cases;
- proposed standard;
- exception handling;
- enforcement/readback.

Output may be policy packet rather than market-style pack.

## Venture / investment thesis

Add:

- customer/problem evidence;
- market/category structure;
- alternatives;
- wedge/differentiation;
- proof plan;
- risks;
- go/no-go gates.

Keep external claims cited and avoid inventing market share where no denominator exists.

## Content/source doctrine

Add:

- source claim extraction;
- repeated ideas;
- contradictions;
- applicability to the owner;
- translation/non-copy boundary;
- ActionRegister.

Do not copy proprietary text, UI, prompts, or source behavior into implementation.

## YouTube video / channel / playlist

Also load `youtube-content` and `references/content-and-youtube-acquisition-router.md`.

For a single video, add transcript/metadata acquisition, claim/timestamp extraction, doctrine/pattern extraction, cross-source validation, applicability, and degraded-state verification.

For a channel or playlist, add identity/scope, recent or selected-window inventory, representative transcript/sample acquisition, repeated claim clusters, contradiction/staleness analysis, admission/watch/reject decision, and ActionRegister.

If transcript capture fails, record `metadata_only`, `transcript_unavailable`, or `blocked_by_platform`; never imply transcript-level coverage from metadata.

## Article / post / newsletter

Also load `blocked-page-recovery` when extraction fails. Add page acquisition/provenance, claim extraction, evidence/support quality, author/source trust and incentives, applicability, and non-copy translation.

Archived snapshots must cite snapshot date and must not be presented as live-current.

## PDF / document / report

Use PDF/document extraction or OCR skills as needed. Add text extraction coverage, claim/table extraction, methodology/evidence quality, caveats, applicability, and ActionRegister.

If extraction coverage is poor or pages are scanned, mark degraded and do not claim full-document coverage without OCR/vision proof.

## Podcast / talk / audio

Prefer transcript source. Add metadata/transcript acquisition, claim extraction with timestamps where available, speaker/source context, cross-check of key claims, applicability, and ActionRegister. If no transcript can be obtained, mark degraded unless a transcription path is approved.

## Mixed content corpus

Add corpus manifest/source classes, acquisition/degraded-state report, claim clusters, contradiction/staleness analysis, pattern/doctrine synthesis, environment fit, ActionRegister, and verification.
