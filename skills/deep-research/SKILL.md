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
2. **Public worker isolation:** Public research workers run with `-t web,terminal,file` only and no access to private context, a knowledge base or an issue tracker. The one local file they may read is `context/owned-assets.md`: the file you pass with `--owned-assets`, or a note that none was supplied. Set `DEEP_RESEARCH_SCAN_OWNED_ASSETS=1` to fill it instead with a name-level list of installed Hermes skills and user systemd units whose names match the topic.
3. **Cross-vendor content review:** One model from a different vendor than the writer checks that claims, numbers, tables and the plain brief match the saved quotes.
4. **Provider call verification:** `scripts/complete_campaign.py` verifies at OpenRouter that the writer and cross-vendor reviewer calls occurred.
5. **Plain prose first:** The owner gets a short plain brief first (no tables in the brief), backed by the reference pack.

## How to Run a Campaign (Phased Driver Only)

Never hand-roll worker units or the retired six-reviewer loop from chat. Always run campaigns through `scripts/run_campaign.py`, which enforces hard per-phase step caps (`--max-turns`) and the four required checks. Run the commands below from the installed `deep-research` skill directory, or use absolute script paths.

### Step 1: Start the Campaign

The `setup` phase scaffolds the campaign root with `scripts/init_campaign.py`.

```bash
python3 scripts/run_campaign.py start \
  --root "$HOME/.hermes/work/dr-<slug>" \
  --question "<exact research question>" \
  --target "<target>" --detach
```

To resume a paused campaign after a phase checkpoint, or to see where it is:

```bash
python3 scripts/run_campaign.py resume --root "$HOME/.hermes/work/dr-<slug>" --refresh-config --detach
python3 scripts/run_campaign.py status --root "$HOME/.hermes/work/dr-<slug>"
```

### Step 2: The Four Required Checks

Every campaign runs exactly four checks inside the driver:

1. **Saved quotes check (`quote_check.py`):**
   ```bash
   python3 scripts/quote_check.py --campaign-root "$HOME/.hermes/work/dr-<slug>"
   ```
2. **Mechanical isolation and reader lint (`kit/mechanical_qa.py`):**
   Checks public worker flags (`-t web,terminal,file`), no private reads, valid frontmatter, tags and links, and no tables or local `.md` links in the plain brief.
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
