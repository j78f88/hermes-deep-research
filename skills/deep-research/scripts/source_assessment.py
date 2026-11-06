#!/usr/bin/env python3
"""source-quality extension: append-only ``SourceAssessmentEvent``,
trust-tier normalization, disposition compatibility, and a ISSUE-553 round-trip
that projects assessment-driven lifecycle transitions through the EXISTING
8-state ledger graph.

Compatibility contract (from ARCHITECTURE-AND-IMPLEMENTATION-PLAN.md "Source
quality extension" / "Exact compatibility contract with ISSUE-552/ISSUE-553"):

  * do NOT build another source registry and do NOT mutate a historical
    ``SourceEnvelope`` — assessment events are a versioned append-only extension;
  * the existing trust-tier vocabulary has descriptive tiers
    (``tier_0_authoritative`` .. ``tier_3_discovery``) and compact/internal
    fixture labels (``T0`` .. ``T4``, ``internal``, ``fixture``); provide an
    explicit normalization rule rather than silently treating ``T0`` as
    ``tier_0_authoritative``;
  * the ISSUE-553 ledger has the narrower eight-state set WITHOUT ``full_load`` /
    ``partial_load``; assessment events must not pretend these state sets are
    interchangeable;
  * canonicalize ``owner_surface`` to lowercase ``linear://...`` and require it
    for ``accepted`` / ``consumed`` lifecycle transitions;
  * project current source standing from the event history through the existing
    ISSUE-553 transition graph, rejecting backward timestamps, invalid transitions
    and missing accepted/consumed Linear owners.

Uses the bundled source_ledger validators. No external source-intelligence
checkout, configuration, network access or source-registry writes are needed.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import importlib.util
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

# Descriptive trust tiers (canonical) and the compact/internal fixture labels.
DESCRIPTIVE_TIERS = {
    "tier_0_authoritative", "tier_1_primary_structured",
    "tier_2_practitioner_independent", "tier_3_discovery", "excluded",
}
# Explicit compatibility mapping (compact -> descriptive). T4 is deliberately
# mapped to the lowest discovery tier, NOT to authoritative.
_COMPACT_TO_DESCRIPTIVE = {
    "T0": "tier_0_authoritative",
    "T1": "tier_1_primary_structured",
    "T2": "tier_2_practitioner_independent",
    "T3": "tier_3_discovery",
    "T4": "tier_3_discovery",
}
# Labels that are meaningful as-is (non-external-authority contexts).
_PASSTHROUGH_LABELS = {"internal", "fixture", "excluded"}

# The ISSUE-553 ledger's narrower 8-state set (NO full_load / partial_load).
LEDGER_STATES = {
    "candidate", "data_point", "watch", "reject", "accepted", "consumed",
    "expired", "superseded",
}
# The fuller ISSUE-552 disposition set includes the two load states.
DISPOSITION_STATES = LEDGER_STATES | {"full_load", "partial_load"}

_LIFECYCLE_LINEAR_REQUIRED = {"accepted", "consumed"}


class AssessmentError(ValueError):
    """Raised on any assessment/normalization/compatibility violation."""


# --------------------------------------------------------------------------- #
# Trust-tier normalization                                                     #
# --------------------------------------------------------------------------- #

def normalize_trust_tier(value: str) -> str:
    """Explicit normalization: descriptive tiers pass through; compact labels map
    by rule; internal/fixture/excluded pass through; anything else is an error."""
    if not isinstance(value, str) or not value.strip():
        raise AssessmentError("trust_tier is required")
    if value in DESCRIPTIVE_TIERS:
        return value
    if value in _COMPACT_TO_DESCRIPTIVE:
        return _COMPACT_TO_DESCRIPTIVE[value]
    if value in _PASSTHROUGH_LABELS:
        return value
    raise AssessmentError(f"unknown trust_tier label: {value!r}")


# --------------------------------------------------------------------------- #
# SourceAssessmentEvent                                                        #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceAssessmentEvent:
    source_id: str
    source_version: str
    dimension: str
    old_value: Any
    new_value: Any
    evidence_refs: tuple[str, ...] | list[str]
    assessor: str
    method_version: str
    observed_at: str
    reason: str
    # lifecycle_transition marks events that map onto the ISSUE-553 ledger graph.
    lifecycle_transition: bool = False
    owner_surface: str | None = None
    review_date: str | None = None
    expiry_date: str | None = None
    confidence: str | None = None
    campaign_id: str | None = None
    claim_id: str | None = None
    no_action_reason: str | None = None

    def validate(self) -> None:
        for name in ("source_id", "source_version", "dimension", "assessor",
                     "method_version", "reason"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise AssessmentError(f"{name} is required")
        refs = list(self.evidence_refs or [])
        if not refs or any(not isinstance(r, str) or not r.strip() for r in refs):
            raise AssessmentError("at least one evidence reference is required")
        _parse_ts(self.observed_at)

        if self.review_date and self.expiry_date:
            if _parse_date(self.review_date) > _parse_date(self.expiry_date):
                raise AssessmentError("review_date cannot be after expiry_date")

        if self.lifecycle_transition:
            # A ledger lifecycle transition must target a ISSUE-553 ledger state.
            if self.new_value not in LEDGER_STATES:
                raise AssessmentError(
                    f"lifecycle transition new_value {self.new_value!r} is not a "
                    "ISSUE-553 ledger state (disposition states like full_load/"
                    "partial_load are not ledger states)"
                )
            if self.old_value is not None and self.old_value not in LEDGER_STATES:
                raise AssessmentError(
                    f"lifecycle transition old_value {self.old_value!r} is not a "
                    "ISSUE-553 ledger state"
                )
            if self.new_value in _LIFECYCLE_LINEAR_REQUIRED:
                surface = self.canonical_owner_surface()
                if not surface or not surface.startswith("linear://"):
                    raise AssessmentError(
                        f"{self.new_value} lifecycle transition requires a linear:// owner surface"
                    )

    def canonical_owner_surface(self) -> str | None:
        if not self.owner_surface:
            return None
        return self.owner_surface.strip().lower()

    def as_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "dimension": self.dimension,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "evidence_refs": list(self.evidence_refs or []),
            "assessor": self.assessor,
            "method_version": self.method_version,
            "observed_at": self.observed_at,
            "reason": self.reason,
            "lifecycle_transition": self.lifecycle_transition,
            "owner_surface": self.canonical_owner_surface(),
            "review_date": self.review_date,
            "expiry_date": self.expiry_date,
            "confidence": self.confidence,
            "campaign_id": self.campaign_id,
            "claim_id": self.claim_id,
            "no_action_reason": self.no_action_reason,
        }


def _parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise AssessmentError("observed_at must be timezone-aware")
    return dt


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


# --------------------------------------------------------------------------- #
# Projection (source-level, without the ledger)                                #
# --------------------------------------------------------------------------- #

def project_source_standing(events: Iterable[SourceAssessmentEvent]) -> dict[str, Any]:
    """Project current per-dimension standing from event history.

    Preserves original envelopes: this reads events only and never mutates them.
    Claim-level support is intentionally NOT derived here (never promote/demote a
    specific claim solely from the source-level profile).
    """
    standing: dict[str, Any] = {}
    for ev in events:
        ev.validate()
        standing[ev.dimension] = {
            "new_value": ev.new_value,
            "observed_at": ev.observed_at,
            "assessor": ev.assessor,
            "evidence_refs": list(ev.evidence_refs or []),
        }
    return {"dimensions": standing}


# --------------------------------------------------------------------------- #
# ISSUE-553 round-trip                                                           #
# --------------------------------------------------------------------------- #

def _load_source_ledger():
    """Load the bundled sibling even when this module is loaded by file path."""
    mod_path = Path(__file__).resolve().with_name("source_ledger.py")
    cached = sys.modules.get("source_ledger")
    if cached is not None and getattr(cached, "__file__", None) == str(mod_path):
        return cached
    spec = importlib.util.spec_from_file_location("source_ledger", mod_path)
    if spec is None or spec.loader is None:
        raise AssessmentError("bundled source_ledger module cannot be loaded")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["source_ledger"] = mod
    spec.loader.exec_module(mod)
    return mod


def project_through_ledger(
    events: Iterable[SourceAssessmentEvent], *, case_id: str,
    ledger_path: Path | None = None,
) -> dict:
    """Project lifecycle events through the local eight-state ledger graph.

    Rejects backward timestamps, invalid transitions and missing owners. The
    former ``ledger_path`` keyword is accepted for call compatibility only:
    it is never opened or executed. The bundled implementation is always used.
    """
    ledger = _load_source_ledger()

    ledger_transitions = []
    for ev in events:
        ev.validate()
        if not ev.lifecycle_transition:
            continue
        occurred = _parse_ts(ev.observed_at)
        kwargs: dict[str, Any] = dict(
            case_id=case_id,
            from_state=ev.old_value,
            to_state=ev.new_value,
            occurred_at=occurred,
            evidence_refs=tuple(ev.evidence_refs or ()),
            reason=ev.reason,
        )
        surface = ev.canonical_owner_surface()
        if surface:
            kwargs["owner_surface"] = surface
        if ev.no_action_reason:
            kwargs["no_action_reason"] = ev.no_action_reason
        if ev.review_date:
            kwargs["review_date"] = _parse_date(ev.review_date)
        if ev.expiry_date:
            kwargs["expiry_date"] = _parse_date(ev.expiry_date)
        if ev.new_value in {"expired", "superseded", "consumed"}:
            kwargs.setdefault("retirement_rule", ev.reason)
        if ev.new_value == "superseded":
            kwargs.setdefault("superseded_by", f"{case_id}#superseded")
        ledger_transitions.append(ledger.LedgerTransition(**kwargs))

    # project_current_state performs the full validation + graph checks.
    return ledger.project_current_state(ledger_transitions)


__all__ = [
    "DESCRIPTIVE_TIERS", "LEDGER_STATES", "DISPOSITION_STATES",
    "AssessmentError", "SourceAssessmentEvent", "normalize_trust_tier",
    "project_source_standing", "project_through_ledger",
]
