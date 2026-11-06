---
name: deep-research-fast
description: Use when a research question needs a cited answer fast.
version: 0.3.1
author: Research contributors
license: MIT
metadata:
  hermes:
    tags: [research, citations, parallel, sqlite, fast]
    related_skills: [deep-research, deepest-research]
---

# Deep research fast mode

## Overview

Fast mode targets a quick, cited answer to one clear research question. It is a smaller workflow than full deep research, not a promise of equivalent depth. Runtime depends on source access, queueing and model responses. Finishing early does not prove coverage.

It splits the searching into many small jobs that run at once on a cheap model. A small SQLite file holds the job list, every saved page with its hash, and each finding with its quote. Each finding is checked as soon as it lands. Its quote must be on its saved page. Then a cheap model from another vendor checks that the quote supports the claim. A strong model writes the answer from findings that pass both checks. A model from a different vendor reviews it. Fast mode has one review. Deepest mode can research the review's missing parts and rewrite the answer for a second review.

The output is one plain answer with numbered citations and a sources list. Every citation points at a saved page and a quote found in that page.

## When to use

Use fast mode when the owner wants a quick, cited answer to a clear question. It suits "how does X work", "what would it take to run Y" and short comparisons.

Use full `deep-research` instead when any of these apply.

- The owner asks for deep research, a research pack or a board brief.
- The work needs issue tickets, diagrams, reviewed deliverables export or the plain brief and reference pack.
- The question is a buy, build or kill decision that needs neutral comparison controls.
- Private context must be kept apart from public workers.

## Run it

Run from this repository's root, or use the absolute path to the installed skill.

```bash
cd skills/deep-research-fast
python3 scripts/fast_research.py run --question "How does X work?" --root ~/.hermes/work/<run-folder>
```

Run it in the background and wait on it. The answer lands in `<root>/runs/<run-id>/answer.md`. Running the same command again resumes an unfinished run for that question and mode. Fast ids start with `fast-` and deepest ids with `deepest-`. An explicit `--run-id` is unchanged.

Other commands:

```bash
python3 scripts/fast_research.py status --root DIR
python3 scripts/fast_research.py record --root DIR --run-id ID     # refresh costs later
python3 scripts/fast_quote_check.py --root DIR --run-id ID --write  # recheck every citation
```

The `record` and quote check commands work for both modes. They include deepest child runs.

## What it does

1. Preflight. Each model must reply "OK" to a one-shot call before any worker starts.
2. Plan. One strong-model call splits the question into four to six topics with search queries.
3. Research. Search jobs and then one fetch job per new page run in parallel, eight at most.
4. Check as you go. Each finding's quote is looked up in its saved page when the job lands. Then one meaning check per page asks whether each quote supports its claim. A claim the quote only partly supports is narrowed to what the quote says. Unsupported findings are dropped.
5. Answer. The writer sees only checked findings and cites them by id. Ids are then turned into numbers.
6. Check the answer. Check cited sentences, narrow or remove unsupported text, then save fact grades and add not-verified marks. Grades stay out of the answer by default. Recheck every citation.
7. Review. A model from a different vendor reviews the answer. The run stops if the vendors match. Deepest mode can add one review round, as described below.
8. Finish and record. Add the readiness line after the title. Put check counts near the end. Save wall time, job counts, models and cost.

Saved pages are the text the `web_extract` tool returned, read back from the session records. When Hermes cut the middle of a long page, the full copy from the web cache is saved instead. No model retypes a page.

## What it skips

Fast mode has no issue tickets, diagrams, deliverables export, plain brief, reference pack or context brief. It has no multi-round reader pairs and only one review. It does not run the `deep-research` completion check. Its own quote check is the citation check.

## Answer layout

Both `ANSWER` and `SYNTHESIS` aim for 1,500 to 2,500 words before the sources. A short question may need less.

After the title, the answer starts with a readiness line. The review's verdict does not set it. In trials the reviewer rejected every answer it saw, good ones included, so a verdict-based line misled. The verdict stays in `review.json` and `run.json`.

