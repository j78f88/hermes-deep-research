# Product Research Planning Example

This is an illustrative campaign plan using HumanLayer as a public product subject. It is not a completed campaign, a current vendor assessment or evidence for a recommendation. Retrieve current sources before making product claims. The filename is retained for existing documentation links.

## Intake

- Target: HumanLayer
- Target type: product/company with public repositories and explanatory content
- Discovery URL: `https://www.humanlayer.com/`
- Factual question: What does the product do, how is it documented, and which capabilities remain unverified?
- Optional technical-assessment question, only if requested: How does it compare with eligible alternatives for the stated workload?
- Output boundary: campaign-local corpus, reference pack and plain brief; reviewed deliverables export only when appropriate for the audience
- Owner gates: no purchase, account creation, customer data, upstream execution, outreach, implementation or legal/product-direction decision

## Parent campaign shape

Example title:

```text
Deep research HumanLayer — product capabilities and evidence gaps
```

Definition of done:

- actual research lanes complete, not only scaffolded;
- current source captures retained with hashes and verbatim supporting quotes;
- product claims separated from repository observations and analyst judgement;
- reference pack accepted before deriving the plain brief;
- independent evidence/privacy, visual, taxonomy and final-assurance checks complete;
- action candidates retained in a sidecar when relevant;
- citations verified, contradictions preserved and missing evidence disclosed;
- no-execution and transient-snapshot cleanup checks recorded;
- completion command returns its real verdict, with HOLD or FAIL reported honestly.

## Candidate lane plan

1. Official product/docs/pricing/security truth
2. Repository-family static inspection and licence boundaries
3. Capability and architecture evidence
4. Customer reports and public failure modes
5. Independent corroboration and contradictory evidence
6. Alternatives, only when comparison is requested and pre-seed controls pass
7. Adversarial/privacy/lock-in review
8. Isolated internal-context retrieval, only when permitted
9. Controller synthesis, plain brief and independent verification

Keep public workers isolated from internal retrieval. Adjust the lane count to meet the selected depth mode without adding irrelevant market or purchaser chapters.

## Artifact paths

Resolve environment variables before use:

- working campaign: `~/.hermes/work/<run-id>/`;
- archive: `${RESEARCH_ARCHIVE_ROOT}/runs/YYYY-MM/<run-id>/`, default root `~/.hermes/research-archive`;
- reference pack: `<campaign-root>/internal/reference-pack.md`;
- plain brief: `<campaign-root>/internal/plain-brief.md`;
- action register: `<campaign-root>/synthesis/action-register.md`;
- verification: `<campaign-root>/receipts/`;
- deliverables export: directly under `${DELIVERABLES_EXPORT_PATH}`, default `~/research-deliverables`;
- optional source corpus: `${SOURCE_INTELLIGENCE_ROOT}`, default `~/source-intelligence`.

Only the reference pack and plain brief are exportable. Declare their exact destination filenames in the manifest, not a dated path copied from another run.

## Receipt rule

Generate verification and completion receipts through the existing tools. Fill them from actual file, provider and destination readbacks. Do not copy example counts, invent hashes or author a PASS manually. A receipt reports a check; rerunning the check establishes current acceptance.

## Illustrative action-register rows

These rows are possible questions or dispositions, not findings about the product.

| ID | Idea/source | State | Next move | Proof / trigger |
|---|---|---|---|---|
| AR-01 | Self-contained work packets | candidate | Compare packet and readback patterns | A reviewer can understand a task without prior chat history |
| AR-02 | Artifact-first review | candidate | Evaluate the review workflow against the stated need | An authorised user can review evidence, comments and the next action from one surface |
| AR-03 | Repository adoption | not approved | Inspect the current pinned source and licence without execution | Maintained source, compatible licence and separate implementation approval |
| AR-04 | Unsupported capability | unknown | Seek a current primary capture or a safe approved test | A reproducible source-bound observation, not a marketing inference |

## Rules the example demonstrates

- Derive the plain brief only after the reference pack is accepted.
- Keep repository truth separate from current hosted-product truth.
- Use issue reports as failure-mode evidence, not prevalence without a denominator.
- Compare eligible alternatives under equal evidence floors when judgement is requested.
- Reuse the research contract, not another campaign’s vendor conclusion.
