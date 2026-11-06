# Relevant History Scan

Use this in every deep-research campaign unless the owner explicitly asks for an external-only run.

## Purpose

Before deciding lane scope or writing conclusions, check whether the owner already has relevant history in:

- approved knowledge-base records and project packets;
- Linear issues, comments, parents/children, and prior receipts;
- prior Hermes sessions;
- prior source-scout, repo-scout or transcript-acquisition artifacts;
- owning project repos or ledgers when named by the target.

The goal is not to bias the external research. The goal is to avoid duplicate work, preserve prior decisions, reuse known caveats, and connect the new evidence to existing accepted work.

## Default behavior

Default to **read-only relevant-history scan**.

Do not ask the owner first unless one of these is true:

- the owner requested an external-only / clean-room view;
- using private/customer/confidential history would change the audience or sharing boundary;
- the research is intended for public/client delivery and internal context must be excluded;
- accessing the likely source requires credentials/secrets or customer data;
- the history scan would mutate Linear/knowledge-base/repositories rather than read them.

Otherwise, scan quietly and label the result as **internal context**, not external evidence.

## Surfaces and methods

### Hermes sessions

Use `session_search` for prior related sessions. Search:

- target name;
- URL/domain/video ID/repo slug;
- synonyms and product/category names;
- prior pack names or artifact slugs.

Session history is context about what was previously said/done. It is not evidence about the current external source.

### Knowledge base

The prior-work surface is named `knowledge_base`. Check only the knowledge-base paths or read-only adapters explicitly admitted for the campaign. No private-note directory layout is assumed. Prior exported deliverables may be read from the resolved `DELIVERABLES_EXPORT_PATH` (default `~/research-deliverables`) when included in that boundary. Expand configured paths before searching; an environment variable name is not a directory.

Search:

- exact target name;
- domain/repo/video/channel IDs;
- category terms;
- existing `ActionRegister`, `SourceCard`, `BuildBrief`, board brief, or source-scout notes.

Respect the admitted read boundary and exclusions. Do not edit knowledge-base records, delete files or copy raw corpus into the export directory. History retrieval does not authorise publication or mutation.

### Linear

Use the issue-tracker integration for readback. Search the live tracker for:

- target name;
- domain/repo/video/channel IDs;
- existing parent campaigns;
- source-scout, deep-research, proof, validation, watch, or candidate issues;
- blocked issues and owner gates already recorded.

If a matching accepted parent exists, reuse/update it rather than creating a duplicate. Do not mutate In Progress issues under Issue steward rules.

### Source/corpus artifacts

Search durable/local source-intelligence roots when relevant:

- `${RESEARCH_ARCHIVE_ROOT}/runs/`, with `RESEARCH_ARCHIVE_ROOT` defaulting to `~/.hermes/research-archive`; include an existing legacy `research/` subtree only when relevant;
- `${SOURCE_INTELLIGENCE_ROOT}`, default `~/source-intelligence`;
- an explicitly configured YouTube ingestion corpus or other artifact store, when installed;
- owning project corpus or repo ledgers when known.

Treat local staging paths as machine evidence, not reader-facing links, unless a browsable/durable mirror exists.

## History scan output

Write a campaign-local artifact:

```text
<campaign-root>/history/relevant-history.md
<campaign-root>/history/relevant-history.json
```

Minimum Markdown sections:

```markdown
# Relevant history scan

## Query terms
## Surfaces checked
## Matches found
## Prior decisions / gates
## Existing Linear owners
## Prior artifacts / packs
## Reusable context for this campaign
## Exclusions / not used
## Recommendation
```

Minimum JSON fields:

```json
{
  "schema_version": "deep-research-relevant-history-v1",
  "status": "not_started|none_found|found|blocked|skipped_external_only",
  "query_terms": [],
  "surfaces_checked": [],
  "matches": [],
  "existing_linear_owners": [],
  "prior_artifacts": [],
  "prior_decisions_or_gates": [],
  "context_to_use": [],
  "context_to_exclude": [],
  "recommendation": ""
}
```

## How to use history in synthesis

In campaign-local history/context sidecars, record:

- `Internal history used:` what prior knowledge-base/Linear/session/source-scout context shaped the analysis.
- `Internal history excluded:` what was found but intentionally not used.
- `Prior owner/gate:` if an existing tracker issue or owner gate controls next action.

Do not copy these process labels into the reference pack or plain brief. In the readers, explain relevant constraints and judgements in plain words without exposing private identifiers. Do not cite internal history as external proof. Use it as operating context, source-of-truth routing, prior decision memory, or comparison baseline.

## PASS / HOLD / FAIL

PASS:

- Sessions, knowledge base, Linear, and likely corpus roots checked or explicitly N/A.
- Matching owners/artifacts linked in campaign-local history artifact.
- Existing accepted parent reused or duplicate creation avoided.
- Prior decisions/gates reflected in the parent/steering packet.

HOLD:

- One important surface could not be checked, but the blocker is explicit.
- History scan found likely matches needing controller review.

FAIL:

- New parent/children created despite obvious existing owner.
- Prior owner decision/gate ignored.
- Internal context presented as current external evidence.
- Private/customer/confidential history used in an external/public/client deliverable without boundary decision.