| Review | Readiness line |
|---|---|
| No usable review | Not reviewed. Check the sources before relying on it. |
| Usable review, no missing parts | Answers the question. Notes are at the end. |
| Usable review with missing parts | Answers part of the question. Still missing: |

A usable review comes from a different vendor and has a saved verdict. Every missing part from the review follows as a short bullet. The answer comes next.
The `## Checks` section comes at the end, before the sources. It holds check counts, citation recall before repairs and review issue counts.
It links `evidence.md` and `review.json` by name. Citation recall and the main sentence counts exclude inferences.
Citation precision is not measured. Both modes print the readiness line and the `review.json` path in their CLI summary.

List each source page once. Each entry gives its citation numbers, then the page title and address.
Keep quotes in `evidence.md` beside the answer. Each citation number has its own entry with the quote, page and saved page path.
The section named "Removed because the sources did not support them" also goes in `evidence.md`.
Keep `citations.json` unchanged. The final citation check must still pass.

## Files

| Path | Holds |
|---|---|
| `scripts/fast_research.py` | Controller with the run, status and record commands |
| `scripts/fast_store.py` | SQLite store in WAL mode for runs, phases, jobs, pages, claims, meaning checks and calls |
| `scripts/fast_runner.py` | Parallel job runner with a hard cap of eight at once |
| `scripts/fast_worker.py` | One-shot worker launcher and read-only session record readers |
| `scripts/fast_quote_check.py` | Citation check against saved pages |
| `scripts/fast_prompts.py` | Worker, checker, writer and reviewer prompts |
| `config/fast-mode.json` | Model ids from the live OpenRouter list, job and time limits |

Run folder layout:

| Path | Holds |
|---|---|
| `fast.db` | The shared database. Later runs on the same root reuse its pages. |
| `pages/` | One text file per saved page, named by its SHA-256 |
| `runs/<id>/answer.md` | The answer with numbered citations and sources |
| `runs/<id>/evidence.md` | Quotes and saved page paths per citation, plus removed claims |
| `runs/<id>/citations.json` | Each citation with its page, hash and quote |
| `runs/<id>/quote_check.json` | The final citation check |
| `runs/<id>/sentence_check.json` | Sentence checks, including inference checks |
| `runs/<id>/grades.json` | Saved fact grades |
| `runs/<id>/review.json` | The review with requested and served models |
| `runs/<id>/run.json` | Wall time, job counts, models, cost, `sentence_check_counts` and deepest `review_round` details |
| `runs/<id>/branches.json` | First-wave follow-up results, including failed branches to retry on resume |
| `runs/<id>/review_questions.json` | Review-round decision, questions and first-review counts |
| `runs/<id>/review_branches.json` | Review-round follow-up results, including failed branches to retry on resume |
| `runs/<id>/round1/` | First answer and check files, kept before the review round's second synthesis |
| `runs/<id>/jobs/` | Each worker's query, output and session id |

## Deepest research

Use this when the owner asks for deepest research on one question. The `deepest-research` skill is a short pointer to this section so the mode shows in the skill list.

```bash
python3 scripts/deepest_research.py run --question "How does X work?" --root DIR [--run-id ID]
```

It runs fast research once, then follows gaps with separate fast runs. They share saved pages, not run ids.
The first wave's default `max_questions` is four. The default `parallel_branches` is four, so these follow-ups run together.
The first wave's hard caps remain ten questions and four branches at once.

Each follow-up asks for one thing. Prefer gaps that block a direct answer. Target exact settings, commands, versions, prices and dates.
Cover named products, people or candidates that the findings mention but do not explain. Do not widen the topic.

Each branch gets 16 search or fetch jobs, four workers and 12 research minutes.
A temporary base failure leaves the parent open for resume. A branch that cannot start is recorded without stopping the others.

