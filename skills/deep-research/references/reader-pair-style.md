# Reader pair structure and style

This is the shared instruction source for both readers, their editors and repairs.
Purpose is explicit. A company, market, product or pricing topic does not request a recommendation.

## Shared prose

Use direct Australian English and complete sentences. Answer first, then explain why and how.
Keep each important quantity with its subject, unit and period. Preserve scope, dates, definitions and qualifications.
Keep enough explanation for a reader to follow the argument. Do not replace explanation with fragment chains.
Use descriptive source links from the accepted evidence. Never invent facts, citations or certainty.
No hype, consultant language or research-method chatter. Process records belong in sidecars.
Short sentences help, but sentence counts do not define good writing. Do not manufacture criticism of clear supported prose.

## Mechanical prose checks

Aim for 12 to 18 words per sentence. Split sentences over 25 words. Never exceed 35 words.
Use one idea per paragraph. No semicolons, em dashes, middle-dot chains or nested bullets outside a fixed decision form.
Avoid long bracketed asides and packed lists inside sentences. Keep tables compact and explain what they mean.
Do not alter the prose checker or remove factual qualifications merely to pass it.

## Factual brief

Use a single `#` title. Open with `**Bottom line.**`, the answer and the material limitation. Then use these sections.

- `## What was asked`
- `## What we found`
- `## What is still missing`

Use connected explanations, not a list of unexplained facts. Link to the detailed reference pack.
There is no required recommendation, options table, risk/stop section or owner decision.
Do not add those chapters merely because the topic involves a company or price.
The brief has a 2,000-word cap and no word floor. Do not pad a simple answer.

## Technical assessment brief

Use the factual brief sections, then add these sections for requested technical judgement.

- `## Technical judgement`
- `## Alternatives and trade-offs`

Separate evidence from judgement. Explain criteria, trade-offs, limits and what would change the judgement.
This requires genuine research and the normal checks. Evidence-reuse-only assessments are unsupported.
No mandatory commercial or purchaser framing. Only include an owner decision when one genuinely exists.
If using `## What the owner needs to decide`, use the existing compact C1 form, or exactly `No call needed.`.

## Reference pack

Organise around the research questions. Answer them with evidence, why/how explanations, useful detail and conflicting interpretations.
Include scope, dates, definitions, source links and material gaps. Do not impose irrelevant chapters.
Use diagrams only when they clarify the answer and inspect their rendered form.
Review the full reference pack before deriving the brief. Derive the brief only from accepted content.
If reference content changes, recheck the pack and brief. Keep the answer and limitations consistent across both readers.

## Existing QA review

Within the existing review, report distinct factual, reasoning, editorial and presentation checks.
Review the full pack first against saved evidence. The later brief editor reads the full argument and both readers, not number counts.
Review important quantities and qualifications semantically: subject, unit and period, scope, date and uncertainty must retain their meaning.
Record a basis for each result. A factual correction or lost qualification is blocking, never optional style.
Keep healthy supported prose. Repair only affected content. Do not add model review stages for these dimensions.

Use the existing evidence/privacy receipt for the full-pack results. Include `status`, `blocking`, `checks` and `important_quantity_qualification_review`.
Bind the current full-pack bytes with `reference_pack_sha256` for layout v2 or `internal_full_pack_sha256` for the older layout.
Each `checks` entry, named `factual`, `reasoning`, `editorial` and `presentation`, has a `verdict` of `PASS` or `FAIL` and a non-empty `basis`.
The `important_quantity_qualification_review` object has the same `verdict` and `basis` fields.
A `PASS` requires an empty `blocking` list, supported quantities and qualifications, and all four dimensions passing.
These fields supplement existing identity, privacy, source, acquisition and quote checks. They do not replace them.
Do not use a fixed review-price estimate in a reader pack. Unknown or stale price measurements remain unknown.
