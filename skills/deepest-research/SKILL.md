---
name: deepest-research
description: Use when the owner asks for deepest research on one question.
version: 1.0.0
author: Research contributors
license: MIT
metadata:
  hermes:
    tags: [research, citations, deepest, follow-ups]
    related_skills: [deep-research-fast, deep-research]
---

# Deepest research

Deepest research answers one question in more depth than fast mode. It runs fast research once. Then it follows the gaps with up to four more fast runs at once. One writer turns everything into a single cited answer. A model from another vendor reviews it. If the review finds missing parts, one extra round researches them and the answer is rewritten and reviewed again.

This skill is a pointer. The code, config, rules and pitfalls live in the `deep-research-fast` skill. Load that skill and follow its "Deepest research" section. Do not copy its rules here.

## When to use

- The owner asks for "deepest research".
- One clear question needs exact settings, commands, versions, prices or dates that a fast run would miss.

Use full `deep-research` instead for a research pack, board brief, issue tickets or a buy, build or kill decision. Use `deep-research-fast` for a quick cited answer.

## Run it

Run from this repository's root, or use the absolute path to the installed skill. Keep `deep-research-fast` installed beside this skill.

```bash
cd skills/deep-research-fast
python3 scripts/deepest_research.py run --question "How does X work?" --root ~/.hermes/work/<run-folder>
```

Run it in the background and wait on it. Run the same command again to resume or to retry failed follow-ups. The answer lands in `<root>/runs/<run-id>/answer.md`. Run ids start with `deepest-`.

## Before you report

- Load `deep-research-fast` and work through its verification checklist.
- Read `review.json` before relying on the answer. A rejecting review does not stop the run.
- Lead with the readiness line and say what is still missing. The line comes from the review's missing parts, not its verdict.
- Metered search and fetch charges are not in the run's cost record.