One `SYNTHESIS` writer call gets the main question and all admitted findings from the base and follow-ups.
Each finding is labelled with its question. The writer organises one answer around the main question, not the follow-ups.
Sources disagree only when two findings state conflicting facts about the same thing. Explain each disagreement once, name both sources and cite both.
Never call agreement, a gap or a single source a disagreement.
The writer does not write separate sections and an opening in separate calls.
The unknown-id rule still applies. An id the writer was not given stops the run with no answer.

### Review round

Only deepest mode can run one extra wave after its first review. Fast mode never gets this round.
The round is skipped when any of these apply.

- The verdict is `accept`.
- The review has no usable missing parts.
- `deepest.review_round.enabled` is false, `0`, `no` or empty. It defaults to true.
- `deepest.review_round.max_questions` is 0.

Otherwise one writer call turns the missing parts into follow-up questions. It reuses `FOLLOWUPS` and `pick_questions`.
Questions matching earlier follow-ups are dropped. `deepest.review_round.max_questions` defaults to four and has a hard cap of four.
This differs from the first wave's cap of ten. The extra round adds a few minutes and more model calls.

### Second answer and archive

The extra child wave uses the first wave's limits and child id rule.
Then synthesis uses every admitted finding from the base and both waves. Sentence checks, review and finish run again.
The second review is final, whatever its verdict. There is no third round.

Before the second synthesis, the controller moves the first answer files into `round1/` inside the run folder.
It keeps these files and their pre-check copies.

- `answer.md`, `evidence.md` and `citations.json`.
- `quote_check.json`, `sentence_check.json` and `grades.json`.
- `review.json` and `not_verified.json`.

### Review round record

The `review_round` entry in `run.json` says whether the round ran or why it was skipped. The reasons are `accepted`, `no_missing_parts`, `disabled`, `no_questions` when every question repeated an earlier one, and `pending` when the round was not reached.
It holds the questions and branch counts. It also keeps the first review's readiness line and issue counts.
The CLI summary says whether the round ran. The final readiness line reflects the final review.
If the second round stops before a new answer is written, the record points at `round1/answer.md` and `round1/review.json`. It says it is the first-round answer. Its quote check and review fields describe the failed second attempt.

Resume works throughout the round, including the child wave, second synthesis and second review.
Finished work stays in place. A resumed round never starts a third round.

### Retry failed follow-ups

If `branches.json` or `review_branches.json` records failed follow-ups, run the same command again.
It retries failed, not stopped, follow-ups in either wave.
Completed follow-ups and saved pages stay in place. Then it redoes the answer, check, review and finish stages.
Earlier answer files are kept with a `.legacy-<stamp>` suffix before the retry.
There is no automatic follow-up retry within one run. Retrying either wave never starts a third review round.
Pass a new `--run-id` to start fresh.

Reading workers extract useful exact settings, commands and code. Code quotes may be shorter than eight words but must still meet the eight-character quote check.
Writers use no tables or block quotes. Any table cells or block quotes that slip through are still checked.

## Checks for both modes

Both modes check quotes on saved pages, then check meaning. They write the answer, check sentences, grade and mark unverified details.
They recheck citations before an independent review. The review verdict does not stop the run. Read its issues before relying on the answer.

A checker reply with trailing commas is parsed once more without those commas. A sentence batch with no usable verdict gets one retry.
An unchecked sentence keeps its meaning-check grade and gains `(Not checked)`.
Narrowed sentences keep their own grades even when their wording changes sentence boundaries.

`SENTENCE_CHECK` accepts a claim that sources disagree or conflict only when its quotes do conflict.
Otherwise the sentence is unsupported.

### Inference sentences

`ANSWER` and `SYNTHESIS` require the `My inference:` label on reasoning beyond the findings.
They allow at most three such sentences. Leave out any further reasoning.
They must never guess a name, setting, price, version, who said something or who owns it.

The sentence checker treats each inference sentence as a claim. It uses quotes from citations in the same paragraph or bullet.
No nearby citations means the sentence is unsupported. Remove it without sending it to the checker.
Other inferences use the same batches, prompt and parsing as cited facts.
The check accepts different letter case, leading stars and a bold lead-in before the label.

