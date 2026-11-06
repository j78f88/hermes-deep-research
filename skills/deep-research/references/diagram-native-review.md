# Diagram native-pixel review (reader-facing diagrams)

Every diagram shown in a deep-research pack must pass independent native-pixel review. Deterministic render checks and the builder’s own inspection do not establish that labels, connectors and meaning are clear to a reader. A prose description of an image is not a substitute for reviewing its actual pixels.

## Why native pixels

`hermes chat --image` does not show the reviewer the image. When `auxiliary.vision` is set, Hermes first describes the image with the auxiliary vision model. The reviewer model then judges that text. `--ignore-user-config` does not change this. The builder's own `vision_analyze` uses the same auxiliary model, so it is not independent either.

`scripts/diagram_review_native.py` sends each PNG directly to the reviewer as an `image_url` part.
The shared settings select the approved OpenRouter route.
The existing secret reference is resolved only for the call and is never printed.
Each view records the served model, response id, image hash and prompt hash.
The reviewer developer must differ from every actual diagram writer.
The requested builder model alone cannot prove who wrote the diagram.

## Writer identity checks

Supply a JSON list of shared-client writer calls, or an object with a `calls` list.
Include every contributing writer, rewriting editor and served fallback.
Before buying image reviews, the script checks each declared generation with the existing provider lookup.
The returned model must match the declared served model exactly.
The existing catalogue maps that confirmed model to its developer.
Neither a caller's `identity_verified` flag nor its developer field authenticates a call.

Missing, unknown, mismatched or unavailable provider identities stop acceptance before image review.
The visual output records the safe lookup results under `writer_verification`.
It never saves raw lookup replies or error text.
Failed attempts without any response identity may stay in the supplied list.
Any declared generation counts for independence, including a truncated attempt.
The returned reviewer must also differ in developer from every authenticated contribution.

The check cannot prove that the supplied author list is complete.
It also cannot prove which image bytes a declared call contributed.
A saved JSON approval remains writable.
Diagrams remain optional for factual work.
Use relation tables when they carry the factual proof more clearly.

## The rubric

Use these rules for both review and repair. Judge the rendered image, not only the source or accompanying text.

| Rule | What fails | Repair principle |
|---|---|---|
| R1 Arrowheads | Lines that merge, tee or share an arrowhead into a box | Route each relationship with a distinct endpoint |
| R2 Legibility | Labels, identity text or legends unreadable at full view | Increase readable text size and simplify labels |
| R3 Dead space | Stretched layouts, long runs, mostly empty containers or excessive margins | Tighten the composition without crowding |
| R4 Nothing covers a line | Boxes, labels or keys sitting on connectors | Move labels clear of line paths |
| R5 Furniture | Keys, legends or cards that dominate or float away | Keep explanation near the relationship it explains |
| R6 Identity | Title missing from pixels; IDs or paths broken across lines | Render the title and preserve identifiers |
| R7 Direction | Direction unclear at a junction | Make source, target and arrow direction explicit |
| R8 Meaning on image | Required conditions or line meanings exist only in a subtitle | Include the essential meaning in the exported pixels |
| R9 One idea | One box per speculative or unbuilt component | Group gated items and remove unnecessary detail |
| R10 Fidelity | Rendered arrows differ from the expected relationships | Compare the image with the required-edge inventory |

## Build rules that pass first time

- Count each node's connectors before layout. Archify anchors every connector at the centre of a side, so plan one connector per side. A node with five or more seams needs a simpler composition. Name the dropped seam in the caption.
- Group things that do not exist yet into one box, such as "Gated, not installed".
- Write conditions on the dashed lines instead of adding a key box. "When needed" and "if approved" are good examples.
- Put the title in the first identity box. Start the identity band at y=24, with rows about 120 to 150 apart.
- Place `labelAt` beside vertical lines, at the gap midpoint.
- Give the region border a solid 2px stroke and white fill in the export style. The default 1px over a translucent fill fails non-text contrast.
- Export at about twice the viewBox width with the collector's `--viewport`.

## Sequence

1. Write `brief.json` from `archify-diagrams/templates/diagram-brief.json`. Use `source` and `target` keys in `required_edges`, and a top-level `omit` list.
2. Render with `archify_sandbox_render.py render --type architecture --validate --check`. Keep a `--keep-source` checkout so `final-validate` and `final-check` can run.
3. Export offline, collect metrics at 2x and cut the top and bottom crops. The top crop is about 20% of the image, the bottom about 25%.
4. Run `python3 scripts/diagram_review_native.py --dir <d> --name <stem> --writer-calls <actual-writer-calls.json>`. Supply actual served writer-call records. Unknown identities block acceptance. The script freezes settings and saves raw responses before parsing.
5. Run the approved diagram QA and contract gates from the installed diagram toolchain; both must say APPROVE. These are optional external dependencies, not a private checkout assumed by this skill. Bind their verdicts, the image hash and native-review records into a deterministic release record with top-level `status: PASS`. If the required tooling is unavailable, record the gap and omit the diagram rather than inventing approval.
6. Point the exhibit ledger entry at the contract record with `render_method: technical_diagrams`. The verifier reads `validation_receipt.status`.
7. Embed the approved diagram in the reference pack with a caption that names any omitted seams. Keep the IR, HTML, metrics and records in the campaign. The Markdown export allowlist does not authorise copying additional image files; any separate asset delivery needs its own approved scope and readback.

## Repairs and omission

Repair while each attempt fixes a failing rule. Stop when a repair leaves the same rule failing, or adds a new failure. There is no fixed number of attempts.

When repairs stop helping, omit the diagram. This is routine work, so never send the owner a diagram question.

1. Put a one-line note in the reference pack where the diagram would have gone. Start it with "Diagram omitted:" and name the problem in plain words.
2. Record the diagram in the exhibit ledger with `status: omitted`, an `omission_reason` naming the failing rule, and `anchor` set to the note's exact text.
3. Carry on with the campaign. The verifier counts a disclosed omission toward the diagram requirement.

The owner can still ask for the diagram later. Stale approvals never carry to a new image hash.

## Calibration

`references/diagram-calibration/calibration.json` holds the owner's verdicts as anchors. Re-run the rubric on them after any rubric change. It must reproduce every verdict marked final, except recorded overrides.

Retain rejected and accepted calibration images with their verdicts, image hashes and any explicit overrides. A source-only or text-only replay is not native-pixel calibration.

Do not loosen a rule to pass one image. If the owner explicitly approves an image the rubric rejects, record an owner override with the approval wording, exact image hash and failing rule. Keep the rule unchanged; an override for one image does not approve later renders.
