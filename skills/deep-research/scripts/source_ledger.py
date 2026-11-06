"""Local append-only source lifecycle ledger for offline assessments.

This pure projection performs no network or external writes. It preserves the
narrow eight-state lifecycle contract; load dispositions are not ledger states.
An owner URI is a structural requirement, not proof of a live issue's existence.
"""
from __future__ import annotations

# Retain the actual executed module for checker attestation.
__checker_module_code__ = __import__("sys")._getframe().f_code

from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Iterable, Mapping

STATES = {
    "candidate", "data_point", "watch", "reject", "accepted", "consumed",
    "expired", "superseded",
}
INITIAL_STATES = {"candidate", "data_point", "watch", "reject"}
TERMINAL_STATES = {"consumed", "expired", "superseded", "reject"}
ALLOWED_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "candidate": frozenset({"data_point", "watch", "reject", "accepted", "expired", "superseded"}),
    "data_point": frozenset({"candidate", "watch", "reject", "expired", "superseded"}),
    "watch": frozenset({"candidate", "data_point", "reject", "expired", "superseded"}),
    "accepted": frozenset({"consumed", "superseded"}),
    "reject": frozenset(),
    "consumed": frozenset(),
    "expired": frozenset(),
    "superseded": frozenset(),
}


def _text(name: str, value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True)
class LedgerTransition:
    """One immutable fact in the source lifecycle."""

    case_id: str
    from_state: str | None
    to_state: str
    occurred_at: datetime
    evidence_refs: tuple[str, ...]
    reason: str
    trigger: str | None = None
    review_date: date | None = None
    expiry_date: date | None = None
    retirement_rule: str | None = None
    no_action_reason: str | None = None
    owner_surface: str | None = None
    superseded_by: str | None = None

    @property
    def event_id(self) -> str:
        material = "\x1f".join((
            self.case_id, self.from_state or "", self.to_state,
            self.occurred_at.isoformat(), self.reason, *self.evidence_refs,
        ))
        return sha256(material.encode("utf-8")).hexdigest()

    def validate(self) -> None:
        _text("case_id", self.case_id)
        _text("transition reason", self.reason)
        if self.to_state not in STATES:
            raise ValueError(f"invalid ledger state: {self.to_state}")
        if self.from_state is not None and self.from_state not in STATES:
            raise ValueError(f"invalid from_state: {self.from_state}")
        if not isinstance(self.occurred_at, datetime) or self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be a timezone-aware datetime")
        if not self.evidence_refs or any(not isinstance(ref, str) or not ref.strip() for ref in self.evidence_refs):
            raise ValueError("at least one evidence reference is required")
        if self.to_state == "watch" and not all((self.trigger, self.review_date, self.expiry_date, self.retirement_rule)):
            raise ValueError("watch transition requires trigger, review_date, expiry_date, and retirement_rule")
        if self.to_state in INITIAL_STATES and not self.no_action_reason:
            raise ValueError(f"{self.to_state} transition requires no_action_reason")
        if self.to_state in {"expired", "superseded", "consumed"} and not self.retirement_rule:
            raise ValueError(f"{self.to_state} transition requires retirement_rule")
        if self.to_state == "superseded" and not self.superseded_by:
            raise ValueError("superseded transition requires superseded_by")
        if self.to_state in {"accepted", "consumed"}:
            if not self.owner_surface or not self.owner_surface.lower().startswith("linear://"):
                raise ValueError("accepted execution requires an existing Linear owner surface")
        if self.review_date and self.expiry_date and self.review_date > self.expiry_date:
            raise ValueError("review_date cannot be after expiry_date")


@dataclass(frozen=True)
class LedgerProjection:
    case_id: str
    state: str
    occurred_at: datetime
    evidence_refs: tuple[str, ...]
    reason: str
    trigger: str | None
    review_date: date | None
    expiry_date: date | None
    retirement_rule: str | None
    no_action_reason: str | None
    owner_surface: str | None
    superseded_by: str | None
    history: tuple[LedgerTransition, ...]
    review_due: bool
    expired: bool

    @property
    def transition_count(self) -> int:
        return len(self.history)


def project_current_state(
    events: Iterable[LedgerTransition], *, as_of: date | None = None,
) -> dict[str, LedgerProjection]:
    """Fold events in ledger order; timestamps may tie but not go backwards per case."""
    histories: dict[str, list[LedgerTransition]] = {}
    event_ids: set[str] = set()
    for event in events:
        event.validate()
        if event.event_id in event_ids:
            raise ValueError(f"duplicate transition: {event.event_id}")
        event_ids.add(event.event_id)
        history = histories.setdefault(event.case_id, [])
        current = history[-1] if history else None
        if current is None:
            if event.from_state is not None:
                raise ValueError("initial transition from_state must be None")
            if event.to_state not in INITIAL_STATES:
                raise ValueError(f"invalid transition: initial -> {event.to_state}")
        else:
            if event.from_state != current.to_state:
                raise ValueError(
                    f"from_state {event.from_state!r} does not match current state {current.to_state!r}"
                )
            if event.to_state not in ALLOWED_TRANSITIONS[current.to_state]:
                raise ValueError(f"invalid transition: {current.to_state} -> {event.to_state}")
            if event.occurred_at < current.occurred_at:
                raise ValueError("transition occurred_at cannot move backwards")
        history.append(event)

    today = as_of or date.today()
    projected: dict[str, LedgerProjection] = {}
    for case_id, history in histories.items():
        current = history[-1]
        projected[case_id] = LedgerProjection(
            case_id=case_id, state=current.to_state, occurred_at=current.occurred_at,
            evidence_refs=current.evidence_refs, reason=current.reason,
            trigger=current.trigger, review_date=current.review_date,
            expiry_date=current.expiry_date, retirement_rule=current.retirement_rule,
            no_action_reason=current.no_action_reason, owner_surface=current.owner_surface,
            superseded_by=current.superseded_by, history=tuple(history),
            review_due=bool(current.review_date and today >= current.review_date),
            expired=bool(current.expiry_date and today > current.expiry_date),
        )
    return projected


def append_transition(
    events: Iterable[LedgerTransition], transition: LedgerTransition,
) -> tuple[LedgerTransition, ...]:
    """Return a new validated event tuple, leaving the input untouched."""
    updated = (*events, transition)
    project_current_state(updated)
    return updated


@dataclass(frozen=True)
class SignalLedger:
    events: tuple[LedgerTransition, ...] = ()

    def append(self, transition: LedgerTransition) -> SignalLedger:
        return SignalLedger(append_transition(self.events, transition))

    def current(self, *, as_of: date | None = None) -> dict[str, LedgerProjection]:
        return project_current_state(self.events, as_of=as_of)


__all__ = [
    "STATES", "ALLOWED_TRANSITIONS", "INITIAL_STATES", "TERMINAL_STATES",
    "LedgerProjection", "LedgerTransition", "SignalLedger", "append_transition",
    "project_current_state",
]
