# Phase 0 pre-seed controls

Comparative or recommendation-oriented deep-research campaigns must pass a Phase 0 pre-seed gate before target-specific comparison lanes are treated as started or complete.

## Required artifacts

`init_campaign.py` creates four campaign-local JSON/Markdown artifacts under `preseed/` and references them from `manifest.json.pre_seed`:

1. `source-characterization-v1` — records the original supplied target/source as `user_supplied_seed` and explicitly rejects incumbent/default/preferred/priority inference.
2. `need-map-v1` — describes workload, outcome, and constraint needs without selecting a solution.
3. `candidate-universe-v1` — records candidate classes and discovery provenance. A passing universe must cover product, process, architecture, current-state/do-nothing, and adjacent-category options with independent non-seed provenance.
4. `comparison-charter-v1` — freezes hard gates, equal evidence floor/budget, source classes, rubric, blinded aliases/order, unblind mapping, duration provenance, and separated decision states.

The scaffold is intentionally initialized as `HOLD`; it is not a pass artifact.

## Fail-closed verifier behavior

`verify_campaign_depth.py` delegates pre-seed checks to `scripts/preseed.py` whenever `manifest.json` contains a `pre_seed` key, in every verifier mode (`brief`, `board_brief_only`, `refresh_delta`, and `true_deep_research`). Legacy manifests retain narrow backward compatibility only when the `pre_seed` key is absent. Current scaffold initialization fails closed if the pre-seed helper cannot be imported; it must not emit `pre_seed: null`.

The verifier HOLDs on:

- missing, invalid, or hash-drifted pre-seed artifacts while still in scaffold HOLD state;
- treating the user-supplied seed as incumbent, default, preferred, priority, or winner solely because it was supplied;
- need maps that select a solution, set `selection_language_forbidden` to anything other than `true`, include selected/preferred/default/incumbent fields, or use obvious selection/recommendation language in workloads, outcomes, or constraints instead of neutral need language;
- missing required candidate classes or independent discovery provenance;
- evidence budget/floor asymmetry, omissions, extras, unequal normalized allocations, or placeholder evidence-floor dimensions across candidates;
- an unfrozen or incomplete comparison charter;
- unsupported duration represented as evidence;
- collapsed decision states (`eligible_set`, `benchmark_winner`, `mission_selection`, `pilot_selection`, `activation_status`);
- benchmark winners whose hard eligibility gate results are missing or anything other than explicit pass values (`true`, `PASS`, or `PASSED`);
- pilot selection implying activation.

The pre-seed helper also exposes a deterministic permutation digest that ignores candidate names and list order, so regression tests can prove candidate-name/order invariance.

## Operator rule

Do not start comparison scoring, target-specific recommendation lanes, pilot selection, or activation language until all four artifacts validate and the charter is frozen. Generated parent/child/worker packets may authorize only universal characterization and evidence setup before the pre-seed gate passes. A pilot remains proposal-only unless separate owner approval authorizes activation.
