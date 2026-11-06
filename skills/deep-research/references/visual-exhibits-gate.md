# Deep Research Visual Exhibits Gate

Use this when a research campaign produces a reference pack.
Useful tables and diagrams explain the evidence rather than decorate the report.
Factual exhibits answer the research questions. They need no invented commercial decision.

Exhibits live in the reference pack. The plain brief carries none: it is plain text for a ten-minute read. (Legacy four-output campaigns keep the older rules at the end of this file.)

## Required threshold for `true_deep_research`

A `true_deep_research` closeout must include:

- at least **3 meaningful exhibits** in the reference pack;
- at least **1 decision-supporting diagram** when the target has systems, workflow, architecture, product, market, category, internal-system, repo/tool, venture, or operating-model structure. A diagram omitted after repairs stopped helping counts, if the pack carries its note (see `references/diagram-native-review.md`);
- an explicit exhibit ledger at `synthesis/visual-exhibits.json`;
- every counted exhibit tied to evidence and a stated reader use.

There is no exhibit minimum for the plain brief. Decorative charts, placeholder tables, repeated prose bullets, and diagrams that do not clarify a research question or decision do not count.

## Output-class rules

- Exhibits use the output class `reference_pack`. The ledger `appears_in` lists `reference_pack`.
- Exhibit IDs, titles, labels, scales, legends, captions, and ordering must be consistent inside the reference pack.
- Numbers and confidence labels must match source data and evidence class.
- The visual QA receipt (`qa/visual-review.json`) has `output_classes` for `reference_pack` and `plain_brief`, both PASS. The plain brief class checks that the brief carries no exhibit, code or process label.

## Exhibit types that count

Use the type that best explains the evidence or requested decision:

- `diagram` — architecture, system, sequence, dataflow, lifecycle, service blueprint, market map, or workflow map.
- `matrix` — competitor matrix, capability matrix, option comparison, feature/fit matrix.
- `scorecard` — scored build/buy/borrow/watch/kill or vendor/source evaluation.
- `portfolio` — option portfolio by value/risk/reversibility/cost.
- `heatmap` — evidence confidence, risk, coverage, missing-proof, or adoption heatmap.
- `timeline` — roadmap, incident chronology, maturity trajectory, release history.
- `table` — dense decision table with real evidence, not a formatting-only table.
- `map` — category map, source/corpus map, ecosystem map, operating model.

## Required ledger shape

`visual-exhibits.json` should use this shape:

```json
{
  "schema_version": "deep-research-visual-exhibits-v2",
  "status": "complete",
  "required": true,
  "topic_requires_diagram": true,
  "thresholds": {
    "full_pack_min_exhibits": 3,
    "board_brief_min_exhibits": 0,
    "diagram_min_count": 1
  },
  "exhibits": [
    {
      "id": "E1",
      "title": "Evidence confidence heatmap",
      "type": "heatmap",
      "output_class": "reference_pack",
      "appears_in": ["reference_pack"],
      "anchor": "Exhibit E1 — Evidence confidence heatmap",
      "source_data_refs": ["S1", "S4", "lane:03-evidence-claims-map"],
      "evidence_refs": ["S1", "S4"],
      "caption": "What the exhibit says in plain language.",
      "encoding": "Rows are claim areas; columns are evidence quality and decision risk.",
      "scale_or_legend": "green=strong, amber=partial, red=unsupported",
      "render_method": "markdown_table",
      "source_artifact": "synthesis/exhibit-data/e1.json",
      "rendered_artifact": "internal/reference-pack.md#exhibit-e1-evidence-confidence-heatmap",
      "validation_receipt": "qa/diagram-e1-validation.json",
      "reviewer": "visual-qa-worker",
      "uses_internal_context": false,
      "decision_use": "Shows which recommendations are proven versus weak before the owner approves build or adoption."
    }
  ]
}
```

Rules:

- `status` must be `complete`, `pass`, or `verified` for the visual gate to pass.
- `appears_in` should be `["reference_pack"]`.
- `anchor` must appear in the relevant Markdown or `source_artifact`/`rendered_artifact` must point to an existing artifact path.
- `evidence_refs`, `source_data_refs`, or `evidence_basis` must connect the exhibit to lane/source evidence.
- `decision_use` is the legacy field name for reader use. For factual work, explain which question the exhibit clarifies. Do not invent a decision.
- reader-facing diagrams use the normal render and native-pixel checks. The reviewer developer must differ from every actual writer. Bind actual writer-call evidence as described in `references/diagram-native-review.md`. Record `render_method: technical_diagrams` and the passing release record.
- Mermaid is a drafting format only. Diagrams in the reference pack, which is eligible for deliverables export, use the approved Archify PNG.

## Verification behavior

`verify_campaign_depth.py --mode true_deep_research` fails closed to `HOLD memo_depth` when:

- `synthesis/visual-exhibits.json` is missing or malformed;
- the ledger status is not complete/pass/verified;
- fewer than 3 valid exhibits appear in the reference pack;
- an exhibit anchor is missing from the reference pack;
- a structural target lacks a valid diagram or a disclosed omission;
- counted exhibits lack evidence linkage or decision use;
- a rendered structural diagram lacks a validation receipt;
- the visual receipt lacks a PASS for `reference_pack` or `plain_brief`.

This gate is about evidence and reader usefulness, not decorative polish. Do not require image generation, screenshots, or polished graphic design to pass; structured matrices, tables, scorecards, maps, and heatmaps are enough when evidence-backed.

## Legacy layout

A campaign without `output_layout: brief-and-reference-v2` keeps the four-output rules: at least 3 exhibits in the internal full pack and 2 in the internal executive brief, output-class consistency across `external_full_pack`, `internal_full_pack` and `internal_executive_brief`, external-pack exhibits that use external evidence only, executive-brief exhibits that are faithful subsets of full-pack exhibits, and a visual receipt with those three classes. The verifier applies them to legacy campaigns as before. They are not migrated.