Keep a supported inference. Narrow one that is only partly supported. Keep the `My inference:` prefix in both cases.
Kept inferences do not gain citation markers from nearby facts.
Remove unsupported inferences to `evidence.md`. Count inference sentences separately in `sentence_check_counts`.

### Dates for current claims

`_CLAIM_RULES` requires a quote with the page's date for availability, live status, support, releases or prices.
This includes claims that something is not available. If the date cannot fit in the quote, say the quote gives no date.

`ANSWER` and `SYNTHESIS` must name the source date for current availability, support or price claims.
Use wording such as `As of a May 2026 report`. With no source date, say `an undated page says`.

`CHECK` and `SENTENCE_CHECK` mark current-status claims as only `partly` supported when the quote gives no date.
The narrowed claim must say the quote gives no date. Never add a date the quote lacks.

## Fact grades

Fact grades use the NATO Admiralty Code. The source mapping is ours, not a record of past reliability.

Grades are saved in `grades.json` but hidden from the answer by default. A blind check of 60 graded sentences across two trials found no link between grade and correctness. Each band was right about 85% of the time. The likely reason is that narrowing makes a weak-source sentence say who said it, so the sentence is true even when the source is weak. Set `grades.show_in_answer` to true in `config/fast-mode.json` only after a better check passes.

| Grade | Plain meaning |
|---|---|
| B | Own docs or a primary record, usually reliable |
| C | Independent account, fairly reliable |
| D | Forum, comment or repost, not usually reliable |
| F | Source reliability unknown |
| 1 | Confirmed by another independent origin |
| 2 | Fully supported by one origin |
| 3 | Supported only after narrowing |
| 6 | No usable support |

Reposts count once. All self sources count as one origin, including a project's docs and repo. Each page gets one source type, the one the checker gave most often for that run's findings on it.
Subdomains merge into the last two host labels. Use the last three for a two-letter country suffix after `com`, `net`, `org`, `gov`, `edu`, `co` or `ac`. This is not a full public suffix list. Shared-author sites still separate authors.
The run never gives A, 4 or 5.

The sentence splitter does not end a sentence at an initialism such as "U.S." or a short abbreviation such as "v." or "Inc.". A citation or closing quote straight after one still ends the sentence. An uncited sentence ending in "U.S." can therefore join the next sentence. The joined sentence is checked in full against its citations.

## Lessons for deepest research

Runs share saved pages, so a follow-up reads pages instead of fetching them again. Keep these rules.

- Keep the meaning check on every path. A finding that says more than its quote spreads across every path that reuses it. This check does not see later writer expansions, so the combined answer still needs its sentence check and review.
- The checker needs the page title and address. Without them it stripped who said a thing, so a vendor's sales line or an issue comment read as plain fact.
- The writer can still say more than a finding. The final review is asked to check every citation, but it still misses some. Check the combined write-up sentence by sentence.
- Speed is not depth. Fast runs finish in minutes and still leave gaps. An answer can omit exact configuration or operating commands even when its other citations pass. Seed follow-up questions from "What is still missing", from rejected findings and from the review's missing parts.
- Sources about a side package can be read as the main project. Keep each finding tied to the product it is about.
- A rejecting review does not stop either mode. Review issue counts stay in `## Checks` and the run record.
- Test any judge on known good and bad answers before tuning against it. The reviewer here rejected every answer, so its verdict no longer sets the readiness line.
- Complete an interrupted `round1/` archive before retrying failed branches. Keep the first answer unchanged.
- Reconsider a skipped review round when a first-wave retry changes the review. Never reopen an extra wave that already started.
- Liveness checks on worker processes must tolerate a process that has only just started.
- Metered search and fetch charges are not in the cost record. Many paths multiply them.

## Limits

The default limits are eight workers at once and at most 38 search or fetch jobs. Code caps them at eight and 40 whatever the config says. Research stops starting new jobs after 25 minutes. Change limits in `config/fast-mode.json`, not in code.

