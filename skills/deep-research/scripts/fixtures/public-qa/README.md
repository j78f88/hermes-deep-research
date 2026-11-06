# Synthetic public QA regression data

These files are invented regression inputs. They are not real campaign history,
provider responses, independent research, or publication evidence. They confer
no publication approval. URLs use the reserved `example.invalid` domain.

`synthetic-campaign/` supplies the layout consumed by `replay_saved`:

- Seven defective drafts retain distinct factual problems. Draft five has a blocking editor review.
- The preserved first-pass reader includes a planned-event qualifier and an unsupported proposed correction.
- The refreshed reader has five style notes and does not contain that proposed correction.
- Original legacy classifications fail closed. The adjacent repaired decisions file fixes only the classification names.
- The repaired decisions bind exact reader and source bytes, but intentionally lack independent served classification judgements. They must still fail acceptance.
- Source indexes bind invented source bytes and reserved URLs. The append-only proof hashes a genuine original byte prefix and the complete saved index.

`MC-2` is retained only because the existing replay parser requires that marker.
It does not identify a real organisation or facility here. Generation numbers
and `historical_selection` outputs describe test layout, not actual history.
The expected replay status is `FAIL`: format-only decisions cannot satisfy the
current independent-judgement requirement. The regression suite itself passes.

## Rebuild

From the scripts directory:

```sh
python fixtures/public-qa/generate.py
```

The generator uses no network, settings, credentials or source campaigns. It
writes only this directory's synthetic campaign and the adjacent
`fixtures/sovereign-decisions-repaired.json` compatibility filename. That filename
is retained for compatibility only. Its contents are entirely synthetic.

Tests read checked-in data and do not need to regenerate it. A separate test
runs a copy of the generator in its temporary directory and compares every
resulting byte with the checked-in data.

The current-acceptance positive and tamper controls reuse the suite's existing
synthetic transport helper. They run the real saved-response, reader, evidence,
and classification binding checks. They do not authenticate any provider or
replace quote, privacy, completion or publication verification.
