#!/usr/bin/env python3
"""RED-first tests for ISSUE-569 SourceAssessmentEvent + trust-tier normalization
+ local ledger round-trip/history compatibility.

The round-trip tests exercise the bundled eight-state ledger, rejecting backward
timestamps, invalid transitions and missing accepted/consumed Linear owners
without an external checkout or mutating historical source envelopes.
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sa = _load("source_assessment")



# --- trust-tier normalization -----------------------------------------------

def test_normalize_descriptive_trust_tiers_are_preserved():
    assert sa.normalize_trust_tier("tier_0_authoritative") == "tier_0_authoritative"
    assert sa.normalize_trust_tier("tier_3_discovery") == "tier_3_discovery"


def test_normalize_compact_labels_map_to_descriptive_tiers():
    assert sa.normalize_trust_tier("T0") == "tier_0_authoritative"
    assert sa.normalize_trust_tier("T1") == "tier_1_primary_structured"
    assert sa.normalize_trust_tier("T2") == "tier_2_practitioner_independent"
    assert sa.normalize_trust_tier("T3") == "tier_3_discovery"


def test_normalize_T4_and_internal_and_fixture_are_explicit_not_authoritative():
    # T4 must not silently become tier_0; it is a discovery-or-lower / non-authoritative label.
    assert sa.normalize_trust_tier("T4") == "tier_3_discovery"
    assert sa.normalize_trust_tier("internal") == "internal"
    assert sa.normalize_trust_tier("fixture") == "fixture"


def test_normalize_never_silently_treats_T0_as_authoritative_without_rule():
    # explicit compatibility rule, not a silent equality
    assert sa.normalize_trust_tier("T0") == "tier_0_authoritative"
    with pytest.raises(sa.AssessmentError):
        sa.normalize_trust_tier("not_a_tier")


# --- disposition compatibility (do NOT conflate the two state sets) ----------

def test_ledger_states_exclude_full_and_partial_load():
    assert "full_load" not in sa.LEDGER_STATES
    assert "partial_load" not in sa.LEDGER_STATES
    assert "full_load" in sa.DISPOSITION_STATES
    assert "partial_load" in sa.DISPOSITION_STATES


def test_assessment_event_rejects_ledger_transition_to_full_load():
    # full_load is a disposition, not a ledger lifecycle state; must not be a ledger transition.
    with pytest.raises(sa.AssessmentError):
        sa.SourceAssessmentEvent(
            source_id="s1", source_version="v1",
            dimension="usefulness", old_value="watch", new_value="full_load",
            evidence_refs=["e1"], assessor="qa", method_version="m1",
            observed_at="2026-09-11T00:00:00+00:00", reason="downstream use",
            lifecycle_transition=True,
        ).validate()


# --- SourceAssessmentEvent invariants ---------------------------------------

def _event(**kw):
    base = dict(
        source_id="s1", source_version="v1",
        dimension="freshness", old_value="fresh", new_value="aging",
        evidence_refs=["e1"], assessor="evidence_qa", method_version="m1",
        observed_at="2026-09-11T00:00:00+00:00", reason="volatility",
    )
    base.update(kw)
    return sa.SourceAssessmentEvent(**base)


def test_accepted_transition_requires_linear_owner_surface():
    with pytest.raises(sa.AssessmentError):
        _event(dimension="usefulness", old_value="candidate", new_value="accepted",
               lifecycle_transition=True).validate()
    # with a linear owner it validates
    _event(dimension="usefulness", old_value="candidate", new_value="accepted",
           lifecycle_transition=True, owner_surface="linear://ISSUE-569").validate()


def test_owner_surface_is_canonicalized_to_lowercase_linear():
    ev = _event(dimension="usefulness", old_value="candidate", new_value="accepted",
                lifecycle_transition=True, owner_surface="LINEAR://ISSUE-569")
    ev.validate()
    assert ev.canonical_owner_surface() == "linear://issue-569"


def test_event_requires_evidence_and_rejects_backward_review_date():
    with pytest.raises(sa.AssessmentError):
        _event(evidence_refs=[]).validate()
    with pytest.raises(sa.AssessmentError):
        _event(review_date="2026-09-20", expiry_date="2026-09-10").validate()


# --- ISSUE-553 round-trip ------------------------------------------------------

def test_assessment_events_round_trip_through_local_ledger_graph():
    """Assessment-driven lifecycle transitions must project through the existing
    ISSUE-553 8-state ledger graph unchanged."""
    events = [
        _event(dimension="usefulness", old_value=None, new_value="candidate",
               lifecycle_transition=True, reason="new signal",
               no_action_reason="await corroboration"),
        _event(dimension="usefulness", old_value="candidate", new_value="accepted",
               lifecycle_transition=True, owner_surface="linear://ISSUE-569",
               observed_at="2026-09-11T02:00:00+00:00", reason="downstream decision use"),
    ]
    projection = sa.project_through_ledger(events, case_id="src-s1")
    assert projection["src-s1"].state == "accepted"
    assert projection["src-s1"].owner_surface.lower().startswith("linear://")


def test_round_trip_rejects_backward_timestamp_through_ledger():
    events = [
        _event(dimension="usefulness", old_value=None, new_value="candidate",
               lifecycle_transition=True, observed_at="2026-09-11T02:00:00+00:00",
               reason="new", no_action_reason="await"),
        _event(dimension="usefulness", old_value="candidate", new_value="accepted",
               lifecycle_transition=True, owner_surface="linear://ISSUE-569",
               observed_at="2026-09-11T01:00:00+00:00", reason="use"),
    ]
    with pytest.raises(ValueError, match="cannot move backwards"):
        sa.project_through_ledger(events, case_id="src-s1")


def test_round_trip_rejects_invalid_transition_through_ledger():
    events = [
        _event(dimension="usefulness", old_value=None, new_value="reject",
               lifecycle_transition=True, reason="spam", no_action_reason="off-topic"),
        _event(dimension="usefulness", old_value="reject", new_value="accepted",
               lifecycle_transition=True, owner_surface="linear://ISSUE-569",
               observed_at="2026-09-11T02:00:00+00:00", reason="use"),
    ]
    with pytest.raises(ValueError, match="invalid transition"):
        sa.project_through_ledger(events, case_id="src-s1")


def test_round_trip_rejects_accepted_without_linear_owner_through_ledger():
    events = [
        _event(dimension="usefulness", old_value=None, new_value="candidate",
               lifecycle_transition=True, reason="new", no_action_reason="await"),
        _event(dimension="usefulness", old_value="candidate", new_value="accepted",
               lifecycle_transition=True, observed_at="2026-09-11T02:00:00+00:00",
               reason="use"),  # no owner_surface
    ]
    with pytest.raises(sa.AssessmentError, match="linear:// owner surface"):
        sa.project_through_ledger(events, case_id="src-s1")


def test_projection_does_not_mutate_input_events():
    events = [_event()]
    snapshot = [e.as_dict() for e in events]
    sa.project_source_standing(events)
    assert [e.as_dict() for e in events] == snapshot


@pytest.fixture
def ledger():
    return _load("source_ledger")


def _transition(ledger, **kwargs):
    fields = dict(
        case_id="source-1", from_state=None, to_state="candidate",
        occurred_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
        evidence_refs=("evidence/a.md",), reason="assessment",
        no_action_reason="await corroboration", trigger="new evidence",
        review_date=date(2026, 9, 12), expiry_date=date(2026, 9, 20),
        retirement_rule="retire old evidence", superseded_by="source-2",
        owner_surface="linear://issue-569",
    )
    fields.update(kwargs)
    return ledger.LedgerTransition(**fields)


# Independent contract oracle: do not derive expectations from implementation.
_GRAPH = {
    "candidate": {"data_point", "watch", "reject", "accepted", "expired", "superseded"},
    "data_point": {"candidate", "watch", "reject", "expired", "superseded"},
    "watch": {"candidate", "data_point", "reject", "expired", "superseded"},
    "accepted": {"consumed", "superseded"},
    "reject": set(), "consumed": set(), "expired": set(), "superseded": set(),
}


def _history_to(ledger, state):
    if state in {"candidate", "data_point", "watch", "reject"}:
        return (_transition(ledger, to_state=state),)
    history = (_transition(ledger),)
    if state == "consumed":
        history += (_transition(ledger, from_state="candidate", to_state="accepted"),)
        return history + (_transition(ledger, from_state="accepted", to_state=state),)
    return history + (_transition(ledger, from_state="candidate", to_state=state),)


@pytest.mark.parametrize("initial", sorted(_GRAPH))
def test_ledger_initial_state_contract(ledger, initial):
    event = _transition(ledger, to_state=initial)
    if initial in {"candidate", "data_point", "watch", "reject"}:
        assert ledger.project_current_state([event])[event.case_id].state == initial
    else:
        with pytest.raises(ValueError, match="invalid transition: initial"):
            ledger.project_current_state([event])


@pytest.mark.parametrize("before", sorted(_GRAPH))
@pytest.mark.parametrize("after", sorted(_GRAPH))
def test_ledger_full_transition_graph(ledger, before, after):
    history = _history_to(ledger, before)
    event = _transition(ledger, from_state=before, to_state=after,
                        occurred_at=datetime(2026, 9, 11, 1, tzinfo=timezone.utc))
    if after in _GRAPH[before]:
        row = ledger.project_current_state((*history, event))[event.case_id]
        assert row.state == after
        assert row.history == (*history, event)
    else:
        with pytest.raises(ValueError, match="invalid transition"):
            ledger.project_current_state((*history, event))


@pytest.mark.parametrize(("changes", "message"), [
    ({"case_id": " "}, "case_id is required"),
    ({"reason": ""}, "transition reason is required"),
    ({"to_state": "full_load"}, "invalid ledger state"),
    ({"from_state": "partial_load"}, "invalid from_state"),
    ({"occurred_at": datetime(2026, 9, 11)}, "timezone-aware"),
    ({"evidence_refs": ()}, "evidence reference"),
    ({"evidence_refs": (" ",)}, "evidence reference"),
    ({"no_action_reason": None}, "no_action_reason"),
    ({"to_state": "watch", "trigger": None}, "watch transition requires"),
    ({"to_state": "watch", "review_date": None}, "watch transition requires"),
    ({"to_state": "watch", "expiry_date": None}, "watch transition requires"),
    ({"to_state": "watch", "retirement_rule": None}, "watch transition requires"),
    ({"to_state": "consumed", "retirement_rule": None}, "retirement_rule"),
    ({"to_state": "expired", "retirement_rule": None}, "retirement_rule"),
    ({"to_state": "superseded", "superseded_by": None}, "superseded_by"),
    ({"to_state": "accepted", "owner_surface": None}, "Linear owner surface"),
    ({"to_state": "consumed", "owner_surface": "https://example.org"}, "Linear owner surface"),
    ({"review_date": date(2026, 9, 21)}, "review_date cannot be after"),
])
def test_ledger_transition_validation(ledger, changes, message):
    with pytest.raises(ValueError, match=message):
        _transition(ledger, **changes).validate()


def test_ledger_timestamp_and_case_order(ledger):
    first = _transition(ledger)
    tied = _transition(ledger, from_state="candidate", to_state="accepted")
    assert ledger.project_current_state([first, tied])[first.case_id].state == "accepted"
    backwards = replace(tied, occurred_at=datetime(2026, 9, 10, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="cannot move backwards"):
        ledger.project_current_state([first, backwards])
    other_case = replace(first, case_id="source-2", occurred_at=backwards.occurred_at)
    assert len(ledger.project_current_state([first, other_case])) == 2


def test_ledger_rejects_duplicate_and_mismatched_history(ledger):
    first = _transition(ledger)
    with pytest.raises(ValueError, match="duplicate transition"):
        ledger.project_current_state([first, first])
    mismatch = _transition(ledger, from_state="watch", to_state="candidate")
    with pytest.raises(ValueError, match="does not match current state"):
        ledger.project_current_state([first, mismatch])
    with pytest.raises(ValueError, match="initial transition from_state must be None"):
        ledger.project_current_state([mismatch])


def test_ledger_append_is_immutable_and_review_flags_are_date_bound(ledger):
    empty = ledger.SignalLedger()
    event = _transition(ledger, to_state="watch", owner_surface="LINEAR://ISSUE-569")
    updated = empty.append(event)
    assert empty.events == ()
    assert updated.events == (event,)
    assert ledger.append_transition((), event) == (event,)
    before = updated.current(as_of=date(2026, 9, 11))[event.case_id]
    due = updated.current(as_of=date(2026, 9, 12))[event.case_id]
    expiry = updated.current(as_of=date(2026, 9, 20))[event.case_id]
    expired = updated.current(as_of=date(2026, 9, 21))[event.case_id]
    assert not before.review_due and not before.expired
    assert due.review_due and not due.expired
    assert not expiry.expired and expired.expired
    assert due.transition_count == 1 and due.history == (event,)
    with pytest.raises(FrozenInstanceError):
        event.reason = "changed"
    with pytest.raises(FrozenInstanceError):
        due.state = "accepted"


def test_local_projection_never_imports_an_external_checkout(tmp_path, monkeypatch):
    external = tmp_path / "external" / "source_intelligence"
    external.mkdir(parents=True)
    (external / "stage553.py").write_text("raise AssertionError('external module executed')\n")
    monkeypatch.setenv("SOURCE_INTELLIGENCE_ROOT", str(external.parent))
    events = [_event(dimension="usefulness", old_value=None, new_value="candidate",
                     lifecycle_transition=True, no_action_reason="await")]
    snapshot = [event.as_dict() for event in events]
    assert sa.project_through_ledger(events, case_id="src-s1")["src-s1"].state == "candidate"
    # The former path keyword remains a no-I/O compatibility argument only.
    assert sa.project_through_ledger(events, case_id="src-s1",
                                     ledger_path=external.parent)["src-s1"].state == "candidate"
    assert [event.as_dict() for event in events] == snapshot
