# Neutral-comparison and anchoring-bias gate

Use this whenever a deep-research request names, links, or foregrounds one product, vendor, framework, architecture, or duration while asking for comparison or recommendation.

## Core rule

A user-supplied target is a **research subject and hypothesis**, not the default candidate or presumed winner. A supplied duration is a **constraint or heuristic**, not an evidence-backed recommendation unless independently justified.

## Required controls

1. Preserve the original framing verbatim, then rewrite the decision question in workload/outcome-first neutral language.
2. Build a candidate registry before allocating research lanes. Mark which candidates were user-supplied, discovered, incumbent, excluded, or benchmark-only.
3. Define hard eligibility requirements separately from weighted preferences and later activation gates.
4. Give every eligible candidate the same minimum evidence dimensions and source-class/freshness floor. A dedicated target lane plus one grouped competitor lane is not comparable evidence.
5. Score candidates under stable aliases after freezing criteria, weights, and tie-breaks. Unblind only after scores and evidence references are fixed.
6. Report these states separately:
   - `eligible_set`
   - `benchmark_winner`
   - `mission_selection`
   - `pilot_selection`
   - `activation_status`
7. Track every fixed duration with provenance: `evidence`, `user_constraint`, `heuristic`, or `policy`. A heuristic must be labelled as such and must not silently become a required programme length.
8. Fail closed on a comparative recommendation when candidate coverage is materially asymmetric, the rubric was not frozen before scoring, or the user-supplied target is promoted from eligible to preferred without comparative evidence.

## Required QA questions

- Did the user-supplied target receive more evidence depth than alternatives?
- Did missing competitor evidence get misread as competitor weakness?
- Did synthesis wording change “eligible/suitable” into “use/recommend”?
- Did a duration or phase plan enter through an analyst prompt and later appear as if externally evidenced?
- Are benchmark winner, mission selection, pilot candidate, and activation authority distinct?
- Would the recommendation survive candidate-name and ordering permutation?

## Regression fixtures

At minimum test:

- named vendor plus unnamed alternatives → neutral reframe, no presumed winner;
- same evidence with candidate names/order permuted → same eligibility and score result;
- one candidate with deeper evidence → HOLD for evidence asymmetry;
- user-supplied duration without evidence → labelled heuristic, not recommendation;
- benchmark winner that violates a hard requirement → not implementation-eligible;
- selected pilot candidate with no activation authority → activation remains blocked;
- benign words such as `gateway`, `token`, and `only` → no unrelated owner-gate classification without action/context evidence.

## Historical-claim discipline

One confirmed biased campaign proves the failure mode exists. A QA detector miss proves detector incompleteness. Neither proves the failure was historically common. Claim prevalence only from a scoped retrospective with an explicit denominator and comparable classification criteria.
