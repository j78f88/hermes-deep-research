#!/usr/bin/env python3
"""prior-work-aware playbook and improvement loop.

This module is the deterministic, offline evaluator that ISSUE-572 owns for the
ISSUE-569 evidence-to-output assurance pipeline. It composes three existing proven
pieces rather than inventing a new improvement framework:

  1. ``evaluate_prior_work_context`` — validates a ``prior-work-context-v1``
     artifact, HOLDs when a required prior-work surface (Linear, Knowledge base) is
     silently missing or unchecked, and surfaces ``duplicate_do_not_build``
     matches for the playbook gate.
  2. ``evaluate_playbook`` — validates an
     ``implementation-or-operating-playbook-v1`` artifact against its bound
     prior-work context: it refuses PASS when the context is incomplete, when the
     bound context hash drifts, when a stage proposes to build prior work already
     classified ``duplicate_do_not_build``, when a stage cites a prior-work ID that
     does not exist, or when the promotion guard would allow automatic mutation.
  3. ``evaluate_improvement_record`` — validates a
     ``research-improvement-record-v1`` artifact: an implementation with no
     observed outcome evidence is ``not_yet_measurable`` (never ``confirmed`` /
     "improved"), a refuted result must carry a reviewed correction proposal and
     preserve the failed approach, and promotion cannot self-approve without an
     independent review.

The module performs **no** live runtime, Linear, export, credential, network,
push or merge work. It only reads dict inputs / local JSON and returns verdicts
whose ``state`` values come from the shared ISSUE-569 state enum. All evaluators
are pure: they never mutate their inputs.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

try:  # jsonschema is a repo dependency; degrade to structural checks if absent.
    import jsonschema

    _HAS_JSONSCHEMA = True
except ImportError:  # pragma: no cover - environment without jsonschema
    _HAS_JSONSCHEMA = False

SCRIPT_DIR = Path(__file__).resolve().parent
REFERENCES_DIR = SCRIPT_DIR.parent / "references"

PRIOR_WORK_SCHEMA = REFERENCES_DIR / "prior-work-context-v1.schema.json"
PLAYBOOK_SCHEMA = REFERENCES_DIR / "implementation-operating-playbook-v1.schema.json"
IMPROVEMENT_SCHEMA = REFERENCES_DIR / "research-improvement-record-v1.schema.json"

# Surfaces the prior-work scan must have checked before a playbook may PASS.
REQUIRED_SURFACES = ("linear", "knowledge_base")

# Relationships that mean "do not build this again".
DUPLICATE_RELATIONSHIPS = {"duplicate_do_not_build", "superseded"}

# Outcome classifications that assert an improvement / settled result and
# therefore require observed evidence to be present.
EVIDENCE_REQUIRING_OUTCOMES = {"confirmed", "mixed", "refuted", "context_limited"}

# Every state this module may emit. Kept a strict subset of the ISSUE-569
# shared state enum (ARCHITECTURE-AND-IMPLEMENTATION-PLAN.md "State machine").
EMITTABLE_STATES = {
    "blocked_invalid_input",
    "blocked_missing_prior_work_context",
    "blocked_duplicate_prior_work",
    "blocked_unverified_improvement",
    "blocked_owner_gate",
    "prior_work_validated",
    "owner_review_ready",
    "outcome_review_due",
    "improvement_reviewed",
}


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def stable_hash(value: Any) -> str:
    """Deterministic SHA-256 over a JSON-serialisable value.

    Matches ``campaign_controller.stable_hash`` so hashes are comparable across
    the deep-research toolchain.
    """
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_schema(path: Path) -> dict | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _schema_errors(instance: Any, schema_path: Path) -> list[str]:
    """Return a list of schema validation error strings (empty when valid)."""
    schema = _load_schema(schema_path)
    if schema is None:
        return [f"schema not found: {schema_path.name}"]
    if not _HAS_JSONSCHEMA:  # pragma: no cover - only when jsonschema missing
        # Minimal structural fallback: required top-level keys must be present.
        required = schema.get("required", [])
        return [f"missing required field: {k}" for k in required if not (isinstance(instance, dict) and k in instance)]
    validator = jsonschema.Draft202012Validator(schema)  # type: ignore[possibly-unbound]
    return [f"{'/'.join(str(p) for p in err.path) or '<root>'}: {err.message}" for err in validator.iter_errors(instance)]


def _verdict(status: str, state: str, reasons: list[str], **extra: Any) -> dict:
    assert state in EMITTABLE_STATES, f"unknown state {state!r}"
    result = {"status": status, "state": state, "reasons": reasons}
    result.update(extra)
    return result


# --------------------------------------------------------------------------- #
# Prior-work adapter                                                           #
# --------------------------------------------------------------------------- #

def duplicate_prior_work_ids(context: dict) -> list[str]:
    """IDs of prior-work matches that must not be rebuilt."""
    ids = [
        m["prior_work_id"]
        for m in context.get("matches", [])
        if m.get("relationship") in DUPLICATE_RELATIONSHIPS
    ]
    return sorted(set(ids))


def _missing_required_surfaces(context: dict) -> list[str]:
    """Required surfaces that were not genuinely checked."""
    by_surface = {s.get("surface"): s for s in context.get("surfaces", [])}
    missing = []
    for name in REQUIRED_SURFACES:
        surface = by_surface.get(name)
        if surface is None:
            missing.append(f"required prior-work surface missing: {name}")
        elif not (surface.get("checked") is True and surface.get("status") == "checked"):
            missing.append(f"required prior-work surface not checked: {name}")
    return missing


def evaluate_prior_work_context(context: dict) -> dict:
    """Validate and classify a ``prior-work-context-v1`` artifact."""
    if not isinstance(context, dict):
        return _verdict("FAIL", "blocked_invalid_input", ["context is not an object"], duplicate_prior_work_ids=[])

    # A silently missing / unchecked required prior-work surface is a domain HOLD,
    # not a generic schema FAIL. The schema's own `contains` guard for linear /
    # knowledge_base would otherwise mask this as blocked_invalid_input, collapsing the
    # distinct SMOKE-PROTOCOL route blocked_missing_prior_work_context. Check
    # surface completeness first so the correct route is emitted.
    missing = _missing_required_surfaces(context)
    dupes = duplicate_prior_work_ids(context)
    if missing:
        return _verdict(
            "HOLD",
            "blocked_missing_prior_work_context",
            missing,
            duplicate_prior_work_ids=dupes,
        )

    errors = _schema_errors(context, PRIOR_WORK_SCHEMA)
    if errors:
        return _verdict("FAIL", "blocked_invalid_input", errors, duplicate_prior_work_ids=[])

    return _verdict(
        "PASS",
        "prior_work_validated",
        [],
        duplicate_prior_work_ids=dupes,
        prior_work_context_sha256=stable_hash(context),
    )


# --------------------------------------------------------------------------- #
# Playbook builder / evaluator                                                #
# --------------------------------------------------------------------------- #

def _known_prior_work_ids(context: dict) -> set[str]:
    return {m.get("prior_work_id") for m in context.get("matches", [])}


def evaluate_playbook(playbook: dict, prior_work_context: dict) -> dict:
    """Validate an ``implementation-or-operating-playbook-v1`` against its context."""
    if not isinstance(playbook, dict):
        return _verdict("FAIL", "blocked_invalid_input", ["playbook is not an object"])
    errors = _schema_errors(playbook, PLAYBOOK_SCHEMA)
    if errors:
        return _verdict("FAIL", "blocked_invalid_input", errors)

    # The bound prior-work context must itself be valid and complete. A playbook
    # cannot PASS when a material lookup surface is silently missing.
    ctx_result = evaluate_prior_work_context(prior_work_context)
    if ctx_result["status"] != "PASS":
        return _verdict(
            "HOLD",
            "blocked_missing_prior_work_context",
            ["bound prior-work context is not PASS"] + ctx_result["reasons"],
        )

    # The declared hash must bind to the exact context that was evaluated.
    declared = playbook.get("prior_work_context_sha256")
    actual = stable_hash(prior_work_context)
    if declared != actual:
        return _verdict(
            "HOLD",
            "blocked_missing_prior_work_context",
            [f"prior_work_context_sha256 hash mismatch: declared {declared}, actual {actual}"],
        )

    # Promotion guard / owner gate: automatic mutation is never allowed. The
    # schema pins this to const false, but we re-check defensively so a builder
    # that skipped schema validation still cannot slip an owner-gate breach past.
    guard = playbook.get("promotion_guard", {})
    if guard.get("automatic_mutation_allowed") is not False:
        return _verdict(
            "HOLD",
            "blocked_owner_gate",
            ["promotion_guard.automatic_mutation_allowed must be false"],
        )

    known_ids = _known_prior_work_ids(prior_work_context)
    dupes = set(ctx_result["duplicate_prior_work_ids"])
    reasons: list[str] = []
    duplicate_breach = False

    for stage in playbook.get("stages", []):
        stage_id = stage.get("stage_id", "<stage>")
        # An activation must never be pre-authorized inside a decision-ready playbook.
        if stage.get("activation_authorized") is not False:
            return _verdict(
                "HOLD",
                "blocked_owner_gate",
                [f"stage {stage_id}: activation_authorized must be false"],
            )
        cited = stage.get("prior_work_ids", [])
        claims = stage.get("claim_ids", [])
        # Every material stage must trace to the evidence bundle and prior work:
        # at least one CLM claim id and at least one PW prior-work id. A stage is
        # material unless it is explicitly marked material=false (e.g. a pure
        # documentation/closeout stage that recommends nothing).
        is_material = stage.get("material", True)
        if is_material:
            if not claims:
                reasons.append(
                    f"stage {stage_id} is material but cites no claim id "
                    "(every material stage must cite >=1 CLM claim id)"
                )
            if not cited:
                reasons.append(
                    f"stage {stage_id} is material but cites no prior-work id "
                    "(every material stage must cite >=1 PW prior-work id)"
                )
        # Every cited prior-work ID must resolve to a real match in the context.
        for pw_id in cited:
            if pw_id not in known_ids:
                reasons.append(f"stage {stage_id} cites unknown prior-work id {pw_id}")
        # A stage may not propose to build prior work already classified duplicate.
        verdict = stage.get("novelty_or_duplicate_verdict")
        for pw_id in cited:
            if pw_id in dupes and verdict in {"new", "replacement", "build"}:
                reasons.append(
                    f"stage {stage_id} proposes to build {pw_id} which is duplicate_do_not_build"
                )
                duplicate_breach = True

    if duplicate_breach:
        return _verdict("HOLD", "blocked_duplicate_prior_work", reasons)
    if reasons:
        return _verdict("HOLD", "blocked_missing_prior_work_context", reasons)

    return _verdict("PASS", "owner_review_ready", [])


# --------------------------------------------------------------------------- #
# Improvement-record evaluator                                                #
# --------------------------------------------------------------------------- #

def _has_observed_evidence(record: dict) -> bool:
    for measure in record.get("observed_measures", []):
        if measure.get("evidence_refs"):
            return True
    return False


def _has_correction_and_preserved_approach(record: dict) -> bool:
    """A refuted result must carry a correction proposal and preserve the approach."""
    proposals = record.get("improvement_proposals", [])
    has_correction = any(
        p.get("destination") != "no_change" and p.get("change")
        for p in proposals
    )
    # The failed prior work must be recorded as contradicted/superseded rather
    # than silently dropped.
    preserved = any(
        e.get("effect") in {"contradicted", "superseded"}
        for e in record.get("prior_work_effects", [])
    )
    return has_correction and preserved


def evaluate_improvement_record(record: dict) -> dict:
    """Validate a ``research-improvement-record-v1`` and enforce honest outcomes."""
    if not isinstance(record, dict):
        return _verdict("FAIL", "blocked_invalid_input", ["improvement record is not an object"])
    errors = _schema_errors(record, IMPROVEMENT_SCHEMA)
    if errors:
        return _verdict("FAIL", "blocked_invalid_input", errors)

    classification = record.get("outcome_classification")
    has_evidence = _has_observed_evidence(record)
    guard = record.get("promotion_guard", {})

    # Owner gate: automatic mutation is never allowed, and an approved promotion
    # must have passed an independent review.
    if guard.get("automatic_mutation_allowed") is not False:
        return _verdict(
            "HOLD",
            "blocked_owner_gate",
            ["promotion_guard.automatic_mutation_allowed must be false"],
        )
    if record.get("promotion_status") == "approved" and guard.get("independent_review_passed") is not True:
        return _verdict(
            "HOLD",
            "blocked_owner_gate",
            ["approved promotion requires independent_review_passed = true"],
        )

    # An implementation with no observed outcome evidence is not_yet_measurable,
    # never improved/confirmed/refuted.
    if classification in EVIDENCE_REQUIRING_OUTCOMES and not has_evidence:
        return _verdict(
            "HOLD",
            "blocked_unverified_improvement",
            [
                f"outcome_classification '{classification}' claims a measured result "
                "but no observed measure carries evidence_refs; must be not_yet_measurable"
            ],
        )

    # A refuted result must create a reviewed correction and preserve the approach.
    if classification == "refuted" and not _has_correction_and_preserved_approach(record):
        return _verdict(
            "HOLD",
            "blocked_unverified_improvement",
            [
                "refuted outcome must carry a correction proposal and preserve the "
                "failed approach (prior_work_effects contradicted/superseded)"
            ],
        )

    if classification == "not_yet_measurable":
        return _verdict("PASS", "outcome_review_due", [])

    return _verdict("PASS", "improvement_reviewed", [])


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def _load_json(path: str | None) -> dict | None:
    if not path:
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate the ISSUE-572 prior-work-aware playbook and improvement loop."
    )
    parser.add_argument("--prior-work-context", required=True, help="Path to prior-work-context-v1 JSON")
    parser.add_argument("--playbook", default="", help="Path to implementation-or-operating-playbook-v1 JSON")
    parser.add_argument("--improvement-record", default="", help="Path to research-improvement-record-v1 JSON")
    parser.add_argument("--out", default="", help="Optional path to write the combined receipt JSON")
    args = parser.parse_args(argv)

    context = _load_json(args.prior_work_context)
    if not isinstance(context, dict):
        raise SystemExit(f"prior-work-context did not parse to a JSON object: {args.prior_work_context}")
    receipt: dict[str, Any] = {
        "schema_version": "stage572-prior-work-playbook-loop-receipt-v1",
        "scope": "offline deterministic evaluation; no live runtime, Linear, export, credential, network, push or merge",
    }

    prior = evaluate_prior_work_context(context)
    receipt["prior_work"] = prior

    if args.playbook:
        playbook = _load_json(args.playbook)
        if not isinstance(playbook, dict):
            raise SystemExit(f"playbook did not parse to a JSON object: {args.playbook}")
        receipt["playbook"] = evaluate_playbook(playbook, context)
    if args.improvement_record:
        record = _load_json(args.improvement_record)
        if not isinstance(record, dict):
            raise SystemExit(f"improvement-record did not parse to a JSON object: {args.improvement_record}")
        receipt["improvement_record"] = evaluate_improvement_record(record)

    evaluated = [receipt[k] for k in ("prior_work", "playbook", "improvement_record") if k in receipt]
    receipt["status"] = "PASS" if all(v["status"] == "PASS" for v in evaluated) else "HOLD_OR_FAIL"

    output = json.dumps(receipt, indent=2) + "\n"
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
    print(output, end="")

    return 0 if receipt["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
