# Owned assets, coverage and build-now

Integrity checks prove that reported claims are supported; they do not prove that research covered the right sources. Before research dispatch, inventory reusable assets, measure catalogue coverage, check platform features and test tool availability. For example, an existing Apify monitor recipe and the `competitor-news-monitor` skill may remove work that a new design would otherwise duplicate. These five rules apply to every new campaign. Confirm that any named recipe or companion skill is actually installed; its name alone is not working proof.

## 1. Owned-assets inventory runs first and is shared with every worker

Before any research thread starts, the controller writes `context/owned-assets.md` and `.json`. It lists facts only:

- accounts and plans the owner already pays for (e.g. Apify business plan, OpenRouter, Linear), with what they allow under standing policy;
- built recipes, skills, scripts, apps and running jobs that touch the topic (search your installed skills, local apps, `hermes cron list` and closed tracker issues);
- tools and plugins available in this session and whether they work (see rule 4).

Every research worker packet includes this file. The clean-room rule still excludes opinions, decisions, private history and preferences. Only an explicitly reviewed, shareable inventory of tool capabilities is admitted; it does not authorise exposing credentials, private identifiers or account history. A worker that ignores an owned asset in its area fails its rubric.

Record each owned asset as a candidate in the pre-seed candidate universe with `class: current_state_do_nothing` or `product`, so it is compared, not forgotten.

Carry owned assets through to the answer. Keep a disposition table in `synthesis/owned-asset-dispositions.json` with one row per relevant owned asset: its exact name, the evidence level (name only, contents read, or proven working), the decision (reuse, compare, unsuitable or unknown) and the findings section that uses it, or the reason it was left out. A decision-relevant owned asset must appear by its exact name in the reader-facing findings, not only in a ledger. A folder that exists is not proof that it works.

## 2. User-named owned assets are leverage, not anchoring risk

When the owner names an asset they already have ("the business Apify plan we have"), the pre-seed still records it as a seed, but the campaign must also answer: "what is the fastest, best use of this asset for the goal?" The neutral comparison covers alternatives. It must not demote or under-research the owned asset. Give it at least the evidence budget of any alternative plus a platform-features check (rule 3).

## 3. Catalogue and platform coverage is exhaustive and measured

- Any store, marketplace, registry or catalogue survey (Apify Store, MCP registries, n8n templates, GitHub topics, model hubs) must page through all results for each query, not the first page. Record per query: results available, results read, coverage ratio. Below 100% needs a written reason.
- Count saved response records and distinct identifiers separately. A reported total, limit or page count does not prove those records were returned. Preserve offsets, actual array lengths and duplicate counts. Use bounded paired probes to explain early empty pages or filtering. State the observed limit and uncertainty rather than inventing backend causes or claiming exhaustive coverage.
- Use at least 15 queries built from the user's goal, synonyms and job-to-be-done terms (for example "news monitoring", "digest", "alerts", "changelog", "rss", "change monitor", "ai news", "newsletter"), plus the platform's category browse.
- For every named platform, check its platform features as well as its products: saved tasks, schedules, webhooks and integrations, monitoring and alerting, MCP or agent interfaces, storage and dataset dedupe. A platform feature that removes custom work is a finding.
- Keep a checklist of every distinct product surface or concept the question names (for example a desktop bot mode versus messaging adapters). Each needs its own supported finding or an explicit 'not researched' gap. Full catalogue coverage does not close a separate surface.
- The reference pack reports coverage numbers in a table. The independent evidence/privacy reviewer must return HOLD for missing coverage numbers or incomplete catalogue work, recording shortfalls honestly. Existing deterministic quote/checker/verifier passes do not prove these research obligations; this is an instruction and independent-review requirement, not a new verifier gate.

## 4. Tool preflight before research

Before dispatch, prove each tool the lanes need actually works in this runtime: web search, page extract, crawl, the platform's plugin or MCP tools, and API tokens by presence only. Record results in `receipts/tool-preflight.json`. A broken tool is fixed (if within approved scope) or named as a gap in the steering packet and in the brief. Workers must not silently fall back to a weaker method without the controller knowing.

## 5. Build-now path when the ask is a build

If the owner's ask includes "build", "have ready", "set up", "pathway to use", or names a delivery (for example "send to my Discord"), the pack must lead with a build-now section:

- what can run within hours using owned assets and standing approvals;
- the exact steps, and which ones need an owner decision (schedule, spend, config);
- then, separately, what a longer staged plan would add.

Do not default to a shadow period or multi-week roadmap unless evidence shows the fast path is unsafe. Any time estimate is labelled as an estimate, with its prerequisites listed. If no reusable asset or approval exists yet, say what is missing instead of promising hours. Ask the owner-gate questions early in the run, not at the end, so activation can happen in the same session.

## Controller checklist (add to steering.md)

- [ ] Every decision-relevant entity reaches the findings by its exact name or ID, with one supported claim and a source, or an explicit omission reason. Generic words such as 'skill' or 'tool' never count as naming it.
- [ ] `synthesis/owned-asset-dispositions.json` written.
- [ ] Every named product surface has its own finding or a stated gap.

- [ ] `context/owned-assets.md` written before dispatch and attached to every worker packet.
- [ ] Linear Done/Canceled issues on the topic scanned and their reusable builds listed.
- [ ] Every catalogue lane reports coverage ratio per query.
- [ ] Platform-features check done for each named platform.
- [ ] `receipts/tool-preflight.json` written; broken tools fixed or named.
- [ ] If the ask is a build: build-now section first; owner-gate questions asked before synthesis.