All controllers share 24 worker slots per host by default. Waiting for a slot counts against the worker timeout.
Waiting for an adopted worker has its own timeout. A fresh launch gets the full timeout, including its slot wait. Ctrl-C leaves a running worker available for adoption on resume.
A kernel lock allows only one controller per run. It is released when the controller exits or dies.

## Common pitfalls

1. Workers are launched as `hermes chat --oneshot -Q --ignore-rules -t web,terminal,file` with an absolute `--query-file`. Use the restricted one-shot worker pattern in `deep-research` and verify the installed CLI supports it. Keep `HERMES_DISABLE_LAZY_INSTALLS=1`, which the launcher sets.
2. Cheap models paraphrase quotes. Those findings fail the check and never reach the answer. Expect some failures. A high failure rate means the fetch prompt needs work, not a looser check.
3. Never loosen the quote check to make a run pass. Fix the prompt or the page capture.
4. Cost comes from Hermes session records. Metered `web_search` and `web_extract` charges are not in those records. Say so when reporting cost.
5. Hermes writes some cost rows, such as title generation, after a session closes. Run `record` again later for final figures.
6. A search pick that was not in that worker's own search results is dropped. This stops made-up URLs. A search job whose results could not be read back queues no pages at all.
7. The writer can cite an id it was never given, or one that failed its check. The run then stops with no answer. The draft is kept in `answer_draft.md` and `quote_check.json` names the ids.
8. Run long commands in the background. A foreground terminal call can end the controller mid-run.
9. Do not point two controllers at one run at the same time. Different runs on one root are fine. A page that one run is fetching is held for it. Other runs wait and then read the saved copy. If that run's controller dies, or its hold runs out, the page stays held while its worker still runs. The next run then saves what that worker fetched and fetches again only if it got nothing.
10. The quote check proves a quote is on its page. It does not prove the quote supports the claim. The meaning check screens findings before writing, but the writer can still broaden them. Still read the review before using the answer.
11. A run can finish well inside its time budget with gaps left. A short runtime does not show that exact settings, commands or other required details were found. The answer's "What is still missing" section names such gaps.
12. A resumed job must save pages from its old session before fetching again. Empty worker output does not mean the fetch failed. Wait for a live worker to end first.
13. A process that has just started can show an empty command line. Reading it then made a live worker look dead under load. The liveness check rereads it briefly. A zombie counts as dead.
14. The meaning checker must come from a different vendor than the worker. Preflight stops the run if they match. A checker reply with no usable verdict keeps those findings out. It never lets them through.
15. A narrowed claim may not add a number that its quote lacks. Such a claim counts as unsupported.
16. A failed final citation check is saved in `quote_check.json` but does not stop the run or the review. The `run` command exits with code 2 instead. Always read the file.

## Operator guide

This skill is the public operator guide. Keep its run order, role handoffs, restart rules, limits and checks aligned with the bundled code. No private operator document is needed to run either mode.

Fast and deepest modes do not export deliverables or need a source-intelligence service. Configuration alone does not authorise export or private-context access.

## Verification checklist

- [ ] `run.json` shows status completed, wall time, job counts and cost.
- [ ] `quote_check.json` verdict is pass.
- [ ] `run.json` shows the meaning check counts, and dropped findings are few.
- [ ] `review.json` shows a different vendor from the writer and a saved verdict. A rejected review is not approval, even when the run completed.
- [ ] `sentence_check.json` and `grades.json` show what was narrowed, removed or left unchecked.
- [ ] `sentence_check_counts` counts inference sentences separately.
- [ ] `evidence.md` holds citation quotes, saved page paths and removed claims.
- [ ] The readiness line matches the review's missing parts, not its verdict. Every missing part is listed. The CLI summary gives the review path.
- [ ] Deepest `run.json` records whether the review round ran and keeps the first review's readiness and issue counts.
- [ ] A deepest review round keeps the first answer in `round1/` and stops after its second review.
- [ ] Current availability, support and price claims state the source date or say the source is undated.
- [ ] The answer says what is still missing.
