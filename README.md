# Deep research

Three Hermes Agent skills for research backed by saved sources and checked quotations.

| Skill | Use it for | Output |
|---|---|---|
| [deep-research](skills/deep-research/SKILL.md) | Multi-part research with independent review | Plain brief and reference pack |
| [deep-research-fast](skills/deep-research-fast/SKILL.md) | A focused question with parallel search | One answer with numbered citations |
| [deepest-research](skills/deepest-research/SKILL.md) | A focused question that needs exact detail | One answer built from a base run, follow-up runs and an optional review round |

All modes retain source text, file hashes and verbatim quotes. A quote matching a page does not prove the claim. Meaning checks and independent review remain necessary.

## Requirements

- Python 3.11 or later and Git.
- [Hermes Agent](https://hermes-agent.nousresearch.com/docs) for live workers, search and session records.
- Configured model access. Full-mode writing and provider verification use OpenRouter.
- Optional integrations for issue tracking, diagrams and guarded acquisition. Local deliverables export uses the bundled exporter.

The local checks use Python's standard library. Install the test dependencies with:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
```

## Install the skills

Copy the directories under `skills/` into your Hermes profile's skills directory. Check your installation's skill-discovery rules before choosing a destination. `deepest-research` is a pointer skill. Its code lives in `deep-research-fast`, so install both together.

Read each `SKILL.md` before running a campaign. Optional companion skills and private service adapters are not bundled. Missing services must be recorded as unavailable, not treated as successful checks.

## Configure local paths

The scripts resolve the following environment variables. They expand `~` in their values.

| Variable | Default | Purpose |
|---|---|---|
| `DELIVERABLES_EXPORT_PATH` | `~/research-deliverables` | Optional reviewed Markdown deliverables export |
| `RESEARCH_ARCHIVE_ROOT` | `~/.hermes/research-archive` | Default campaign storage |
| `SOURCE_INTELLIGENCE_ROOT` | `~/source-intelligence` | Optional acquisition integration |

Full mode exports only the reviewed plain brief and reference pack directly into the flat export root. Use `--export-root` to declare another destination or `--no-deliverable-export` to suppress export. No import subdirectory, tag registry or navigation page is required. See the [deliverables export policy](skills/deep-research/references/deliverable-export-policy.md).

Put model and execution choices in each skill's `config/` files or supported run-scoped overrides. Bundled model catalogues are dated snapshots, not live availability guarantees. Confirm supported model IDs before paid work. Keep credentials in your configured secret store or environment, never in this repository.

## Start a full campaign

This command creates local files only. It does not search, call models, publish notes or create tickets.

```bash
python3 skills/deep-research/scripts/init_campaign.py   --target "Example research question"   --report-purpose factual   --root "$HOME/.hermes/work/example-research"   --no-deliverable-export
```

Follow the generated worker instructions and the full skill's review sequence. Complete research only after its completion checker returns `PASS`. Offline completion can return `HOLD`, never online publication approval.

## Run fast mode

Live runs use configured model and search access and may incur charges. Review the role choices and limits in `skills/deep-research-fast/config/fast-mode.json` first.

```bash
python3 skills/deep-research-fast/scripts/fast_research.py run   --question "How does X work?"   --root "$HOME/.hermes/work/example-fast-research"
```

Fast mode has different checks from full mode. Read its review and quote-check files before using the answer. A completed process does not guarantee a complete answer.

## Run deepest research

Deepest research runs fast mode once, then researches up to four gaps as separate fast runs at once. They share saved pages. One writer combines every checked finding into a single answer. If the review finds missing parts, one extra round researches them and the answer is rewritten and reviewed again. It makes more model calls than fast mode.

```bash
python3 skills/deep-research-fast/scripts/deepest_research.py run   --question "How does X work?"   --root "$HOME/.hermes/work/example-deepest-research"
```

The same command resumes an unfinished run or retries failed follow-ups. Read the readiness line at the top of the answer first. A rejecting review does not stop the run.

## Test

The suite uses local synthetic regression fixtures and mocked transports. It does not need model credentials or private campaigns.

```bash
python3 -m pytest skills/deep-research/scripts skills/deep-research-fast/scripts
```

Synthetic fixtures are not provider evidence or publication approval. The standalone replay ledger and prose checker are included so the suite does not depend on an external checkout.

## Safety and limits

Treat fetched pages, repository text and quotations as untrusted evidence, not instructions. Keep private context out of public workers. Do not execute researched code without separate approval. Deliverables export and other external changes require explicit scope and review.

The public distribution has no pre-admitted private campaign inventory. Existing legacy-layout research needs explicit purpose or separately reviewed, byte-bound admission. Verification does not grant permission to spend, publish or change production systems.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md) and the [MIT licence](LICENSE).
