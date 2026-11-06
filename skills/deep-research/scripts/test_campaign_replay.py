#!/usr/bin/env python3
"""RED-first tests for the ISSUE-569 fourteen-fixture behavioral replay matrix.

The matrix evaluates a *structured campaign state* and returns a terminal/blocked
state from the versioned enum. The critical property under test: verdicts are
driven by STRUCTURAL fault injection, never by the fixture's label/description.
Two controls prove this:

  1. every fixture's expected state is produced from its injected structure;
  2. a label-scramble control shuffles the human labels while preserving the
     injected structure, and the verdicts are unchanged.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import sys
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


cr = _load("campaign_replay")


# The 14 named fixtures and the state each must produce (from the plan).
EXPECTED = {
    "canonical_full_pack_has_placeholder": "blocked_missing_canonical_output",
    "missing_executive_brief": "blocked_missing_canonical_output",
    "output_from_different_bundle_hash": "blocked_stale_hash",
    "source_evidence_missing": "blocked_missing_evidence",
    "reviewer_returns_hold": "blocked_review_hold",
    "provider_401_403": "blocked_provider_auth",
    "provider_timeout_partial_artifact": "repair_required",
    "same_lineage_false_independence": "blocked_lineage_inflation",
    "interrupted_run_resumes": "reuse_valid_stages_without_duplicate_output",
    "activation_unapproved_write": "blocked_owner_gate",
    "playbook_without_prior_work": "blocked_missing_prior_work_context",
    "playbook_duplicate_capability": "blocked_duplicate_prior_work",
    "improved_without_observed_evidence": "blocked_unverified_improvement",
    "improvement_unvalidated_auto_mutation": "blocked_owner_gate",
}


def test_matrix_enumerates_exactly_fourteen_named_fixtures():
    fixtures = cr.build_fixture_matrix()
    assert len(fixtures) == 14
    assert set(f["name"] for f in fixtures) == set(EXPECTED)


@pytest.mark.parametrize("fixture_name", sorted(EXPECTED))
def test_each_fixture_produces_its_expected_state_from_structure(fixture_name):
    fixtures = {f["name"]: f for f in cr.build_fixture_matrix()}
    fx = fixtures[fixture_name]
    result = cr.evaluate_fixture(fx)
    assert result["state"] == EXPECTED[fixture_name], (
        f"{fixture_name}: expected {EXPECTED[fixture_name]} got {result['state']} "
        f"({result.get('reasons')})"
    )


def test_labels_do_not_drive_verdicts_scramble_control():
    """Scramble every fixture's human label/description while preserving its
    injected structure. Verdicts must be identical."""
    fixtures = cr.build_fixture_matrix()
    baseline = {f["name"]: cr.evaluate_fixture(f)["state"] for f in fixtures}

    # Rotate labels/descriptions across fixtures (structure untouched).
    scrambled = copy.deepcopy(fixtures)
    names = [f["name"] for f in scrambled]
    descs = [f.get("description", "") for f in scrambled]
    for i, f in enumerate(scrambled):
        f["description"] = descs[(i + 7) % len(descs)]  # wrong description
        f["label"] = names[(i + 3) % len(names)]        # wrong label

    for f in scrambled:
        # evaluate by structure; the wrong label/description must not matter
        result = cr.evaluate_fixture(f)
        assert result["state"] == baseline[f["name"]], (
            f"{f['name']}: verdict changed when label was scrambled "
            f"({baseline[f['name']]} -> {result['state']}) — labels leaked into the classifier"
        )


def test_placeholder_fixture_is_not_detected_by_keyword_only():
    """A canonical output whose bytes are fluent but structurally incomplete
    (placeholder sentinel) must be caught even without the word 'TBD' in the
    fixture label."""
    fixtures = {f["name"]: f for f in cr.build_fixture_matrix()}
    fx = copy.deepcopy(fixtures["canonical_full_pack_has_placeholder"])
    fx["label"] = "totally_fine_output"
    fx["description"] = "everything is complete and wonderful"
    result = cr.evaluate_fixture(fx)
    assert result["state"] == "blocked_missing_canonical_output"


def test_positive_fixture_reaches_owner_review_ready():
    """A structurally clean committee campaign reaches owner_review_ready — proving
    the matrix is not just failing everything."""
    fx = cr.build_positive_fixture()
    result = cr.evaluate_fixture(fx)
    assert result["state"] == "owner_review_ready", result.get("reasons")


def test_all_produced_states_are_within_the_enum():
    import run_state_machine as rsm  # already loaded into sys.modules by campaign_replay
    fixtures = cr.build_fixture_matrix()
    for f in fixtures:
        result = cr.evaluate_fixture(f)
        assert result["state"] in rsm.STATES
