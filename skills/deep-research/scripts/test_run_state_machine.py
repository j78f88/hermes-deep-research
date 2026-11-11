#!/usr/bin/env python3
"""RED-first tests for the append-only research run-state machine.

Covers the versioned state enum, append-only transitions (no backward
timestamps, correlation-id scoped, artifact-hash bound), retry lineage, and
durable idempotent resume producing ``reuse_valid_stages_without_duplicate_output``.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent


def _load(name: str):
    import sys

    spec = importlib.util.spec_from_file_location(name, _HERE / f"{name}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses introspection needs the module registered
    spec.loader.exec_module(mod)
    return mod


rs = _load("run_state_machine")


def _t(**kw):
    base = dict(
        correlation_id="corr-1",
        from_state=None,
        to_state="accepted",
        occurred_at="2026-09-11T00:00:00+00:00",
        actor="controller",
        reason="mission accepted",
        artifact_hashes={},
    )
    base.update(kw)
    return rs.Transition(**base)


# --- enum integrity ----------------------------------------------------------

def test_state_enum_contains_all_named_blocked_and_resume_states():
    for s in (
        "blocked_invalid_input", "blocked_missing_canonical_output",
        "blocked_missing_evidence", "blocked_provider_auth", "blocked_exhausted_retry",
        "blocked_review_hold", "blocked_stale_hash", "blocked_lineage_inflation",
        "blocked_duplicate_job", "blocked_owner_gate", "blocked_missing_prior_work_context",
        "blocked_duplicate_prior_work", "blocked_unverified_improvement",
        "repair_required", "reuse_valid_stages_without_duplicate_output",
        "prior_work_scanning", "prior_work_validated", "owner_review_ready",
        "implementation_proposed", "implementation_evidence_wait", "outcome_review_due",
        "improvement_reviewed", "research_only_closed",
    ):
        assert s in rs.STATES


def test_fixture_cannot_invent_a_state_outside_the_enum():
    with pytest.raises(rs.StateError):
        rs.RunState().append(_t(to_state="totally_made_up_state"))


# --- append-only invariants --------------------------------------------------

def test_initial_transition_requires_none_from_state():
    with pytest.raises(rs.StateError):
        rs.RunState().append(_t(from_state="accepted", to_state="research_running"))


def test_transition_from_state_must_match_current():
    st = rs.RunState().append(_t())
    with pytest.raises(rs.StateError):
        st.append(_t(from_state="research_running", to_state="bundle_validated",
                     occurred_at="2026-09-11T01:00:00+00:00"))


def test_timestamps_cannot_move_backwards():
    st = rs.RunState().append(_t())
    st = st.append(_t(from_state="accepted", to_state="research_running",
                      occurred_at="2026-09-11T02:00:00+00:00"))
    with pytest.raises(rs.StateError):
        st.append(_t(from_state="research_running", to_state="bundle_validated",
                     occurred_at="2026-09-11T01:00:00+00:00"))


def test_append_does_not_mutate_prior_state():
    st0 = rs.RunState().append(_t())
    n_before = len(st0.transitions)
    st0.append(_t(from_state="accepted", to_state="research_running",
                  occurred_at="2026-09-11T02:00:00+00:00"))
    assert len(st0.transitions) == n_before


def test_current_state_reflects_last_transition():
    st = rs.RunState().append(_t())
    st = st.append(_t(from_state="accepted", to_state="research_running",
                      occurred_at="2026-09-11T02:00:00+00:00"))
    assert st.current_state() == "research_running"


# --- retry lineage -----------------------------------------------------------

def test_retry_transition_records_retry_of_prior_attempt():
    st = rs.RunState().append(_t())
    st = st.append(_t(from_state="accepted", to_state="research_running",
                      occurred_at="2026-09-11T02:00:00+00:00", attempt=1))
    # a provider 429 -> bounded retry: a new attempt referencing the prior one
    st = st.append(_t(from_state="research_running", to_state="research_running",
                      occurred_at="2026-09-11T03:00:00+00:00", attempt=2,
                      retry_of="research_running#1", terminal_reason="http_429"))
    last = st.transitions[-1]
    assert last.attempt == 2
    assert last.retry_of == "research_running#1"
    assert last.terminal_reason == "http_429"


def test_no_silent_provider_substitution_requires_terminal_reason_on_blocked_auth():
    st = rs.RunState().append(_t())
    st = st.append(_t(from_state="accepted", to_state="research_running",
                      occurred_at="2026-09-11T02:00:00+00:00"))
    with pytest.raises(rs.StateError):
        # blocked_provider_auth without a recorded terminal_reason is a silent drop
        st.append(_t(from_state="research_running", to_state="blocked_provider_auth",
                     occurred_at="2026-09-11T03:00:00+00:00"))


# --- durable idempotent resume ----------------------------------------------

def _completed_run():
    st = rs.RunState().append(_t())
    st = st.append(_t(from_state="accepted", to_state="research_running",
                      occurred_at="2026-09-11T02:00:00+00:00"))
    st = st.append(_t(from_state="research_running", to_state="bundle_validated",
                     occurred_at="2026-09-11T03:00:00+00:00",
                     artifact_hashes={"bundle": "a" * 64}))
    st = st.append(_t(from_state="bundle_validated", to_state="bundle_frozen",
                     occurred_at="2026-09-11T04:00:00+00:00",
                     artifact_hashes={"bundle": "a" * 64}))
    return st


def test_resume_reuses_valid_completed_stages_without_duplicate_output():
    st = _completed_run()
    plan = rs.resume(
        st,
        correlation_id="corr-1",
        completed_stage_hashes={"bundle": "a" * 64},
    )
    assert plan["outcome"] == "reuse_valid_stages_without_duplicate_output"
    assert "bundle_frozen" in plan["reused_stages"]
    assert plan["duplicate_outputs"] == []


def test_resume_rejects_correlation_id_mismatch():
    st = _completed_run()
    with pytest.raises(rs.StateError):
        rs.resume(st, correlation_id="other-corr", completed_stage_hashes={})


def test_resume_invalidates_stage_when_hash_drifts():
    st = _completed_run()
    plan = rs.resume(
        st,
        correlation_id="corr-1",
        completed_stage_hashes={"bundle": "b" * 64},  # drifted
    )
    # drifted hash means the frozen bundle stage cannot be reused
    assert "bundle_frozen" not in plan["reused_stages"]
    assert plan["rebuild_from"] == "bundle_validated" or "bundle" in plan["invalidated"]


def test_resume_never_duplicates_an_already_emitted_output():
    st = _completed_run()
    st = st.append(_t(from_state="bundle_frozen", to_state="prior_work_scanning",
                     occurred_at="2026-09-11T04:30:00+00:00"))
    st = st.append(_t(from_state="prior_work_scanning", to_state="prior_work_validated",
                     occurred_at="2026-09-11T04:45:00+00:00"))
    st = st.append(_t(from_state="prior_work_validated", to_state="outputs_running",
                     occurred_at="2026-09-11T05:00:00+00:00"))
    st = st.append(_t(from_state="outputs_running", to_state="outputs_complete",
                     occurred_at="2026-09-11T06:00:00+00:00",
                     artifact_hashes={"exec_brief": "c" * 64}))
    plan = rs.resume(
        st,
        correlation_id="corr-1",
        completed_stage_hashes={"bundle": "a" * 64, "exec_brief": "c" * 64},
    )
    assert plan["outcome"] == "reuse_valid_stages_without_duplicate_output"
    assert "outputs_complete" in plan["reused_stages"]
    assert plan["duplicate_outputs"] == []
