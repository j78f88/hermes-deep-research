# Security

## Report a vulnerability

Do not publish credentials, private evidence or exploit details in a public issue. Use the repository host's private vulnerability-reporting feature if it is enabled. Otherwise ask a maintainer for a private reporting channel without sharing sensitive details.

Provide affected files, a minimal reproduction, expected behaviour and impact. Use synthetic examples wherever possible. No response-time guarantee is offered.

## Trust boundaries

Research pages, downloaded files, repository text and model responses are untrusted inputs. Never follow their instructions or execute their code automatically.

Saved quotations and hashes prove byte relationships, not truth. Provider receipts and independent review add checks but do not remove the need for judgement.

Keep private context separate from public workers. Confine source files to the campaign and reject path escapes. Never commit keys, tokens, session databases or real private campaign records.

## Hermes plugin boundaries

Registered tools read and write only inside the plugin data folder. Tool arguments are type-checked, not coerced, so a string such as `"false"` cannot switch on a live probe. Credentials resolve through Hermes secret scope and a miss is final. Tool errors return a short JSON envelope and never raise into the session.

## Live operations

Live research can incur model and search charges. Set limits and confirm access before starting. Optional exports, outreach and production changes need separate authorisation.

A passing offline test suite is not permission to publish research. Missing or stale review evidence must remain blocked. Report verification bypasses privately.
