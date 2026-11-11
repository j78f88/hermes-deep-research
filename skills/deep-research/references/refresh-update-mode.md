# Refresh / Update Mode

Use when the owner asks to update an existing deep-research pack or when a prior pack is older than the decision requires.

## Goal

Do not rerun the whole campaign by default. Produce a **delta brief** unless the original scope is obsolete.

## Inputs

- Prior reference pack and plain brief (or legacy full pack and executive brief)
- Prior manifest/citation ledger
- Prior ActionRegister
- New decision question or update reason

## Procedure

1. **Read prior verdict.** Extract final recommendation, ActionRegister, evidence gaps, volatile facts, and what-would-change table.
2. **Define refresh scope.** Recheck only volatile/high-impact facts unless the owner asks for full rebuild.
3. **Run delta lanes.** Typical lanes:
   - current product/state delta;
   - market/customer/repo delta if relevant;
   - ActionRegister status delta;
   - adversarial delta;
   - updated recommendation.
4. **Compare claims.** Mark unchanged / changed / contradicted / newly discovered / no longer accessible.
5. **Write delta brief.** Link old pack and state whether original recommendation holds.
6. **Update ActionRegister.** Preserve old IDs; add new IDs only for new actions.
7. **Verify.** New citations, hashes, and readbacks; do not rewrite old evidence silently.

## Delta brief outline

```markdown
# <Target> — deep research update / delta brief

## Prior baseline
## Why refresh now
## What changed
## What did not change
## New evidence gaps
## Action changes (link to the sidecar)
## Updated answer or requested technical judgement
## What would change it next
## Sources
```

## Full rebuild triggers

Run a new full campaign only if:

- target pivoted materially;
- acquisition/shutdown/rebrand changed the object;
- source availability changed enough to invalidate prior pack;
- the owner asks a substantially different decision question;
- prior pack failed citation/verification standards;
- old ActionRegister is missing and cannot be safely backfilled.

## Approved in-place deliverables refresh

Use these steps when the owner has approved replacing previously exported notes. A refresh does not waive evidence or review gates.

- Before pre-export QA, reset stale final-assurance hashes with `research_qa_repair.prepare_preexport_assurance_stub()` and check for a single `## Sources` heading with `validate_reference_sources_section()`. Both are functions in `scripts/research_qa_repair.py`.
- Repair reader drafts with `plain_brief_rewrite.py --campaign-root <absolute-root> --corrected-draft <file> --corrected-draft-authors <authors.json>`. The current path preserves the corrected draft and makes no writer call; the existing independent editor reviews it. The authors packet binds `draft_sha256` and every contributing `writer_calls` record, including served fallbacks.
- Recheck the current reference pack, editor-note dispositions, publication QA and completion before exporting. Do not hand-edit accepted reader bytes or copy old approvals onto a changed draft.
- Run `export_deliverables.py --campaign-root <absolute-root> --overwrite-approved`. It archives prior exported deliverables into `orchestration/prior-deliverable-exports/` and records `manifest.deliverable_exports.prior_exports` before replacement.

Resolve `DELIVERABLES_EXPORT_PATH` from the environment (default `~/research-deliverables`) and preserve the original destination bindings. Keep archived campaigns under the resolved `RESEARCH_ARCHIVE_ROOT` (default `~/.hermes/research-archive`), normally `runs/YYYY-MM/<run-id>/`.

## Model roles

Resolve model choices from the shared non-secret settings before dispatch. Do not copy a historical model pair or a private tool profile into a new run.

| Role | Requirement |
|---|---|
| Corpus and materials audit | Read only the admitted campaign evidence |
| Targeted web delta extraction | Capture fresh evidence and preserve the prior baseline |
| Evidence, packaging and final assurance QA | Use independent reviewers and current hash bindings |
| Plain-brief writer | Record every actual writer and served fallback |
| Plain-brief acceptance editor | Developer differs from every actual writer; review both readers and admitted evidence |

Toolsets should expose only what the role needs. Capability, privacy, context limits and reviewer independence must survive any fallback. Freeze effective settings and record actual served identities separately.
