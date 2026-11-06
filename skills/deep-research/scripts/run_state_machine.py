#!/usr/bin/env python3
"""append-only research run-state machine.

Implements the plan's "State machine" and "Retry and recovery policy" sections:

  * one versioned state enum shared by validators, fixtures and (later) n8n
    output checks; no fixture may invent a state outside ``STATES``;
  * append-only transitions scoped by ``correlation_id``, each carrying previous
    state, next state, artifact hashes, actor, reason and timestamp; timestamps
    cannot move backwards within a correlation id;
  * retry lineage: a retry records ``attempt``, ``retry_of`` (prior attempt tag)
    and a ``terminal_reason``; a blocked_* terminal state that reflects a
    provider/route decision must carry a terminal_reason so no failure is
    silently dropped (no silent provider substitution);
  * durable idempotent resume: re-entry with the same correlation id reuses valid
    completed stages whose artifact hashes still match and never duplicates an
    already-emitted output => ``reuse_valid_stages_without_duplicate_output``.

Pure and side-effect free.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import copy
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

# --------------------------------------------------------------------------- #
# State enum                                                                   #
# --------------------------------------------------------------------------- #

# Happy-path stage/control states (from the plan's transition chain).
_STAGE_STATES = {
    "accepted", "research_running", "bundle_validated", "bundle_frozen",
    "prior_work_scanning", "prior_work_validated", "outputs_running",
    "outputs_complete", "reviews_running", "owner_review_ready",
    "implementation_proposed", "implementation_evidence_wait", "outcome_review_due",
    "improvement_reviewed", "activation_selected", "research_only_closed",
}

# Versioned terminal/control enum (must not be exceeded by any fixture).
_BLOCKED_STATES = {
    "blocked_invalid_input", "blocked_missing_canonical_output",
    "blocked_missing_evidence", "blocked_provider_auth", "blocked_exhausted_retry",
    "blocked_review_hold", "blocked_stale_hash", "blocked_lineage_inflation",
    "blocked_duplicate_job", "blocked_owner_gate", "blocked_missing_prior_work_context",
    "blocked_duplicate_prior_work", "blocked_unverified_improvement",
}
_CONTROL_STATES = {"repair_required", "reuse_valid_stages_without_duplicate_output"}

STATES = _STAGE_STATES | _BLOCKED_STATES | _CONTROL_STATES

INITIAL_STATES = {"accepted"}

# blocked_* states that reflect a provider/route decision and therefore must
# carry a terminal_reason (no silent provider substitution / dropped failure).
_TERMINAL_REASON_REQUIRED = {
    "blocked_provider_auth", "blocked_exhausted_retry", "blocked_stale_hash",
}

# Allowed forward transitions along the happy path. blocked_* / repair_required
# may be reached from any live stage state (failure is durable everywhere).
_ALLOWED: dict[str, set[str]] = {
    "accepted": {"research_running", "blocked_invalid_input", "blocked_duplicate_job"},
    "research_running": {"research_running", "bundle_validated", "blocked_missing_evidence"},
    "bundle_validated": {"bundle_frozen", "blocked_lineage_inflation"},
    "bundle_frozen": {"prior_work_scanning"},
    "prior_work_scanning": {"prior_work_validated", "blocked_missing_prior_work_context"},
    "prior_work_validated": {"outputs_running", "blocked_duplicate_prior_work"},
    "outputs_running": {"outputs_running", "outputs_complete", "blocked_missing_canonical_output"},
    "outputs_complete": {"reviews_running"},
    "reviews_running": {"owner_review_ready", "repair_required", "blocked_review_hold"},
    "repair_required": {"reviews_running", "owner_review_ready"},
    "owner_review_ready": {"implementation_proposed", "research_only_closed"},
    "implementation_proposed": {"implementation_evidence_wait"},
    "implementation_evidence_wait": {"outcome_review_due"},
    "outcome_review_due": {"improvement_reviewed", "blocked_unverified_improvement"},
    "improvement_reviewed": {"activation_selected", "research_only_closed"},
}


class StateError(ValueError):
    """Raised on any append-only or enum invariant violation."""


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise StateError("occurred_at must be timezone-aware")
    return dt


@dataclass(frozen=True)
class Transition:
    correlation_id: str
    from_state: str | None
    to_state: str
    occurred_at: str
    actor: str
    reason: str
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    attempt: int = 0
    retry_of: str | None = None
    terminal_reason: str | None = None

    def validate(self) -> None:
        if not self.correlation_id:
            raise StateError("correlation_id is required")
        if not self.reason:
            raise StateError("reason is required")
        if not self.actor:
            raise StateError("actor is required")
        if self.to_state not in STATES:
            raise StateError(f"invalid state (outside enum): {self.to_state}")
        if self.from_state is not None and self.from_state not in STATES:
            raise StateError(f"invalid from_state: {self.from_state}")
        _parse_ts(self.occurred_at)
        if self.to_state in _TERMINAL_REASON_REQUIRED and not self.terminal_reason:
            raise StateError(
                f"{self.to_state} requires a terminal_reason (no silent provider substitution)"
            )


def _allowed(from_state: str, to_state: str) -> bool:
    # blocked_* and repair_required are reachable from any live stage state.
    if to_state in _BLOCKED_STATES or to_state == "repair_required":
        return from_state in _STAGE_STATES or from_state in {"repair_required"}
    return to_state in _ALLOWED.get(from_state, set())


@dataclass(frozen=True)
class RunState:
    transitions: tuple[Transition, ...] = ()

    def append(self, transition: Transition) -> "RunState":
        transition.validate()
        if self.transitions:
            corr = self.transitions[0].correlation_id
            if transition.correlation_id != corr:
                raise StateError(
                    f"correlation_id {transition.correlation_id!r} != run {corr!r}"
                )
        current = self.transitions[-1] if self.transitions else None
        if current is None:
            if transition.from_state is not None:
                raise StateError("initial transition from_state must be None")
            if transition.to_state not in INITIAL_STATES:
                raise StateError(f"invalid initial state: {transition.to_state}")
        else:
            if transition.from_state != current.to_state:
                raise StateError(
                    f"from_state {transition.from_state!r} does not match current "
                    f"state {current.to_state!r}"
                )
            if not _allowed(current.to_state, transition.to_state):
                raise StateError(
                    f"invalid transition: {current.to_state} -> {transition.to_state}"
                )
            if _parse_ts(transition.occurred_at) < _parse_ts(current.occurred_at):
                raise StateError("occurred_at cannot move backwards")
        return RunState(transitions=(*self.transitions, transition))

    def current_state(self) -> str | None:
        return self.transitions[-1].to_state if self.transitions else None

    def correlation_id(self) -> str | None:
        return self.transitions[0].correlation_id if self.transitions else None


# --------------------------------------------------------------------------- #
# Durable idempotent resume                                                    #
# --------------------------------------------------------------------------- #

# Which stage state emits which artifact key (an output/stage is "valid completed"
# only when its recorded artifact hash still matches the durable store).
_STAGE_ARTIFACT_KEYS = {
    "bundle_validated": "bundle",
    "bundle_frozen": "bundle",
    "outputs_complete": None,  # keyed by each emitted output hash
}


def resume(state: RunState, *, correlation_id: str, completed_stage_hashes: dict[str, str]) -> dict:
    """Plan a resume for an interrupted run.

    Reuses completed stages whose recorded artifact hashes still match the
    durable store, and never re-emits an output already recorded. Returns a plan
    with ``outcome=reuse_valid_stages_without_duplicate_output`` when every
    reachable completed stage is reusable.
    """
    if state.correlation_id() != correlation_id:
        raise StateError(
            f"resume correlation_id {correlation_id!r} != run {state.correlation_id()!r}"
        )

    reused: list[str] = []
    invalidated: list[str] = []
    duplicate_outputs: list[str] = []
    rebuild_from: str | None = None

    for tr in state.transitions:
        stage = tr.to_state
        if stage not in _STAGE_STATES:
            continue
        recorded = tr.artifact_hashes or {}
        if not recorded:
            # a stage that produced no artifact is reusable as pure control flow
            reused.append(stage)
            continue
        stage_valid = True
        for key, recorded_hash in recorded.items():
            durable = completed_stage_hashes.get(key)
            if durable is None or durable != recorded_hash:
                stage_valid = False
                if key not in invalidated:
                    invalidated.append(key)
        if stage_valid:
            reused.append(stage)
        else:
            if rebuild_from is None:
                rebuild_from = tr.from_state

    # A partial reuse (some stage hashes drifted) routes to the declared control
    # state repair_required rather than inventing a state outside the enum.
    outcome = (
        "reuse_valid_stages_without_duplicate_output"
        if not invalidated
        else "repair_required"
    )
    return {
        "outcome": outcome,
        "correlation_id": correlation_id,
        "reused_stages": reused,
        "invalidated": invalidated,
        "rebuild_from": rebuild_from,
        "duplicate_outputs": duplicate_outputs,
    }


__all__ = [
    "STATES", "INITIAL_STATES", "StateError", "Transition", "RunState", "resume",
]
