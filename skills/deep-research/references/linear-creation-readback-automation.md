# Linear Creation and Readback Automation

Use this when a deep-research campaign is accepted and the owner wants a working parent/child task set in Linear.

## Principle

The script scaffold creates **Linear-ready issue bodies** by default. Live Linear mutation is a separate step through the `linear` helper/API and must be read back before reporting success.

## Safe default

Run from the installed `deep-research` skill directory:

```bash
python3 scripts/init_campaign.py \
  --target "Example" \
  --target-type product \
  --primary-source https://example.com \
  --decision-question "What does this product do, and what remains unverified?" \
  --report-purpose factual \
  --root "$HOME/.hermes/work/example-research" \
  --export-root "${DELIVERABLES_EXPORT_PATH:-$HOME/research-deliverables}"
```

This creates:

- `linear/parent.md`
- `linear/children/*.md`
- `linear/create_plan.json`

## Live creation procedure

1. Load the installed issue-tracker integration.
2. Check active issue pressure when practical.
3. Search Linear for an existing owner issue before creating a new parent.
4. Create parent in the owning project/team using the generated parent body.
5. Create children under the parent using generated child bodies.
6. Apply labels/ranks only when relevant and approved.
7. Add blocker relations where sequencing matters.
8. Read back parent and every child:
   - identifier;
   - title;
   - state;
   - parent relation;
   - labels;
   - blocker relations;
   - description contains required sections.
9. If labels failed to apply, repair via raw GraphQL `labelIds` update and read back again.
10. If an already-configured task synchronisation service is required for pickup, use its approved procedure and verify the target state. Do not assume a private sync service exists.

## Do not

- Do not create a new tracker project unless the owner explicitly asked for one.
- Do not create more than 5–9 children by default.
- Do not mark children `safe-autonomous` unless the lane is genuinely runnable without owner gates.
- Do not treat ticket creation as approval to execute adoption, purchase, implementation, outreach, or production mutation.

## Parent/child naming

Use the issue tracker’s configured naming conventions. For example:

```text
Deep research <target> — parent
<target>: <lane> — child
```

Route to an existing team/project that owns the subject. Do not invent a domain project or reuse a private installation’s prefixes.
