# Contributing

Keep changes small and describe the behaviour they change. Include a regression test for fixes.

## Local checks

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest skills/deep-research/scripts skills/deep-research-fast/scripts
git diff --check
```

Run the full suite before proposing a change. Include the test counts and any limits in your report. Tests must work without private campaigns, credentials or model calls.

## Verification rules

Do not loosen quotation matching, source hashes, receipt validation, reviewer independence or code attestation to make tests pass. Fix the inputs or the underlying defect instead.

Label synthetic fixtures clearly. They must never be usable as real publication evidence. Keep both healthy and negative controls for verification changes.

## Privacy and scope

Use generic examples and configurable paths. Do not include private notes, task IDs, account names, network details or credentials. Check images and file metadata as well as text.

Do not run live paid research, export deliverables, publish content or change runtime settings as part of a test. Get explicit authorisation for external changes.

Describe optional integrations and graceful failure. Keep core offline checks independent of other local repositories.
