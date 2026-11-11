# Phase packet templates (DR-897)

`scripts/run_campaign.py` renders these with Python `string.Template` (`${NAME}` fields; a literal dollar is `$$`).
Each rendered packet carries every path and rule its phase needs, so a phase session never reads the
skill's references or scripts. The first line names the phase for logs and test doubles.

- `lane.md`: one public research lane (toolsets web, terminal, file; no skills tool).
- `synthesis.md`: the reference pack and its exhibit ledger from the lane evidence.
- `review-common.md`: shared header for the cross-vendor reference-pack review.
- `review-evidence_privacy.md`: cross-vendor reference-pack review (claims, exhibits and privacy).
- `repair.md`: one bounded repair round on the reference pack from review findings.
