# Instantiation Workflow

Use this when starting a new deep-research campaign from a target/link/idea.

## Intake fields

Collect or infer:

- target name;
- target type;
- primary link/source;
- decision question;
- audience;
- output boundary;
- owner gates;
- whether Linear parent/children are explicitly requested or already accepted;
- whether deliverables export is explicitly suppressed.

If the owner says “deep research <link>” and gives no extra details, default:

- `report_purpose: factual`; explain what the target is, how it works, what the evidence establishes and what remains unknown;
- use `decision_assessment` only if technical judgement was requested; do not infer a buy/build/kill recommendation from the topic;
- audience: the requester and future readers;
- output boundary: source-scout campaign root + reference pack + plain brief (what the owner reads first);
- deliverables export: only `internal/reference-pack.md` and `internal/plain-brief.md` unless the owner explicitly says corpus-only / no deliverables export / external-only;
- no public outreach, spend, credential use, upstream execution, production/customer data, destructive actions, or implementation.

The request itself authorizes all ordinary work inside that boundary. Do not pause for permission to scaffold, inspect sources, dispatch workers, use safe fallbacks, write local/private artifacts, synthesize, verify, run independent QA, or export the allowlisted internal outputs to the approved deliverables export directory. Ask only if a true owner gate is actually required.

## Procedure

1. **Route and load skills.** Load `deep-research` and any installed source-intelligence/citation guidance. If Linear mutation is approved, load its integration and the applicable issue-tracker policy. Missing companions are explicit capability gaps, not permission to invent tools or skip acceptance.
2. **Select lane plan.** Use `target-type-router.md`; choose 5–9 lanes.
3. **Create campaign root.** Use `scripts/init_campaign.py` or create the structure manually. Resolve `DELIVERABLES_EXPORT_PATH` (default `~/research-deliverables`), `RESEARCH_ARCHIVE_ROOT` (default `~/.hermes/research-archive`) and the optional `SOURCE_INTELLIGENCE_ROOT` (unset by default) as described in `../SKILL.md#local-paths-and-configuration`; pass absolute campaign and export paths using `--root` and `--export-root`. Confirm it includes `internal/`, `context/`, `qa/`, `synthesis/`, `manifest.output_layout`, strict `deliverable_exports` allowlist metadata, and `campaign_state.json` controller state.
4. **Run internal retrieval separately.** Unless the owner requested external-only research, check prior sessions, private knowledge-base records, Linear issues, and likely source/corpus roots. Record candidates with source path, freshness, authority, relevance, privacy classification, and conflicts. Do not pass this material to research workers.
5. **Create Context Brief.** Librarian writes `context/context-brief.md` and `.json` with accepted decisions, owner constraints, terminology, relevant findings, stale/gap markers, exclusions, privacy classes, and source-boundary rules.
6. **Create steering packet.** Include target, decision question, lane plan, source boundaries, gates, two-output contract, relevant-history/context isolation, and verification checklist.
7. **Create/update Linear.** Only if accepted/explicit: parent issue + child issues. If Linear is not approved, write local `linear/parent.md` and `linear/children/*.md` as ready-to-create bodies.
8. **Dispatch research lanes.** Use workers for lanes likely to exceed 5 tool calls or requiring independent research. Claim each lane in `campaign_state.json` first; duplicate launch joins/rejects rather than creating competing work. Claims are file-locked and may include a lease so interrupted running attempts can be recovered as stale on restart. Worker dispatch and continuation between lanes do not require additional permission. Worker packets must exclude Context Brief/private context. The controller reads the Context Brief only after all lanes finish (`campaign_controller.record_context_release`).
9. **Collect lane outputs by completion event.** Require each worker to return artifacts, cited sources, gaps, contradictions, ActionRegister candidates, visual candidates, and verification status. Ingest completions into the controller state; do not add a status-poll loop. Events must be idempotent and transition-checked. `output_path` must resolve to a real non-symlink file under the campaign root before hashing. On restart, select only missing/stale/invalid/failed lanes and reuse completed lanes whose output hash still matches; terminal retry-ceiling lanes are reported as HOLD/block and are not selected again.
10. **Retry/fallback discipline.** For transient lane failures, allow one same-route retry and one fallback attempt. After the fallback fails, mark the lane terminal with a reason. If an upstream artifact changes, invalidate only downstream dependents. For material QA failures, cap repair cycles per failure class and block after the cap rather than looping indefinitely.
11. **Internal synthesis.** After every lane has finished, read the Context Brief and write `internal/reference-pack.md` from the lane evidence and the Context Brief. Write the action register sidecar `synthesis/action-register.md`. Keep process records out of the pack.
12. **Write/update visuals.** Write/update `synthesis/visual-exhibits.json` with output class, source data refs, captions, encodings/scales, anchors, render method, artifacts, validation receipts, reviewers, and whether internal context is used.
13. **Run independent QA.** Complete packets/receipts for context handoff, evidence/privacy, visual review, taxonomy/routing, Librarian repair/no-change, and final assurance. Reviewer identities must differ from producers where independence is required.
14. **Write and decide the plain brief.** After QA accepts the final reference pack, run `scripts/plain_brief_rewrite.py --campaign-root <root>`. It writes `internal/plain-brief.md`, `qa/plain-brief-review.json` and supported editor-note decisions. Missing or invalid judgements leave `qa/editor-note-decisions.json` pending. Every current and retained note must be decided against the actual brief and admitted evidence by the existing independent editor, not relabelled locally. Then use `scripts/run_publication_reviews.py` so publication QA starts only after the decision file passes. Finally, `campaign_controller.record_plain_brief(root)` marks the `plain_brief` phase. If the reference pack changes later, rerun it. See `plain-brief-output.md`.
15. **Copy exports into the configured export root.** Run `scripts/export_deliverables.py --campaign-root <root>` unless deliverables export is explicitly suppressed. The exporter hard-fails unless the plan contains exactly the two allowlisted files (reference pack and plain brief) and the plain-brief gate is clean. Controller closeout must distinguish and structurally validate local copy, local commit, remote push, and remote readback: truthy placeholders are not proof; hashes/refs must read back against real files and matching commit IDs when deliverables closeout is requested.
16. **Verify.** Run `scripts/verify_campaign_depth.py`, citation, manifest/hash, required-section, privacy, visual, no-exec, cleanup, history/context, frontmatter/tag/link, internal-only deliverables export/readback, controller replay metrics, active/idle timing, repair-cycle caps, and Linear/export checks.
17. **Close out.** Run `scripts/complete_campaign.py` first. Point the owner at the plain brief first. Report artifact paths, verification facts, gates, and follow-ups. Do not stop at intermediate phases to ask whether to continue.

