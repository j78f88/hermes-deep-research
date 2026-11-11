---
name: deep-research
description: Use when the owner asks for deep research.
version: 3.0.0
author: Research contributors
license: MIT
metadata:
  hermes:
    tags: [research, citations, synthesis, verification, campaign, plain-brief]
    related_skills: [deep-research-fast, deepest-research, youtube-content]
---

# Deep Research

Use this skill when the owner explicitly asks for **deep research** on a complex, multi-part question. For a single question, use `deep-research-fast` (about 5 minutes) or `deepest-research` (about 17 minutes) instead.

## Core Guarantees (Never Weaken)

1. **Saved quotes:** Every cited claim maps to a saved source file and a verbatim quote verified by `scripts/quote_check.py`.
2. **Public worker isolation:** Public research workers run with `-t web,terminal,file` only. The terminal toolset is a shell as your user, not a sandbox. Isolation is the packet rules, which tell workers not to read private context, a knowledge base or an issue tracker, plus an after-the-fact read audit (`receipts/worker-read-audit.json`). The context-handoff check holds the run when that audit records a forbidden read, holds an error, is missing, or lacks a row for a lane. The one local file they may read is `context/owned-assets.md`: the file you pass with `--owned-assets`, or a note that none was supplied. Set `DEEP_RESEARCH_SCAN_OWNED_ASSETS=1` to fill it instead with a name-level list of installed Hermes skills and user systemd units whose names match the topic.
3. **Cross-vendor content review:** One model from a different vendor than the writer checks that claims, numbers, tables and the plain brief match the saved quotes.
4. **Provider call verification:** `scripts/complete_campaign.py` verifies at OpenRouter that the writer and cross-vendor reviewer calls occurred.
5. **Plain prose first:** The owner gets a short plain brief first (no tables in the brief), backed by the reference pack.

## How to Run a Campaign (Phased Driver Only)

Never hand-roll worker units or the retired six-reviewer loop from chat. Always run campaigns through `scripts/run_campaign.py`, which enforces hard per-phase step caps (`--max-turns`) and the four required checks. Run the commands below from the installed `deep-research` skill directory, or use absolute script paths.

### Step 1: Start, Wait, Report and Resume

`start` runs the `setup` phase, which scaffolds the campaign root with `scripts/init_campaign.py`, then runs every phase.

```bash
python3 scripts/run_campaign.py start --detach \
  --root "$HOME/.hermes/work/dr-<slug>" \
  --question "<exact research question>" \
  --target "<target>" --target-type <type> --purpose factual \
  --lanes-file <lanes.json> --export live
```

`--lanes-file` is a JSON list of `{"id": "<scaffold lane id>", "focus": "<what this lane must find>"}`. Without it, `--lanes` takes a comma list of lane ids, and with neither the driver runs the four default public lanes. Use `--export dry-run` or `--export skip` when the result must not reach the export root. Both need `--export-reason "<why>"`. `--purpose decision_assessment` also needs `--preseed-dir`.

`--detach` prints the wait and status commands. Wait with one blocking call on the printed `wait_for_jobs.py --marker <root>/orchestration/driver.done` command. Never loop sleep-and-check. Then report the verdict exactly as printed:

```bash
python3 scripts/run_campaign.py status --root "$HOME/.hermes/work/dr-<slug>"
```

If the run stops, fix the cause and resume:

```bash
python3 scripts/run_campaign.py resume --root "$HOME/.hermes/work/dr-<slug>" --refresh-config --detach
```

### Step 2: The Four Required Checks

Every campaign runs exactly four checks inside the driver:

1. **Saved quotes check (`quote_check.py`):**
   ```bash
   python3 scripts/quote_check.py --campaign-root "$HOME/.hermes/work/dr-<slug>"
   ```
2. **Mechanical isolation and reader lint (`kit/mechanical_qa.py`):**
   Checks the read audit for private reads, valid frontmatter, tags and links, and no tables or local `.md` links in the plain brief. It does not check worker flags: `research_settings.load_phases` limits the lane toolsets to web, terminal and file when `config/phases.yaml` loads.
3. **Single cross-vendor content review (`evidence_privacy`) and plain brief editor (`plain_brief_rewrite.py`):**
   One cross-vendor review pass checks claims, numbers, exhibits and privacy against saved quotes. Blocking notes from that review go to the bounded repair rounds.
4. **Provider call completion check (`complete_campaign.py`):**
   ```bash
   python3 scripts/complete_campaign.py --campaign-root "$HOME/.hermes/work/dr-<slug>"
   ```
   The campaign is done only when it prints `PASS`. Report HOLD or FAIL exactly as printed.

### Step 3: Export the Deliverables

The driver's `export` phase runs `scripts/export_deliverables.py`, which copies exactly the plain brief and the reference pack into the export root. When the export root is a git work tree with an `origin` remote, the driver also commits and pushes exactly those two files and reads the remote back. To export by hand:

```bash
python3 scripts/export_deliverables.py --campaign-root "$HOME/.hermes/work/dr-<slug>"
```

Check the readback hashes of both files before reporting completion. Give the owner the plain-brief path first and the reference-pack path second.

## Local paths and configuration

| Variable | Default | Purpose |
|---|---|---|
| `DELIVERABLES_EXPORT_PATH` | `~/research-deliverables` | Flat directory for approved Markdown deliverables |
| `RESEARCH_ARCHIVE_ROOT` | `~/.hermes/research-archive` | Durable campaign archive (`runs/YYYY-MM/<run-id>/`) |
| `SOURCE_INTELLIGENCE_ROOT` | (optional, unset) | Optional local source corpus directory |

The driver finds `hermes` on `PATH`. `--detach` and the default launcher use `systemd-run --user`; pass `--launcher direct` where that is not available. Phase budgets, models and toolsets live in `config/phases.yaml` and `config/model-routing.yaml`. Settings cannot switch off checks, lower thresholds or authorise spending.

## Legacy layout

Campaigns whose manifest has no `output_layout: brief-and-reference-v2` keep the four-output layout. The verifier, exporter and completion check still verify them as before. They are not migrated. The legacy QA repair and re-freeze protocol was removed with the retired six-reviewer files.
