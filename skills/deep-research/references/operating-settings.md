# Research settings

Core research uses two configuration files and one small resolver.
It does not need a new service or global Hermes provider changes.

## What lives where

| File | Purpose |
|---|---|
| `config/model-routing.yaml` | Role models, capability evidence and generation parameters |
| `config/execution-policy.yaml` | Concurrency, retries, backoff, timeouts and worker duration |
| `config/research.env.example` | Supported run-scoped environment names, without secrets |
| `scripts/research_settings.py` | Strict validation and effective settings |
| `scripts/model_client.py` | Shared calls, bounded recovery and actual route records |

The configuration uses YAML-compatible JSON.
The model catalogue has its retrieval date and hashes.
Its prices describe that retrieval, not a current quote or spending approval.
Unknown latency and incomplete costs remain unknown.

## Precedence

An explicit run override wins over the role environment value.
The environment value wins over the selected profile.
The default profile is `quality`.

Call `load_settings(overrides=...)` before dispatch to freeze an explicit choice.
Unknown keys and invalid combinations fail before calls.
An endpoint override cannot introduce a new provider.
The existing OpenRouter secret reference is resolved only when needed.
Never put credentials in settings, snapshots or reports.

## Roles and independence

Evidence extraction has a separate role from substantive synthesis and review.
Synthesis and acceptance use capable models with supported high reasoning.
The dated catalogue records the capabilities used for selection.
Price and speed matter only among suitable routes.

Record every actual writer of the current output.
An editor that rewrites reader bytes is also a writer.
A comments-only reviewer does not become a writer.

The acceptance reviewer must differ in developer from every actual writer.
A different host or model name is not sufficient.
Unknown served identity cannot establish independence.
Fallbacks must preserve capability, context, privacy and independence.

## Freeze and record

Use `settings.freeze()` to save the effective non-secret settings before dispatch.
A resume must match that snapshot.
A changed snapshot cannot overwrite the original.

Keep actual calls separately from the frozen settings.
Retain failed attempts, served identities, fallbacks, usage and raw responses.
Save raw responses before interpreting application JSON or review verdicts.
Do not make another paid call merely because a valid response needs local parsing repair.

Authentication and permission failures stop immediately.
They do not authorise inspecting or replacing credentials.
Worker deadlines bound cooperative calls and retries, not a new background watchdog.

## Acceptance is not configurable

Settings cannot disable acceptance or relax the reader format checks.
They cannot approve spending or change historical facts, units or formulae.
No absent limit means unlimited spending.
Preserve every limit you explicitly approved.

The shared readiness check binds both readers and their supporting evidence.
It retains unresolved findings across revisions.
It also binds semantic manifest fields, including purpose and destinations.
Only the five documented exporter bookkeeping fields are exempt.

## Existing workflow

Use `init_campaign.py` with the question's explicit purpose.
Use the normal research lanes and controller synthesis.
Review the reference pack before deriving the brief.
Then use the installed brief generator, completion check and deliverables exporter.

See [Reader pair structure and style](reader-pair-style.md) for the two outputs.
See [Plain brief](plain-brief-output.md) for generation and delivery commands.
The business overlay remains separate and unsupported by core.