## Linear creation policy

- If the owner explicitly asks for parent/children, create them.
- If the work is accepted for execution, Linear is canonical.
- If the ask is exploratory and not accepted, scaffold local issue bodies but do not mutate Linear unless the owner asks.
- Do not create a new tracker project unless the owner explicitly asks for one.

## Manual fallback

If the script is unavailable:

```text
<campaign-root>/
  steering.md
  manifest.json
  citations.json
  history/
    relevant-history.md
    relevant-history.json
  lanes/
  workers/
  linear/
    parent.md
    children/
  internal/
    reference-pack.md
    plain-brief.md
  context/
    context-brief.md
    context-brief.json
  synthesis/
    action-register.md
    visual-exhibits.json
  qa/
    packets/
    context-handoff-review.json
    evidence-privacy-review.json
    visual-review.json
    taxonomy-review.json
    librarian-repair-receipt.json
    final-assurance.json
    plain-brief-review.json
    plain-brief-rounds/
  receipts/
    verification.txt
```

Then fill templates by hand from this skill and `references/dual-output-and-independent-qa.md`.

For a legacy four-output campaign (no `output_layout: brief-and-reference-v2`), steps 11 and 12 instead write and freeze `external/original-full-research-pack.md`, then write the internal full pack and executive brief from it. That layout is verified as before and is not migrated.
