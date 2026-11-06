from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from reader_test_support import refresh_fixture_acceptance_bindings, semantic_review_fields
from publication_test_support import run_frozen_legacy_cli, run_frozen_legacy_export

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
INIT = SCRIPT_DIR / "init_campaign.py"
VERIFY = SCRIPT_DIR / "verify_campaign_depth.py"
EXPORT = SCRIPT_DIR / "export_deliverables.py"


def load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_campaign_depth", VERIFY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_export_module():
    spec = importlib.util.spec_from_file_location("export_deliverables", EXPORT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
    if check and proc.returncode != 0:
        raise AssertionError(proc.stdout)
    return proc


def prepare_test_export(export: Path) -> None:
    """The flat destination needs no pre-existing directory or metadata files."""
    assert not export.is_symlink()


def test_stage603_init_campaign_scaffolds_preseed_hold_gate(tmp_path: Path) -> None:
    root = tmp_path / "campaign"

    run([
        sys.executable,
        str(INIT),
        "--target",
        "Seed Vendor",
        "--target-type",
        "product",
        "--primary-source",
        "https://example.com/seed",
        "--decision-question",
        "Which workload outcome should we satisfy?",
        "--root",
        str(root),
    ], check=True)

    manifest = json.loads((root / "manifest.json").read_text())
    pre_seed = manifest["pre_seed"]
    assert pre_seed["status"] == "HOLD"
    assert pre_seed["comparison_lanes_authorized"] is False
    assert pre_seed["target_specific_lanes_authorized"] is False
    assert sorted(pre_seed["artifacts"]) == [
        "candidate_universe",
        "comparison_charter",
        "need_map",
        "source_characterization",
    ]
    for artifact in pre_seed["artifacts"].values():
        assert (root / artifact["json"]).exists()
        assert (root / artifact["markdown"]).exists()
        assert artifact["sha256"]
    source = json.loads((root / "preseed" / "source-characterization-v1.json").read_text())
    assert source["characterization"]["role"] == "user_supplied_seed"
    assert source["characterization"]["not_incumbent_default_preferred_or_priority"] is True


def test_stage603_scaffolded_true_deep_research_holds_on_preseed_gate(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    run([sys.executable, str(INIT), "--target", "Seed Vendor", "--target-type", "product", "--root", str(root)], check=True)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["metrics"]["pre_seed_present"] is True
    assert payload["metrics"]["pre_seed_status"] == "HOLD"
    assert any("pre_seed_candidate_classes_missing" in hold for hold in payload["holds"])
    assert any("pre_seed_comparison_charter_unfrozen" in hold for hold in payload["holds"])


def test_stage603_verifier_fails_closed_on_seed_defaulting_and_decision_collapse(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    from preseed import scaffold_preseed

    pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["pre_seed"] = pre_seed
    manifest["target_specific_lanes_authorized"] = True
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    source_path = root / "preseed" / "source-characterization-v1.json"
    source = json.loads(source_path.read_text())
    source["characterization"]["not_incumbent_default_preferred_or_priority"] = False
    source_path.write_text(json.dumps(source), encoding="utf-8")
    charter_path = root / "preseed" / "comparison-charter-v1.json"
    charter = json.loads(charter_path.read_text())
    charter["decision_states"] = {"pilot_selection": "seed-vendor", "activation_status": "active"}
    charter_path.write_text(json.dumps(charter), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_seed_defaulting" in hold for hold in holds)
    assert any("pre_seed_decision_state_collapse" in hold for hold in holds)
    assert any("pre_seed_pilot_selection_implies_activation" in hold for hold in holds)


def test_stage603_verifier_detects_evidence_asymmetry_unsupported_duration_and_gate_violating_winner(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    from preseed import scaffold_preseed

    pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["pre_seed"] = pre_seed
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    universe_path = root / "preseed" / "candidate-universe-v1.json"
    universe = json.loads(universe_path.read_text())
    universe["candidates"] = [
        {"id": "seed", "name": "Seed Vendor", "class": "product", "source_status": "user_supplied_seed", "discovery_provenance": [{"type": "user_supplied"}], "evidence": {"security": ["S1"]}, "hard_gate_results": {"security": False}},
        {"id": "process", "name": "Process change", "class": "process", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search"}], "evidence": {}},
        {"id": "architecture", "name": "Architecture pattern", "class": "architecture", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search"}], "evidence": {}},
        {"id": "do-nothing", "name": "Current state", "class": "current_state_do_nothing", "source_status": "discovered", "discovery_provenance": [{"type": "current_state"}], "evidence": {}},
        {"id": "adjacent", "name": "Adjacent category", "class": "adjacent_category", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search"}], "evidence": {}},
    ]
    universe_path.write_text(json.dumps(universe), encoding="utf-8")
    charter_path = root / "preseed" / "comparison-charter-v1.json"
    charter = json.loads(charter_path.read_text())
    charter.update({
        "frozen": True,
        "evidence_floor": {"dimensions": ["security"]},
        "evidence_budget": {"seed": {"source_count": 5}, "process": {"source_count": 1}},
        "source_classes": ["official", "independent"],
        "rubric": [{"criterion": "fit", "weight": 1, "scale": "1-5", "definition": "Fit quality"}],
        "blinded_aliases": {c["id"]: f"A{i}" for i, c in enumerate(universe["candidates"])},
        "alias_order": [f"A{i}" for i in range(len(universe["candidates"]))],
        "unblind_mapping": {f"A{i}": c["id"] for i, c in enumerate(universe["candidates"])},
        "duration_provenance": [{"duration": "90 days", "provenance": "evidence", "evidence_refs": []}],
        "decision_states": {"eligible_set": ["seed"], "benchmark_winner": "seed", "mission_selection": None, "pilot_selection": None, "activation_status": "blocked_pending_owner_approval"},
        "winner": "seed",
    })
    charter_path.write_text(json.dumps(charter), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_evidence_asymmetry" in hold for hold in holds)
    assert any("pre_seed_duration_unsupported_as_evidence" in hold for hold in holds)
    assert any("pre_seed_hard_gate_violating_winner" in hold for hold in holds)


def test_stage603_permutation_digest_ignores_candidate_names_and_order(tmp_path: Path) -> None:
    from preseed import permutation_invariant_digest

    candidates = [
        {"id": "a", "name": "Seed Vendor", "class": "product", "source_status": "user_supplied_seed", "evidence": {"security": ["S1"]}, "hard_gate_results": {"security": True}, "score": 7},
        {"id": "b", "name": "Other", "class": "process", "source_status": "discovered", "evidence": {"security": ["S2"]}, "hard_gate_results": {"security": True}, "score": 7},
    ]
    charter = {"rubric": [{"criterion": "security", "weight": 1, "scale": "1-5", "definition": "Security quality"}], "hard_eligibility_gates": ["security"]}
    first = permutation_invariant_digest({"candidates": candidates}, charter)
    permuted = [dict(candidates[1], name="Renamed Other"), dict(candidates[0], name="Renamed Seed")]

    assert permutation_invariant_digest({"candidates": permuted}, charter) == first


def test_stage603_current_manifest_with_null_preseed_does_not_use_legacy_bypass(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["pre_seed"] = None
    manifest["target_specific_lanes_authorized"] = True
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("pre_seed_manifest_invalid" in hold for hold in payload["holds"])


def test_stage603_verifier_rejects_empty_frozen_charter_and_missing_winner_gates(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    from preseed import scaffold_preseed

    pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["pre_seed"] = pre_seed
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    universe_path = root / "preseed" / "candidate-universe-v1.json"
    candidates = [
        {"id": "seed", "name": "Seed Vendor", "class": "product", "source_status": "user_supplied_seed", "discovery_provenance": [{"type": "user_supplied"}], "evidence": {}},
        {"id": "process", "name": "Process change", "class": "process", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S1"}], "evidence": {}},
        {"id": "architecture", "name": "Architecture pattern", "class": "architecture", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S2"}], "evidence": {}},
        {"id": "do-nothing", "name": "Current state", "class": "current_state_do_nothing", "source_status": "discovered", "discovery_provenance": [{"type": "current_state", "source_ref": "S3"}], "evidence": {}},
        {"id": "adjacent", "name": "Adjacent category", "class": "adjacent_category", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S4"}], "evidence": {}},
    ]
    universe_path.write_text(json.dumps({"schema_version": "candidate-universe-v1", "status": "PASS", "candidates": candidates}), encoding="utf-8")
    charter_path = root / "preseed" / "comparison-charter-v1.json"
    charter = json.loads(charter_path.read_text())
    charter.update({
        "status": "PASS",
        "frozen": True,
        "hard_eligibility_gates": [],
        "evidence_floor": {},
        "evidence_budget": {},
        "source_classes": [],
        "rubric": [],
        "blinded_aliases": {c["id"]: f"A{i}" for i, c in enumerate(candidates)},
        "alias_order": [f"A{i}" for i in range(len(candidates))],
        "unblind_mapping": {f"A{i}": c["id"] for i, c in enumerate(candidates)},
        "duration_provenance": [],
        "decision_states": {"eligible_set": ["seed"], "benchmark_winner": "seed", "mission_selection": None, "pilot_selection": None, "activation_status": "blocked_pending_owner_approval"},
        "winner": "missing-candidate",
    })
    charter_path.write_text(json.dumps(charter), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_hard_eligibility_gates_empty" in hold for hold in holds)
    assert any("pre_seed_rubric_empty" in hold for hold in holds)
    assert any("pre_seed_winner_not_in_candidate_universe" in hold for hold in holds)


def test_stage603_verifier_treats_nonboolean_failed_winner_gate_as_hard_gate_violation(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    from preseed import scaffold_preseed

    pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["pre_seed"] = pre_seed
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    candidates = [
        {"id": "seed", "name": "Seed Vendor", "class": "product", "source_status": "user_supplied_seed", "discovery_provenance": [{"type": "user_supplied", "ref": "https://example.com"}], "evidence": {"security": ["S1"]}, "hard_gate_results": {"security": "FAIL"}},
        {"id": "process", "name": "Process change", "class": "process", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S1"}], "evidence": {"security": ["S2"]}},
        {"id": "architecture", "name": "Architecture pattern", "class": "architecture", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S2"}], "evidence": {"security": ["S3"]}},
        {"id": "do-nothing", "name": "Current state", "class": "current_state_do_nothing", "source_status": "discovered", "discovery_provenance": [{"type": "current_state", "source_ref": "S3"}], "evidence": {"security": ["S4"]}},
        {"id": "adjacent", "name": "Adjacent category", "class": "adjacent_category", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S4"}], "evidence": {"security": ["S5"]}},
    ]
    (root / "preseed" / "candidate-universe-v1.json").write_text(json.dumps({"schema_version": "candidate-universe-v1", "status": "PASS", "candidates": candidates}), encoding="utf-8")
    charter = {
        "schema_version": "comparison-charter-v1",
        "status": "PASS",
        "frozen": True,
        "hard_eligibility_gates": ["security"],
        "evidence_floor": {"dimensions": ["security"]},
        "evidence_budget": {c["id"]: {"source_count": 2} for c in candidates},
        "source_classes": ["official", "independent"],
        "rubric": [{"criterion": "security", "weight": 1, "scale": "1-5", "definition": "Security quality"}],
        "blinded_aliases": {c["id"]: f"A{i}" for i, c in enumerate(candidates)},
        "alias_order": [f"A{i}" for i in range(len(candidates))],
        "unblind_mapping": {f"A{i}": c["id"] for i, c in enumerate(candidates)},
        "duration_provenance": [{"duration": "30 days", "provenance": "heuristic"}],
        "decision_states": {"eligible_set": ["process"], "benchmark_winner": "seed", "mission_selection": "process", "pilot_selection": None, "activation_status": "blocked_pending_owner_approval"},
        "winner": "seed",
    }
    (root / "preseed" / "comparison-charter-v1.json").write_text(json.dumps(charter), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert any("pre_seed_hard_gate_violating_winner" in hold for hold in json.loads(proc.stdout)["holds"])


def test_stage603_top_level_lane_authorization_flags_are_guarded(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    from preseed import scaffold_preseed

    pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["pre_seed"] = pre_seed
    manifest["comparison_lanes_authorized"] = True
    manifest["target_specific_lanes_authorized"] = True
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_top_level_comparison_lanes_authorized_with_holds" in hold for hold in holds)
    assert any("pre_seed_top_level_target_specific_lanes_authorized_with_holds" in hold for hold in holds)



def passing_preseed_payloads():
    candidates = [
        {"id": "seed", "name": "Seed Vendor", "class": "product", "source_status": "user_supplied_seed", "discovery_provenance": [{"type": "user_supplied", "ref": "https://example.com"}], "evidence": {"security": ["S1"], "operations": ["S2"]}, "hard_gate_results": {"security": True, "operations": "PASS"}},
        {"id": "process", "name": "Process change", "class": "process", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S3"}], "evidence": {"security": ["S3"], "operations": ["S4"]}, "hard_gate_results": {"security": "PASSED", "operations": True}},
        {"id": "architecture", "name": "Architecture pattern", "class": "architecture", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S5"}], "evidence": {"security": ["S5"], "operations": ["S6"]}, "hard_gate_results": {"security": True, "operations": True}},
        {"id": "do-nothing", "name": "Current state", "class": "current_state_do_nothing", "source_status": "discovered", "discovery_provenance": [{"type": "current_state", "source_ref": "S7"}], "evidence": {"security": ["S7"], "operations": ["S8"]}, "hard_gate_results": {"security": True, "operations": True}},
        {"id": "adjacent", "name": "Adjacent category", "class": "adjacent_category", "source_status": "discovered", "discovery_provenance": [{"type": "independent_search", "source_ref": "S9"}], "evidence": {"security": ["S9"], "operations": ["S10"]}, "hard_gate_results": {"security": True, "operations": True}},
    ]
    aliases = {candidate["id"]: f"Alias {index}" for index, candidate in enumerate(candidates, start=1)}
    universe = {"schema_version": "candidate-universe-v1", "status": "PASS", "candidates": candidates}
    charter = {
        "schema_version": "comparison-charter-v1",
        "status": "PASS",
        "frozen": True,
        "hard_eligibility_gates": ["security", "operations"],
        "evidence_floor": {"dimensions": ["security", "operations"], "minimum_sources_per_dimension": 1},
        "evidence_budget": {candidate["id"]: {"normalized_allocation": 1, "source_count": 2} for candidate in candidates},
        "source_classes": ["official", "independent"],
        "rubric": [{"criterion": "security", "weight": 1, "scale": "1-5", "definition": "Security quality"}],
        "blinded_aliases": aliases,
        "alias_order": list(aliases.values()),
        "unblind_mapping": {alias: candidate_id for candidate_id, alias in aliases.items()},
        "duration_provenance": [{"duration": "30 days", "provenance": "heuristic"}],
        "decision_states": {"eligible_set": [candidate["id"] for candidate in candidates], "benchmark_winner": "process", "mission_selection": None, "pilot_selection": None, "activation_status": "blocked_pending_owner_approval"},
        "winner": "process",
    }
    return universe, charter


def attach_preseed(root: Path, universe: dict, charter: dict, *, need_overrides: dict | None = None, pre_overrides: dict | None = None) -> None:
    from preseed import scaffold_preseed

    pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
    manifest = json.loads((root / "manifest.json").read_text())
    if pre_overrides:
        pre_seed.update(pre_overrides)
    else:
        pre_seed.update({"status": "PASS", "gate_status": "PASS"})
    manifest["pre_seed"] = pre_seed
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    source_path = root / "preseed" / "source-characterization-v1.json"
    source = json.loads(source_path.read_text())
    source["status"] = "PASS"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    need_path = root / "preseed" / "need-map-v1.json"
    need = json.loads(need_path.read_text())
    need.update({"status": "PASS", "workloads": ["Intake triage workload"], "outcomes": ["Reduce manual review time"], "constraints": ["No production mutation"], "solution_selected": False, "selection_language_forbidden": True})
    if need_overrides:
        need.update(need_overrides)
    need_path.write_text(json.dumps(need), encoding="utf-8")
    (root / "preseed" / "candidate-universe-v1.json").write_text(json.dumps(universe), encoding="utf-8")
    (root / "preseed" / "comparison-charter-v1.json").write_text(json.dumps(charter), encoding="utf-8")
    refresh_preseed_hashes(root)


def refresh_preseed_hashes(root: Path) -> None:
    manifest_path = root / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for meta in manifest["pre_seed"]["artifacts"].values():
        meta["sha256"] = __import__("hashlib").sha256((root / meta["json"]).read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    refresh_fixture_acceptance_bindings(root)


def test_stage603_preseed_artifact_payload_statuses_must_explicitly_pass_for_gate_pass(tmp_path: Path) -> None:
    invalid_statuses = [
        ("source_characterization", "HOLD"),
        ("need_map", "complete"),
        ("candidate_universe", True),
        ("comparison_charter", None),
    ]
    for key, status in invalid_statuses:
        root = tmp_path / key
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        attach_preseed(root, universe, charter)
        manifest = json.loads((root / "manifest.json").read_text())
        artifact_rel = manifest["pre_seed"]["artifacts"][key]["json"]
        artifact_path = root / artifact_rel
        payload = json.loads(artifact_path.read_text())
        if status is None:
            payload.pop("status", None)
        else:
            payload["status"] = status
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        refresh_preseed_hashes(root)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (key, status, proc.stdout)
        holds = json.loads(proc.stdout)["holds"]
        assert any(f"pre_seed_{key}_artifact_status_invalid" in hold for hold in holds), (key, status, holds)


def test_stage603_preseed_artifact_payload_status_accepts_pass_casing_and_passed(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(root, universe, charter)
    manifest = json.loads((root / "manifest.json").read_text())
    statuses = {
        "source_characterization": "PASS",
        "need_map": "pass",
        "candidate_universe": "PASSED",
        "comparison_charter": "passed",
    }
    for key, status in statuses.items():
        artifact_path = root / manifest["pre_seed"]["artifacts"][key]["json"]
        payload = json.loads(artifact_path.read_text())
        payload["status"] = status
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
    refresh_preseed_hashes(root)

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0, proc.stdout


def test_stage603_preseed_manifest_sha256_is_required_valid_hex_and_exact_readback(tmp_path: Path) -> None:
    cases = [
        ("missing", None, "pre_seed_source_characterization_manifest_sha256_missing"),
        ("short", "abc123", "pre_seed_source_characterization_manifest_sha256_malformed"),
        ("nonhex", "g" * 64, "pre_seed_source_characterization_manifest_sha256_malformed"),
        ("stale", "0" * 64, "pre_seed_source_characterization_manifest_sha256_mismatch"),
    ]
    for label, sha_value, expected_hold in cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        attach_preseed(root, universe, charter)
        manifest = json.loads((root / "manifest.json").read_text())
        meta = manifest["pre_seed"]["artifacts"]["source_characterization"]
        if sha_value is None:
            meta.pop("sha256", None)
        else:
            meta["sha256"] = sha_value
        manifest["pre_seed"].update({"status": "PASS", "gate_status": "PASS"})
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        holds = json.loads(proc.stdout)["holds"]
        assert any(expected_hold in hold for hold in holds), (label, holds)


def test_stage603_preseed_manifest_sha256_must_cover_every_required_artifact(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(root, universe, charter)
    manifest = json.loads((root / "manifest.json").read_text())
    for key in manifest["pre_seed"]["artifacts"]:
        manifest["pre_seed"]["artifacts"][key]["sha256"] = "1" * 64
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    for key in ["source_characterization", "need_map", "candidate_universe", "comparison_charter"]:
        assert any(f"pre_seed_{key}_manifest_sha256_mismatch" in hold for hold in holds), holds

def test_stage603_current_preseed_holds_apply_in_non_true_modes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    from preseed import scaffold_preseed

    pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["pre_seed"] = pre_seed
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "brief", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["metrics"]["pre_seed_present"] is True
    assert any("pre_seed_comparison_charter_unfrozen" in hold for hold in payload["holds"])


def test_stage603_legacy_manifest_without_preseed_key_still_bypasses_preseed_in_brief(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False, include_preseed=False)

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "brief", "--json")

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["metrics"]["pre_seed_present"] is False


def test_stage603_true_deep_research_requires_preseed_manifest_key(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False, include_preseed=False)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["status"] == "HOLD"
    assert payload["metrics"]["pre_seed_present"] is False
    assert any("pre_seed_required_for_true_deep_research" in hold for hold in payload["holds"])


def test_stage603_blinded_controls_must_be_exact_bijection(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    charter["blinded_aliases"] = {"seed": "Alias 1", "process": "Alias 1", "architecture": "TBD", "unknown": "Alias X", "adjacent": "Alias 5"}
    charter["alias_order"] = ["Alias 1", "Alias 1", "Alias 5", "Alias X"]
    charter["unblind_mapping"] = {"Alias 1": "process", "Alias 5": "adjacent", "Alias X": "unknown"}
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_blinded_alias_keys_mismatch" in hold for hold in holds)
    assert any("pre_seed_blinded_alias_values_invalid" in hold for hold in holds)
    assert any("pre_seed_alias_order_invalid" in hold for hold in holds)
    assert any("pre_seed_unblind_mapping_invalid" in hold for hold in holds)


def test_stage603_blinded_controls_fail_closed_on_non_scalar_alias_surface_without_traceback(tmp_path: Path) -> None:
    malformed_cases = [
        (
            "candidate_id_dict",
            lambda universe, charter: universe["candidates"][0].update({"id": {"candidate": "seed"}}),
            "pre_seed_candidate_id_invalid",
        ),
        (
            "candidate_id_bool",
            lambda universe, charter: universe["candidates"][0].update({"id": True}),
            "pre_seed_candidate_id_invalid",
        ),
        (
            "alias_key_placeholder",
            lambda universe, charter: charter["blinded_aliases"].update({"TBD": "Alias X"}),
            "pre_seed_blinded_alias_keys_mismatch",
        ),
        (
            "alias_value_bool",
            lambda universe, charter: charter["blinded_aliases"].update({"seed": True}),
            "pre_seed_blinded_alias_values_invalid",
        ),
        (
            "alias_value_number",
            lambda universe, charter: charter["blinded_aliases"].update({"seed": 1}),
            "pre_seed_blinded_alias_values_invalid",
        ),
        (
            "alias_value_null",
            lambda universe, charter: charter["blinded_aliases"].update({"seed": None}),
            "pre_seed_blinded_alias_values_invalid",
        ),
        (
            "alias_value_list",
            lambda universe, charter: charter["blinded_aliases"].update({"seed": ["Alias 1"]}),
            "pre_seed_blinded_alias_values_invalid",
        ),
        (
            "alias_value_dict",
            lambda universe, charter: charter["blinded_aliases"].update({"seed": {"alias": "Alias 1"}}),
            "pre_seed_blinded_alias_values_invalid",
        ),
        (
            "alias_order_list",
            lambda universe, charter: charter["alias_order"].__setitem__(0, ["Alias 1"]),
            "pre_seed_alias_order_invalid",
        ),
        (
            "alias_order_dict",
            lambda universe, charter: charter["alias_order"].__setitem__(0, {"alias": "Alias 1"}),
            "pre_seed_alias_order_invalid",
        ),
        (
            "unblind_key_placeholder",
            lambda universe, charter: charter["unblind_mapping"].update({"TBD": "seed"}),
            "pre_seed_unblind_mapping_invalid",
        ),
        (
            "unblind_value_bool",
            lambda universe, charter: charter["unblind_mapping"].update({"Alias 1": True}),
            "pre_seed_unblind_mapping_invalid",
        ),
        (
            "unblind_value_number",
            lambda universe, charter: charter["unblind_mapping"].update({"Alias 1": 1}),
            "pre_seed_unblind_mapping_invalid",
        ),
        (
            "unblind_value_null",
            lambda universe, charter: charter["unblind_mapping"].update({"Alias 1": None}),
            "pre_seed_unblind_mapping_invalid",
        ),
        (
            "unblind_value_list",
            lambda universe, charter: charter["unblind_mapping"].update({"Alias 1": ["seed"]}),
            "pre_seed_unblind_mapping_invalid",
        ),
        (
            "unblind_value_dict",
            lambda universe, charter: charter["unblind_mapping"].update({"Alias 1": {"id": "seed"}}),
            "pre_seed_unblind_mapping_invalid",
        ),
    ]
    for label, mutate, expected_hold in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        mutate(universe, charter)
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, label
        assert "Traceback" not in proc.stdout
        assert "TypeError" not in proc.stdout
        assert any(expected_hold in hold for hold in json.loads(proc.stdout)["holds"]), label


def test_stage603_evidence_budget_exact_equal_and_non_placeholder_floor(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    charter["evidence_budget"] = {"seed": {"source_count": 2}, "process": {"source_count": 2}, "extra": {"source_count": 2}}
    charter["evidence_floor"] = {"dimensions": ["security", "TBD"]}
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_evidence_budget_candidate_mismatch" in hold for hold in holds)
    assert any("pre_seed_evidence_floor_placeholder" in hold for hold in holds)


def test_stage603_winner_gate_results_fail_closed_on_unknown_values(tmp_path: Path) -> None:
    for value in ["UNKNOWN", "pending", None, {"status": "PASS"}]:
        root = tmp_path / str(value).replace("/", "_")
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        universe["candidates"][1]["hard_gate_results"]["security"] = value
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2
        assert any("pre_seed_hard_gate_violating_winner" in hold for hold in json.loads(proc.stdout)["holds"]), value


def test_stage603_need_map_rejects_selection_flags_fields_and_recommendation_language(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(
        root,
        universe,
        charter,
        need_overrides={
            "selection_language_forbidden": False,
            "preferred_solution": "Seed Vendor",
            "workloads": ["Run onboarding workload, not a recommended vendor list"],
            "outcomes": ["Select Seed Vendor for rollout"],
            "constraints": ["Default to incumbent platform"],
        },
    )

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_need_map_selection_language_not_forbidden" in hold for hold in holds)
    assert any("pre_seed_need_map_forbidden_selection_fields" in hold for hold in holds)
    assert any("pre_seed_need_map_selection_language" in hold for hold in holds)


def test_stage603_neutral_need_map_mentions_recommendation_workload_without_selecting_solution(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(root, universe, charter, need_overrides={"workloads": ["Team writes recommendation memos"], "outcomes": ["Decision quality improves"], "constraints": ["No selected solution before evidence gate"]})

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0


def test_stage603_init_fails_closed_when_preseed_import_unavailable(tmp_path: Path) -> None:
    isolated_script = tmp_path / "init_campaign.py"
    isolated_script.write_text(INIT.read_text(encoding="utf-8"), encoding="utf-8")
    root = tmp_path / "campaign"

    proc = run([sys.executable, str(isolated_script), "--target", "Seed Vendor", "--target-type", "product", "--root", str(root)])

    assert proc.returncode != 0
    assert "preseed scaffold unavailable" in proc.stdout
    assert not (root / "manifest.json").exists()


def test_stage603_manifest_declared_preseed_status_must_match_computed_verdict(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(root, universe, charter, pre_overrides={"status": "HOLD", "gate_status": "HOLD"})

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_manifest_status_mismatch" in hold for hold in holds)
    assert any("pre_seed_manifest_gate_status_mismatch" in hold for hold in holds)


def test_stage603_decision_states_schema_rejects_malformed_values_without_traceback(tmp_path: Path) -> None:
    malformed_cases = [
        ("eligible_empty", {"eligible_set": []}, "pre_seed_decision_states_eligible_set_invalid"),
        ("eligible_mapping", {"eligible_set": {"id": "seed"}}, "pre_seed_decision_states_eligible_set_invalid"),
        ("eligible_unknown", {"eligible_set": ["unknown"]}, "pre_seed_decision_states_unknown_id"),
        ("eligible_bool", {"eligible_set": [True]}, "pre_seed_decision_states_eligible_set_invalid"),
        ("benchmark_mapping", {"benchmark_winner": {"id": "process"}}, "pre_seed_decision_states_benchmark_winner_invalid"),
        ("benchmark_unknown", {"benchmark_winner": "unknown"}, "pre_seed_decision_states_unknown_id"),
        ("mission_list", {"mission_selection": ["process"]}, "pre_seed_decision_states_mission_selection_invalid"),
        ("pilot_bool", {"pilot_selection": True}, "pre_seed_decision_states_pilot_selection_invalid"),
        ("activation_mapping", {"activation_status": {"state": "blocked"}}, "pre_seed_decision_states_activation_status_invalid"),
        ("activation_active", {"activation_status": "active"}, "pre_seed_pilot_selection_implies_activation"),
    ]
    for label, overrides, expected_hold in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        charter["decision_states"].update(overrides)
        if label == "activation_active":
            charter["decision_states"]["pilot_selection"] = "process"
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any(expected_hold in hold for hold in holds), (label, holds)


def test_stage603_decision_states_schema_accepts_null_optional_selections_and_safe_blocked_status(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    charter["decision_states"] = {
        "eligible_set": [candidate["id"] for candidate in universe["candidates"]],
        "benchmark_winner": None,
        "mission_selection": None,
        "pilot_selection": None,
        "activation_status": "not_started",
    }
    charter["winner"] = "process"
    attach_preseed(root, universe, charter)

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0, proc.stdout


def test_stage603_need_map_rejects_direct_named_recommendation_outcomes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(
        root,
        universe,
        charter,
        need_overrides={"outcomes": ["Recommend Seed Vendor for rollout"]},
    )

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_need_map_selection_language outcomes" in hold for hold in holds)


def test_stage603_need_map_rejects_direct_pick_choose_and_use_selection_language(tmp_path: Path) -> None:
    cases = [
        ("pick", "outcomes", "Pick Seed Vendor for rollout"),
        ("choose", "workloads", "Choose Seed Vendor for rollout"),
        ("use", "constraints", "Use Seed Vendor as the platform"),
        ("adopt", "outcomes", "Adopt Seed Vendor architecture"),
        ("recommended", "workloads", "Recommended tool is Seed Vendor"),
    ]
    for label, field, phrase in cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        attach_preseed(root, universe, charter, need_overrides={field: [phrase]})

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, phrase
        holds = json.loads(proc.stdout)["holds"]
        assert any(f"pre_seed_need_map_selection_language {field}" in hold for hold in holds), phrase


def test_stage603_need_map_allows_neutral_pickup_use_and_default_mentions(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(
        root,
        universe,
        charter,
        need_overrides={
            "workloads": ["Pick up intake records from the queue", "Use case inventory before evidence gate"],
            "outcomes": ["Recommended evidence format is documented", "Default operating hours are captured"],
            "constraints": ["No selected solution before evidence gate"],
        },
    )

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0


def test_stage603_need_map_rejects_selected_vendor_and_related_selection_field_family(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(
        root,
        universe,
        charter,
        need_overrides={
            "selected_vendor": "Seed Vendor",
            "chosenVendor": "Seed Vendor",
            "recommend-product": "Seed Vendor",
            "selection_architecture": "Seed Architecture",
            "choose_option": "Seed Vendor",
            "preferred_product": "Seed Vendor",
            "default_platform": "Incumbent Stack",
            "incumbentApproach": "Current Stack",
            "adopt_tool": "Seed Vendor",
            "use_candidate": "Seed Vendor",
        },
    )

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    forbidden_hold = next(hold for hold in holds if "pre_seed_need_map_forbidden_selection_fields" in hold)
    for field in [
        "selected_vendor",
        "chosenVendor",
        "recommend-product",
        "selection_architecture",
        "choose_option",
        "preferred_product",
        "default_platform",
        "incumbentApproach",
        "adopt_tool",
        "use_candidate",
    ]:
        assert field in forbidden_hold


def test_stage603_need_map_rejects_decision_noun_morphology_selection_fields(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    forbidden_fields = {
        "recommendationVendor": "Seed Vendor",
        "recommendations_solution": "Seed Vendor",
        "recommending-platform": "Seed Vendor",
        "preferenceTool": "Seed Vendor",
        "preferences_option": "Seed Vendor",
        "preferring-approach": "Seed Vendor",
        "adoptionProduct": "Seed Vendor",
        "adoptions_candidate": "Seed Vendor",
        "adopting-architecture": "Seed Architecture",
        "defaultingVendor": "Incumbent Stack",
        "defaults_platform": "Incumbent Stack",
        "incumbencySolution": "Current Stack",
        "incumbencies_tool": "Current Stack",
        "choiceCandidate": "Seed Vendor",
        "choices-approach": "Seed Vendor",
        "choosing_option": "Seed Vendor",
    }
    attach_preseed(root, universe, charter, need_overrides=forbidden_fields)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    forbidden_hold = next(hold for hold in holds if "pre_seed_need_map_forbidden_selection_fields" in hold)
    for field in forbidden_fields:
        assert field in forbidden_hold


def test_stage603_need_map_rejects_acronym_camel_selection_fields(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    forbidden_fields = {
        "recommendationURLVendor": "Seed Vendor",
        "preferenceAPIPlatform": "Seed Vendor",
        "choiceSSOTool": "Seed Vendor",
    }
    attach_preseed(root, universe, charter, need_overrides=forbidden_fields)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    forbidden_hold = next(hold for hold in holds if "pre_seed_need_map_forbidden_selection_fields" in hold)
    for field in forbidden_fields:
        assert field in forbidden_hold


def test_stage603_need_map_rejects_nested_selection_key_paths(tmp_path: Path) -> None:
    cases = [
        ("selected_vendor", {"outcomes": [{"selected": {"vendor": "Seed Vendor"}}]}),
        ("chosen_vendor_camel", {"workloads": [{"chosen": {"vendorName": "Seed Vendor"}}]}),
        ("recommend_product_kebab", {"constraints": [{"recommend": {"product-name": "Seed Vendor"}}]}),
        ("default_platform_deep", {"outcomes": [{"decision": {"default": {"platform": "Seed Vendor"}}}]}),
    ]
    for label, need_overrides in cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        attach_preseed(root, universe, charter, need_overrides=need_overrides)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, label
        holds = json.loads(proc.stdout)["holds"]
        assert any("pre_seed_need_map_selection_language" in hold for hold in holds), (label, holds)


def test_stage603_need_map_allows_nested_neutral_selection_control_structures(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(
        root,
        universe,
        charter,
        need_overrides={
            "outcomes": [{"selected": {"deadline": "Friday"}}],
            "workloads": [{"triage": {"vendorSurvey": "collect neutral inputs"}}],
            "constraints": [{"before_selection": {"candidate_inventory": "must be complete"}}],
        },
    )

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0, proc.stdout


def test_stage603_need_map_allows_decision_morphology_without_solution_object(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    attach_preseed(
        root,
        universe,
        charter,
        need_overrides={
            "recommendationDeadline": "Friday",
            "preferenceSurvey": "Run before scoring",
            "adoptionReadiness": "Measured as an outcome",
            "defaultOperatingHours": "Business hours",
            "incumbencyNotes": "Current ownership context",
            "choiceCriteria": "Evidence completeness",
            "recommendationURLDeadline": "Friday",
            "preferenceAPISurvey": "Run before scoring",
            "choiceSSOCriteria": "Evidence completeness",
        },
    )

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0


def test_stage603_malformed_candidate_scalars_return_structured_hold_not_traceback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    universe["candidates"][0]["score"] = {"value": 7}
    universe["candidates"][1]["score"] = None
    universe["candidates"][2]["class"] = {"kind": "architecture"}
    universe["candidates"][3]["source_status"] = ["discovered"]
    universe["candidates"][4]["id"] = ["adjacent"]
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert "Traceback" not in proc.stdout
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_candidate_id_invalid" in hold for hold in holds)
    assert any("pre_seed_candidate_class_invalid" in hold for hold in holds)
    assert any("pre_seed_candidate_source_status_invalid" in hold for hold in holds)
    assert any("pre_seed_candidate_score_invalid" in hold for hold in holds)


def test_stage603_malformed_candidate_mappings_and_booleans_return_structured_hold_not_traceback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    universe["candidates"][0]["id"] = True
    universe["candidates"][1]["evidence"] = ["S1"]
    universe["candidates"][2]["hard_gate_results"] = ["PASS"]
    universe["candidates"][3]["discovery_provenance"] = [{"type": {"nested": "current_state"}, "source_ref": ["S3"]}]
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert "Traceback" not in proc.stdout
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_candidate_id_invalid" in hold for hold in holds)
    assert any("pre_seed_candidate_evidence_invalid" in hold for hold in holds)
    assert any("pre_seed_candidate_hard_gate_results_invalid" in hold for hold in holds)
    assert any("pre_seed_candidate_provenance_ref_invalid" in hold for hold in holds)


def test_stage603_discovery_provenance_requires_well_formed_attribution_entries(tmp_path: Path) -> None:
    malformed_cases = [
        ("not_list_string", "x", "pre_seed_candidate_provenance_gap"),
        ("not_list_bool", True, "pre_seed_candidate_provenance_gap"),
        ("not_list_number", 1, "pre_seed_candidate_provenance_gap"),
        ("not_list_null", None, "pre_seed_candidate_provenance_gap"),
        ("list_string", ["x"], "pre_seed_candidate_provenance_invalid"),
        ("list_bool", [False], "pre_seed_candidate_provenance_invalid"),
        ("list_number", [1], "pre_seed_candidate_provenance_invalid"),
        ("list_null", [None], "pre_seed_candidate_provenance_invalid"),
        ("empty_dict", [{}], "pre_seed_candidate_provenance_type_invalid"),
        ("placeholder_type", [{"type": "TBD", "source_ref": "S1"}], "pre_seed_candidate_provenance_type_invalid"),
        ("placeholder_ref", [{"type": "independent_search", "source_ref": "TBD"}], "pre_seed_candidate_provenance_ref_invalid"),
        ("missing_ref", [{"type": "independent_search"}], "pre_seed_candidate_provenance_ref_invalid"),
    ]
    for label, provenance, expected_hold in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        universe["candidates"][1]["discovery_provenance"] = provenance
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        assert "TypeError" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any(expected_hold in hold for hold in holds), (label, holds)


def test_stage603_seed_discovery_provenance_requires_user_supplied_ref_not_fail_open(tmp_path: Path) -> None:
    malformed_cases = [
        ("list_string", ["x"], "pre_seed_candidate_provenance_invalid"),
        ("missing_ref", [{"type": "user_supplied"}], "pre_seed_candidate_provenance_ref_invalid"),
        ("wrong_type", [{"type": "independent_search", "source_ref": "S1"}], "pre_seed_candidate_user_supplied_provenance_gap"),
    ]
    for label, provenance, expected_hold in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        universe["candidates"][0]["discovery_provenance"] = provenance
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "brief", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any(expected_hold in hold for hold in holds), (label, holds)


def test_stage603_candidate_universe_nested_malformed_types_return_structured_hold_matrix(tmp_path: Path) -> None:
    cases = [
        ("candidate_name_dict", lambda universe, charter: universe["candidates"][1].update({"name": {"text": "Process"}}), "pre_seed_candidate_name_invalid"),
        ("candidate_evidence_nested_bad_ref", lambda universe, charter: universe["candidates"][2]["evidence"].update({"cost": [{"ref": "S99"}]}), "pre_seed_candidate_evidence_ref_invalid"),
        ("candidate_hard_gate_nested_bad_value", lambda universe, charter: universe["candidates"][2]["hard_gate_results"].update({"security": {"status": "PASS"}}), "pre_seed_candidate_hard_gate_result_invalid"),
        ("candidate_provenance_type_bool", lambda universe, charter: universe["candidates"][1].update({"discovery_provenance": [{"type": True, "source_ref": "S3"}]}), "pre_seed_candidate_provenance_type_invalid"),
        ("candidate_provenance_ref_list", lambda universe, charter: universe["candidates"][1].update({"discovery_provenance": [{"type": "independent_search", "source_ref": ["S3"]}]}), "pre_seed_candidate_provenance_ref_invalid"),
        ("candidate_provenance_extra_nested", lambda universe, charter: universe["candidates"][1].update({"discovery_provenance": [{"type": "independent_search", "source_ref": "S3", "notes": {"bad": True}}]}), "pre_seed_candidate_provenance_notes_invalid"),
        ("candidate_required_classes_nested", lambda universe, charter: universe.update({"required_candidate_classes": [{"class": "product"}]}), "pre_seed_required_candidate_classes_invalid"),
        ("candidate_independent_flag_string", lambda universe, charter: universe.update({"independent_discovery_required": "yes"}), "pre_seed_independent_discovery_required_invalid"),
        ("candidate_coverage_status_dict", lambda universe, charter: universe.update({"coverage_status": {"state": "complete"}}), "pre_seed_coverage_status_invalid"),
    ]
    for label, mutate, expected_hold in cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        mutate(universe, charter)
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        assert "TypeError" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any(expected_hold in hold for hold in holds), (label, holds)


def test_stage603_evidence_budget_compares_complete_records_not_only_normalized_allocation(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    charter["evidence_budget"] = {
        candidate["id"]: {"normalized_allocation": 1, "source_count": 2, "review_hours": 4}
        for candidate in universe["candidates"]
    }
    charter["evidence_budget"]["seed"] = {"normalized_allocation": 1, "source_count": 8, "review_hours": 4}
    charter["evidence_budget"]["process"] = {"normalized_allocation": 1, "source_count": 2, "review_hours": 6}
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_evidence_asymmetry" in hold for hold in holds)


def test_stage603_evidence_floor_enforces_minimum_sources_per_dimension(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    charter["evidence_floor"] = {"dimensions": ["security", "operations"], "minimum_sources_per_dimension": 2}
    universe["candidates"][0]["evidence"]["security"] = ["S1"]
    universe["candidates"][1]["evidence"]["operations"] = "S4"
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_candidate_evidence_gap seed:" in hold and "security" in hold for hold in holds)
    assert any("pre_seed_candidate_evidence_gap process:" in hold and "operations" in hold for hold in holds)


def test_stage603_evidence_floor_malformed_minimum_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    charter["evidence_floor"] = {"dimensions": ["security"], "minimum_sources_per_dimension": "two"}
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    holds = json.loads(proc.stdout)["holds"]
    assert any("pre_seed_evidence_floor_invalid" in hold for hold in holds)


def test_stage603_evidence_refs_must_be_non_placeholder_strings(tmp_path: Path) -> None:
    malformed_refs = [True, False, 1, 1.5, None, {"id": "S1"}, ["S1"], "", "TBD", "placeholder"]
    for index, malformed_ref in enumerate(malformed_refs):
        root = tmp_path / f"malformed_ref_{index}"
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        universe["candidates"][0]["evidence"]["security"] = [malformed_ref]
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (malformed_ref, proc.stdout)
        assert "Traceback" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any("pre_seed_candidate_evidence_gap seed:security" in hold for hold in holds), (malformed_ref, holds)


def test_stage603_evidence_refs_accept_non_placeholder_strings(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    universe["candidates"][0]["evidence"]["security"] = [" S-1 "]
    attach_preseed(root, universe, charter)

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0, proc.stdout


def test_stage603_malformed_hard_eligibility_gates_return_structured_hold_not_traceback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)
    universe, charter = passing_preseed_payloads()
    charter["hard_eligibility_gates"] = [{"id": "security"}, "operations"]
    attach_preseed(root, universe, charter)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert "Traceback" not in proc.stdout
    payload = json.loads(proc.stdout)
    assert any("pre_seed_hard_eligibility_gates_invalid" in hold for hold in payload["holds"])


def test_stage603_need_map_solution_selected_malformed_types_fail_closed_without_traceback(tmp_path: Path) -> None:
    malformed_cases = [
        ("true", True),
        ("string", "seed"),
        ("number", 1),
        ("list", []),
        ("dict", {"selected": "seed"}),
    ]
    for label, value in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        attach_preseed(root, universe, charter, need_overrides={"solution_selected": value})

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        assert "TypeError" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any("pre_seed_need_map_solution_selected_invalid" in hold for hold in holds), (label, holds)


def test_stage603_need_map_solution_selected_allows_only_false_or_null(tmp_path: Path) -> None:
    for label, value in [("false", False), ("null", None)]:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        attach_preseed(root, universe, charter, need_overrides={"solution_selected": value})

        proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

        assert proc.returncode == 0, proc.stdout


def test_stage603_comparison_charter_source_classes_require_non_placeholder_unique_strings(tmp_path: Path) -> None:
    malformed_cases = [
        ("null", [None]),
        ("bool", [True]),
        ("number", [1]),
        ("list", [["official"]]),
        ("dict", [{"class": "official"}]),
        ("placeholder", ["TBD"]),
        ("duplicate", ["official", " official "]),
    ]
    for label, source_classes in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        charter["source_classes"] = source_classes
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any("pre_seed_source_classes_invalid" in hold for hold in holds), (label, holds)


def test_stage603_comparison_charter_rubric_requires_well_formed_objects(tmp_path: Path) -> None:
    malformed_cases = [
        ("null", [None]),
        ("bool", [False]),
        ("number", [1]),
        ("list", [["security"]]),
        ("missing_scale", [{"criterion": "security", "weight": 1}]),
        ("missing_name", [{"id": "security", "weight": 1, "scale": "1-5"}]),
        ("nested_weight", [{"criterion": "security", "weight": {"score": 1}, "scale": "1-5"}]),
        ("placeholder_name", [{"criterion": "TBD", "weight": 1, "scale": "1-5"}]),
    ]
    for label, rubric in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        charter["rubric"] = rubric
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any("pre_seed_rubric_invalid" in hold for hold in holds), (label, holds)


def test_stage603_comparison_charter_duration_entries_require_schema_by_provenance(tmp_path: Path) -> None:
    malformed_cases = [
        ("null", [None], "pre_seed_duration_provenance_invalid"),
        ("bool", [True], "pre_seed_duration_provenance_invalid"),
        ("number", [1], "pre_seed_duration_provenance_invalid"),
        ("list", [["30 days"]], "pre_seed_duration_provenance_invalid"),
        ("missing_duration", [{"provenance": "heuristic"}], "pre_seed_duration_value_invalid"),
        ("evidence_missing_refs", [{"duration": "30 days", "provenance": "evidence"}], "pre_seed_duration_unsupported_as_evidence"),
        ("bad_provenance", [{"duration": "30 days", "provenance": "guess"}], "pre_seed_duration_provenance_invalid"),
        ("nested_duration", [{"duration": {"value": "30 days"}, "provenance": "heuristic"}], "pre_seed_duration_value_invalid"),
    ]
    for label, durations, expected_hold in malformed_cases:
        root = tmp_path / label
        write_depth_pass_fixture(root, deliverable_export_enabled=False)
        universe, charter = passing_preseed_payloads()
        charter["duration_provenance"] = durations
        attach_preseed(root, universe, charter)

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2, (label, proc.stdout)
        assert "Traceback" not in proc.stdout
        holds = json.loads(proc.stdout)["holds"]
        assert any(expected_hold in hold for hold in holds), (label, holds)


def test_stage603_broad_malformed_preseed_matrix_returns_structured_verdict_without_throwing(tmp_path: Path) -> None:
    cases = [
        ("source_forbidden_inferences", [None, True, 1, "TBD", [], {}, [{"nested": []}]], lambda need, universe, charter, value: None, "pre_seed_seed_forbidden_inferences_incomplete"),
        ("need_workloads", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: need.__setitem__("workloads", value), "pre_seed_need_map_workloads_missing"),
        ("need_outcomes", [None, False, 1.5, "TBD", {}, []], lambda need, universe, charter, value: need.__setitem__("outcomes", value), "pre_seed_need_map_outcomes_missing"),
        ("need_constraints", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: need.__setitem__("constraints", value), "pre_seed_need_map_constraints_missing"),
        ("candidate_candidates", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: universe.__setitem__("candidates", value), "pre_seed_candidate_universe_empty"),
        ("charter_hard_gates", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: charter.__setitem__("hard_eligibility_gates", value), "pre_seed_hard_eligibility_gates_empty"),
        ("charter_evidence_floor", [None, True, 1, "TBD", [], {}], lambda need, universe, charter, value: charter.__setitem__("evidence_floor", value), "pre_seed_evidence_floor_empty"),
        ("charter_evidence_budget", [None, False, 1, "TBD", [], {}], lambda need, universe, charter, value: charter.__setitem__("evidence_budget", value), "pre_seed_evidence_budget_empty"),
        ("charter_source_classes", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: charter.__setitem__("source_classes", value), "pre_seed_source_classes_empty"),
        ("charter_rubric", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: charter.__setitem__("rubric", value), "pre_seed_rubric_empty"),
        ("charter_blinded_aliases", [None, True, 1, "TBD", [], {}], lambda need, universe, charter, value: charter.__setitem__("blinded_aliases", value), "pre_seed_blinded_controls_incomplete"),
        ("charter_alias_order", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: charter.__setitem__("alias_order", value), "pre_seed_blinded_controls_incomplete"),
        ("charter_unblind_mapping", [None, True, 1, "TBD", [], {}], lambda need, universe, charter, value: charter.__setitem__("unblind_mapping", value), "pre_seed_blinded_controls_incomplete"),
        ("charter_duration_provenance", [None, True, 1, "TBD", {}, []], lambda need, universe, charter, value: charter.__setitem__("duration_provenance", value), "pre_seed_duration_provenance_empty"),
        ("charter_decision_states", [None, True, 1, "TBD", [], {}], lambda need, universe, charter, value: charter.__setitem__("decision_states", value), "pre_seed_decision_state_collapse"),
        ("charter_winner", [True, 1, [], {}, [{"nested": []}]], lambda need, universe, charter, value: charter.__setitem__("winner", value), "pre_seed_winner_invalid"),
    ]
    for field_name, malformed_values, mutate, expected_hold in cases:
        for value_index, value in enumerate(malformed_values):
            root = tmp_path / field_name / str(value_index)
            write_depth_pass_fixture(root, deliverable_export_enabled=False)
            universe, charter = passing_preseed_payloads()
            if field_name == "source_forbidden_inferences":
                attach_preseed(root, universe, charter)
                source_path = root / "preseed" / "source-characterization-v1.json"
                source = json.loads(source_path.read_text())
                source["forbidden_inferences"] = value
                source_path.write_text(json.dumps(source), encoding="utf-8")
                refresh_preseed_hashes(root)
            else:
                need = {"workloads": ["Intake triage workload"], "outcomes": ["Reduce manual review time"], "constraints": ["No production mutation"]}
                mutate(need, universe, charter, value)
                attach_preseed(root, universe, charter, need_overrides=need)

            proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

            assert proc.returncode == 2, (field_name, value, proc.stdout)
            assert "Traceback" not in proc.stdout
            assert "TypeError" not in proc.stdout
            holds = json.loads(proc.stdout)["holds"]
            assert any(expected_hold in hold for hold in holds), (field_name, value, holds)

def test_init_campaign_creates_worker_packets(tmp_path: Path) -> None:
    root = tmp_path / "campaign"

    proc = run([
        sys.executable,
        str(INIT),
        "--target",
        "Example Product",
        "--target-type",
        "product",
        "--output-layout",
        "legacy",
        "--root",
        str(root),
    ], check=True)

    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    manifest = json.loads((root / "manifest.json").read_text())
    lanes = manifest["lanes"]
    assert len(lanes) >= 6
    assert (root / "synthesis" / "visual-exhibits.json").exists()
    assert manifest["visual_exhibits"]["full_pack_min_exhibits"] == 3
    assert manifest["visual_exhibits"]["board_brief_min_exhibits"] == 2
    assert manifest["visual_exhibits"]["diagram_min_count"] == 1
    for lane in lanes:
        packet_path = root / "workers" / f"{lane['id']}-packet.md"
        assert packet_path.exists()
        packet = packet_path.read_text(encoding="utf-8")
        assert "authorizes universal characterization and evidence setup" in packet
        assert "Target-specific/comparison lane execution" in packet
        assert "Context Brief material" not in packet
        assert "internal source paths" in packet
        assert (root / "lanes" / f"{lane['id']}.md").exists()
        assert (root / "lanes" / f"{lane['id']}.json").exists()
    steering = (root / "steering.md").read_text(encoding="utf-8")
    assert "target-specific/comparison execution is not authorized" in steering
    assert "manifest.json.pre_seed" in steering
    parent = (root / "linear" / "parent.md").read_text(encoding="utf-8")
    assert "Ordinary preparatory research execution is pre-authorized" in parent
    assert "pre-seed gate passes" in parent


def test_scaffold_contains_dual_outputs_context_qa_and_routing_contracts(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    run([sys.executable, str(INIT), "--target", "Example Product", "--target-type", "product", "--output-layout", "legacy", "--root", str(root)], check=True)

    manifest = json.loads((root / "manifest.json").read_text())
    assert (root / "external" / "original-full-research-pack.md").exists()
    assert (root / "internal" / "full-research-pack.md").exists()
    assert (root / "internal" / "executive-brief.md").exists()
    assert (root / "context" / "context-brief.md").exists()
    context_json = json.loads((root / "context" / "context-brief.json").read_text())
    assert context_json["schema_version"] == "deep-research-context-brief-v1"
    assert {"source_path", "freshness", "authority", "privacy_classification", "exclusions"}.issubset(context_json["required_fields"])
    assert manifest["external_output"]["path"] == "external/original-full-research-pack.md"
    assert manifest["external_output"]["routing"] == "corpus_only_never_export"
    assert manifest["external_output"]["provenance_class"] == "clean_room_external_evidence_only"
    assert [item["path"] for item in manifest["internal_outputs"]] == [
        "internal/full-research-pack.md",
        "internal/executive-brief.md",
        "internal/plain-brief.md",
    ]
    assert manifest["deliverable_exports"]["allowlist"] == [
        "internal/full-research-pack.md",
        "internal/executive-brief.md",
        "internal/plain-brief.md",
    ]
    assert manifest["plain_brief"]["required"] is True
    assert manifest["plain_brief"]["review"] == "qa/plain-brief-review.json"
    assert manifest["deliverable_exports"]["plain_brief_staging"] == "internal/plain-brief.md"
    assert "external/original-full-research-pack.md" in manifest["deliverable_exports"]["denylist"]
    for rel in [
        "qa/packets/evidence-privacy.md",
        "qa/packets/visual.md",
        "qa/packets/taxonomy.md",
        "qa/packets/librarian-repair.md",
        "qa/context-handoff-review.json",
        "qa/evidence-privacy-review.json",
        "qa/visual-review.json",
        "qa/taxonomy-review.json",
        "qa/librarian-repair-receipt.json",
        "qa/final-assurance.json",
    ]:
        assert (root / rel).exists(), rel
    assert manifest["qa"]["receipts"]["evidence_privacy"] == "qa/evidence-privacy-review.json"


def test_scaffolded_true_deep_research_is_hold_not_pass(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    run([
        sys.executable,
        str(INIT),
        "--target",
        "Example Product",
        "--target-type",
        "product",
        "--root",
        str(root),
        "--output-layout",
        "legacy",
    ], check=True)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["status"] == "HOLD"
    assert payload["classification"] == "memo_depth"
    assert payload["metrics"]["visual_exhibits_status"] == "not_started"
    assert any("source_count" in hold for hold in payload["holds"])
    assert any("internal_full_pack_words" in hold for hold in payload["holds"])
    assert any("visual_exhibits_status" in hold for hold in payload["holds"])
    assert any("qa_receipt_context_handoff" in hold for hold in payload["holds"])


def write_depth_pass_fixture(
    root: Path,
    *,
    with_visuals: bool = True,
    target_type: str = "idea",
    deliverable_export_enabled: bool = True,
    receipt_overrides: dict[str, dict] | None = None,
    include_preseed: bool = True,
) -> None:
    for d in ["lanes", "workers", "synthesis", "history", "external", "internal", "context", "qa/packets", "export", "receipts"]:
        (root / d).mkdir(parents=True)
    lanes = [{"id": f"lane-{i}", "name": f"Lane {i}"} for i in range(6)]
    for lane in lanes:
        (root / "lanes" / f"{lane['id']}.md").write_text("# lane\nstatus: PASS\n", encoding="utf-8")
        (root / "lanes" / f"{lane['id']}.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
        (root / "workers" / f"{lane['id']}-packet.md").write_text("# packet\nclean-room external evidence only\n", encoding="utf-8")
    external_text = "# Original external full research pack\n\n"
    external_text += "## Evidence boundary\nClean-room external evidence only.\n\n"
    external_text += "## External evidence says\n" + "word [S1] " * 13000
    external_text += "\n## Visual decision surface / exhibit ledger\n### Exhibit E1 — System map\n### Exhibit E2 — Option matrix\n"
    (root / "external" / "original-full-research-pack.md").write_text(external_text, encoding="utf-8")
    external_hash = __import__("hashlib").sha256(external_text.encode()).hexdigest()
    internal_text = "---\ntitle: Example Product — full research pack\ncreated: 2026-08-30\ntype: source\nstage: import\nsource: https://example.com\nauthor: Research agent / Hermes internal synthesis\ncaptured_via: deep research internal synthesis\ntags: [source/adapted]\nrelated:\n  - \"[[executive-brief]]\"\n---\n# the owner full research pack\n\n"
    internal_text += "## What external evidence says\n" + "external_evidence [S1] word " * 4000
    internal_text += "\n## What the owner's existing knowledge and decisions say\ninternal_context [[Known Note]] word " + "word " * 3500
    internal_text += "\n## What follows when those layers are reconciled\ncontroller_inference word " + "word " * 3500
    internal_text += "\n## Internal reconciliation table\n| Topic | External evidence | Internal context | Reconciled implication |\n|---|---|---|---|\n| Fit | external_evidence [S2] | internal_context [[Known Note]] | controller_inference proceed carefully |\n"
    internal_text += "\n## Visual decision surface / exhibit ledger\n### Exhibit E1 — System map\n### Exhibit E2 — Option matrix\n### Exhibit E3 — Evidence heatmap\n"
    internal_text += "\n## ActionRegister\n" + "".join(f"| AR{i} | candidate | evidence |\n" for i in range(5))
    internal_text += "\n## Contradictions / gaps\nnone\n\n## What would change this\nnew evidence\n\n"
    (root / "internal" / "full-research-pack.md").write_text(internal_text, encoding="utf-8")
    brief_text = "---\ntitle: Example Product — executive brief\ncreated: 2026-08-30\ntype: source\nstage: import\nsource: https://example.com\nauthor: Research agent / Hermes internal synthesis\ncaptured_via: deep research internal synthesis\ntags: [source/adapted]\nrelated:\n  - \"[[full-research-pack]]\"\n---\n# the owner executive brief\n\n## The board answer\ncontroller_inference answer.\n\n## Key exhibits\n### Exhibit E1 — System map\n### Exhibit E2 — Option matrix\n\n## Caveats\nuncertainty survives.\n" + "word " * 1600
    (root / "internal" / "executive-brief.md").write_text(brief_text, encoding="utf-8")
    (root / "synthesis" / "full-pack.md").write_text(internal_text, encoding="utf-8")
    (root / "synthesis" / "board-brief.md").write_text(brief_text, encoding="utf-8")
    context = {
        "schema_version": "deep-research-context-brief-v1",
        "status": "complete",
        "items": [{"source_path": "/home/example/knowledge-base/Knowledge.md", "freshness": "2026-08-01", "authority": "owner-approved", "privacy_classification": "private", "exclusions": ["do not export externally"]}],
    }
    (root / "context" / "context-brief.md").write_text("# Context Brief\nPrivate the owner context.\n", encoding="utf-8")
    (root / "context" / "context-brief.json").write_text(json.dumps(context), encoding="utf-8")
    (root / "history" / "relevant-history.json").write_text(json.dumps({"status": "complete"}), encoding="utf-8")
    (root / "citations.json").write_text(json.dumps({"sources": [{"id": f"S{i}"} for i in range(25)]}), encoding="utf-8")
    if with_visuals:
        (root / "synthesis" / "visual-exhibits.json").write_text(json.dumps({
            "schema_version": "deep-research-visual-exhibits-v2",
            "status": "complete",
            "topic_requires_diagram": target_type in {"product", "company", "market", "category", "repo", "tool", "architecture", "internal-system", "workflow", "venture", "operating-model"},
            "exhibits": [
                {"id": "E1", "title": "System map", "type": "diagram", "output_class": "both", "appears_in": ["internal_full_pack", "internal_executive_brief", "external_full_pack"], "anchor": "Exhibit E1 — System map", "evidence_refs": ["S1"], "source_data_refs": ["S1"], "decision_use": "Shows workflow gates.", "render_method": "technical-diagrams", "source_artifact": "synthesis/diagrams/e1.json", "validation_receipt": "qa/diagram-e1-validation.json", "reviewer": "visual-qa"},
                {"id": "E2", "title": "Option matrix", "type": "matrix", "output_class": "both", "appears_in": ["internal_full_pack", "internal_executive_brief", "external_full_pack"], "anchor": "Exhibit E2 — Option matrix", "evidence_refs": ["S2"], "source_data_refs": ["S2"], "decision_use": "Compares build and watch options.", "reviewer": "visual-qa"},
                {"id": "E3", "title": "Evidence heatmap", "type": "heatmap", "output_class": "internal_full_pack", "appears_in": ["internal_full_pack"], "anchor": "Exhibit E3 — Evidence heatmap", "evidence_refs": ["S3"], "source_data_refs": ["S3"], "decision_use": "Separates strong from weak claims.", "reviewer": "visual-qa"},
            ],
        }), encoding="utf-8")
        (root / "qa" / "diagram-e1-validation.json").write_text(json.dumps({"status": "PASS"}), encoding="utf-8")
    receipts = {
        "context-handoff-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "context-qa", "producers": ["taxonomy"], "checks": {"isolation": "PASS", "privacy_classification": "PASS"}},
        "evidence-privacy-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "evidence-qa", "producers": ["external-worker", "controller"], "controller": "controller", "external_pack_sha256": external_hash, "internal_full_pack_sha256": __import__("hashlib").sha256(internal_text.encode()).hexdigest(), "privacy_scan_findings": [], "blocking": [], **semantic_review_fields()},
        "visual-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "visual-qa", "producers": ["controller"], "output_classes": {"external_full_pack": "PASS", "internal_full_pack": "PASS", "internal_executive_brief": "PASS"}},
        "taxonomy-review.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "taxonomy-qa", "producers": ["controller"], "reviewed_paths": ["internal/full-research-pack.md", "internal/executive-brief.md"], "live_taxonomy_hash": "abc123", "routing_invariant": "PASS", "tags": {"allowed": ["source/adapted"], "invalid": []}, "internal_links": {"status": "PASS"}},
        "librarian-repair-receipt.json": {"schema_version": "deep-research-qa-receipt-v1", "status": "PASS", "reviewer": "librarian-packaging", "producers": ["controller"], "changed_paths": []},
        "final-assurance.json": {"schema_version": "deep-research-final-assurance-v1", "status": "PASS", "reviewer": "research-final", "producers": ["controller"], "external_pack_sha256": external_hash},
    }
    if not deliverable_export_enabled:
        receipts["taxonomy-review.json"] = {"schema_version": "deep-research-qa-receipt-v1", "status": "N/A", "reason": "deliverable export explicitly suppressed", "reviewer": "taxonomy-qa", "producers": ["controller"]}
        receipts["librarian-repair-receipt.json"] = {"schema_version": "deep-research-qa-receipt-v1", "status": "N/A", "reason": "deliverable export explicitly suppressed", "reviewer": "librarian-packaging", "producers": ["controller"]}
    if receipt_overrides:
        for name, override in receipt_overrides.items():
            receipts[name].update(override)
    for name, data in receipts.items():
        (root / "qa" / name).write_text(json.dumps(data), encoding="utf-8")
    (root / "export" / "example-full.md").write_text((root / "internal" / "full-research-pack.md").read_text(), encoding="utf-8")
    (root / "export" / "example-brief.md").write_text((root / "internal" / "executive-brief.md").read_text(), encoding="utf-8")
    exported = []
    if deliverable_export_enabled:
        readback_root = root.parent / f"{root.name}-export-readback"
        readback_dir = readback_root
        readback_dir.mkdir(parents=True)
        for rel in ["internal/full-research-pack.md", "internal/executive-brief.md"]:
            source = root / rel
            destination = readback_dir / source.name
            destination.write_bytes(source.read_bytes())
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            exported.append({"staged_path": rel, "export_path": str(destination), "sha256": digest, "readback_sha256": digest})
    manifest_payload = {
        "report_purpose": "factual",
        "root": str(root),
        "target_type": target_type,
        "lanes": lanes,
        "external_output": {"path": "external/original-full-research-pack.md", "sha256": external_hash, "frozen_sha256": external_hash, "routing": "corpus_only_never_export"},
        "internal_outputs": [{"path": "internal/full-research-pack.md"}, {"path": "internal/executive-brief.md"}],
        "context": {"markdown": "context/context-brief.md", "json": "context/context-brief.json"},
        "qa": {"receipts": {"context_handoff": "qa/context-handoff-review.json", "evidence_privacy": "qa/evidence-privacy-review.json", "visual": "qa/visual-review.json", "taxonomy_review": "qa/taxonomy-review.json", "librarian_repair": "qa/librarian-repair-receipt.json", "final_assurance": "qa/final-assurance.json"}},
        "deliverable_exports": {"enabled": deliverable_export_enabled, "status": "copied_to_export" if deliverable_export_enabled else "suppressed_by_no_deliverable_export", "export_root": str(root.parent / f"{root.name}-export-readback"), "intended_directory": ".", "allowlist": ["internal/full-research-pack.md", "internal/executive-brief.md"], "full_pack_staging": "internal/full-research-pack.md", "board_brief_staging": "internal/executive-brief.md", "exports": exported},
        "visual_exhibits": {"path": "synthesis/visual-exhibits.json"},
    }
    if include_preseed:
        universe, charter = passing_preseed_payloads()
        from preseed import scaffold_preseed

        pre_seed = scaffold_preseed(root, target="Seed Vendor", primary_source="https://example.com", decision_question="Compare options")
        manifest_payload["pre_seed"] = pre_seed
        (root / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
        attach_preseed(root, universe, charter)
    else:
        (root / "manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
    refresh_fixture_acceptance_bindings(root)


def test_true_deep_research_holds_without_visual_exhibits(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, with_visuals=False)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["status"] == "HOLD"
    assert any("visual_exhibits_missing" in hold for hold in payload["holds"])


def test_true_deep_research_pass_requires_lane_workers_sources_exports_and_visuals(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, with_visuals=True)

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "PASS"
    assert payload["classification"] == "true_deep_research"
    assert payload["metrics"]["full_pack_exhibit_count"] == 3
    assert payload["metrics"]["board_brief_exhibit_count"] == 2
    assert payload["metrics"]["diagram_count"] == 1
    assert payload["metrics"]["external_pack_hash_stable"] is True


def test_true_deep_research_holds_on_exact_repeated_prose(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, with_visuals=True)
    pack = root / "internal" / "full-research-pack.md"
    repeated = "This deliberately long duplicated paragraph proves that repeated padding cannot satisfy true deep research quality gates even when all structural artifacts exist and word-count thresholds pass."
    pack.write_text(pack.read_text() + f"\n\n{repeated}\n\n{repeated}\n", encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["metrics"]["internal_full_pack_duplicate_prose_groups"] == 1
    assert any("internal_full_pack_duplicate_prose_groups" in hold for hold in payload["holds"])


def test_true_deep_research_thresholds_reward_depth_without_forcing_bloat() -> None:
    module = load_verify_module()
    thresholds = module.DEFAULT_THRESHOLDS["true_deep_research"]
    # Internal full-pack depth floor stays; human-facing outputs have caps, never floors.
    assert thresholds["min_full_words"] == 8000
    for mode_thresholds in module.DEFAULT_THRESHOLDS.values():
        assert "min_board_words" not in mode_thresholds
        assert mode_thresholds["max_plain_brief_words"] == module.PLAIN_BRIEF_MAX_WORDS
    assert module.load_plain_brief_module().MAX_WORDS == module.PLAIN_BRIEF_MAX_WORDS


def test_structural_true_deep_research_holds_without_diagram(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, with_visuals=True, target_type="product")
    visual_path = root / "synthesis" / "visual-exhibits.json"
    data = json.loads(visual_path.read_text())
    for exhibit in data["exhibits"]:
        if exhibit["type"] == "diagram":
            exhibit["type"] = "map"
    visual_path.write_text(json.dumps(data), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("diagram_count" in hold for hold in payload["holds"])


def test_board_brief_exhibits_require_board_brief_anchors(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, with_visuals=True)
    text = (root / "internal" / "executive-brief.md").read_text()
    (root / "internal" / "executive-brief.md").write_text(text.replace("### Exhibit E1 — System map\n### Exhibit E2 — Option matrix", "No exhibits here."), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["metrics"]["board_brief_exhibit_count"] == 0
    assert any("board_brief_exhibits" in hold for hold in payload["holds"])


def test_structural_diagram_requirement_cannot_be_waived_by_ledger(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, with_visuals=True, target_type="operating-model")
    visual_path = root / "synthesis" / "visual-exhibits.json"
    data = json.loads(visual_path.read_text())
    data["topic_requires_diagram"] = False
    for exhibit in data["exhibits"]:
        if exhibit["type"] == "diagram":
            exhibit["type"] = "map"
    visual_path.write_text(json.dumps(data), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["metrics"]["topic_requires_diagram"] is True
    assert any("diagram_count" in hold for hold in payload["holds"])


def test_operating_model_scaffold_requires_diagram(tmp_path: Path) -> None:
    root = tmp_path / "campaign"

    run([
        sys.executable,
        str(INIT),
        "--target",
        "Operating Model Example",
        "--target-type",
        "operating-model",
        "--root",
        str(root),
    ], check=True)

    manifest = json.loads((root / "manifest.json").read_text())
    visual = json.loads((root / "synthesis" / "visual-exhibits.json").read_text())
    assert manifest["visual_exhibits"]["topic_requires_diagram"] is True
    assert manifest["visual_exhibits"]["diagram_min_count"] == 1
    assert visual["topic_requires_diagram"] is True
    assert visual["thresholds"]["diagram_min_count"] == 1


def test_external_privacy_scan_blocks_private_markers(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    external = root / "external" / "original-full-research-pack.md"
    external.write_text(external.read_text() + "\nLeaked [[Private Note]] and ISSUE-520 and /home/example/knowledge-base.\n", encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("external_privacy_scan" in hold for hold in payload["holds"])


def test_external_privacy_scan_blocks_lowercase_and_unicode_linear_refs(tmp_path: Path) -> None:
    for leaked in ["ISSUE-520", "ISSUE—520", "https://linear.app/example-team/issue/ISSUE-520/example"]:
        root = tmp_path / leaked.replace("/", "_").replace(":", "_")
        write_depth_pass_fixture(root)
        external = root / "external" / "original-full-research-pack.md"
        external.write_text(external.read_text() + f"\nLeaked {leaked}.\n", encoding="utf-8")

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2
        assert any("external_privacy_scan" in hold for hold in json.loads(proc.stdout)["holds"])


def test_external_privacy_scan_blocks_context_path_and_space_variants(tmp_path: Path) -> None:
    for leaked in [
        "context/context-brief.md",
        "context\\context_brief.json",
        "context brief",
        "internal context",
        "internal-context",
    ]:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        write_depth_pass_fixture(root)
        external = root / "external" / "original-full-research-pack.md"
        external.write_text(external.read_text() + f"\nLeaked {leaked}.\n", encoding="utf-8")

        proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

        assert proc.returncode == 2
        assert any("external_privacy_scan" in hold for hold in json.loads(proc.stdout)["holds"])


def test_exporter_blocks_context_path_and_space_leak_even_when_hashes_refresh(tmp_path: Path) -> None:
    for leaked in ["context/context-brief.md", "internal context"]:
        root = tmp_path / str(len(list(tmp_path.iterdir())))
        export = root / "test-export"
        prepare_test_export(export)
        write_depth_pass_fixture(root)
        external = root / "external" / "original-full-research-pack.md"
        external.write_text(external.read_text() + f"\nLeaked {leaked}.\n", encoding="utf-8")
        digest = __import__("hashlib").sha256(external.read_bytes()).hexdigest()
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["external_output"]["sha256"] = digest
        manifest["external_output"]["frozen_sha256"] = digest
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        for name in ["evidence-privacy-review.json", "final-assurance.json"]:
            receipt = root / "qa" / name
            data = json.loads(receipt.read_text())
            data["external_pack_sha256"] = digest
            receipt.write_text(json.dumps(data), encoding="utf-8")

        proc = run_frozen_legacy_export(root, export, "--dry-run")

        assert proc.returncode == 3
        assert "privacy scan failed" in proc.stdout


def test_evidence_reviewer_cannot_equal_controller_or_producer(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, receipt_overrides={"evidence-privacy-review.json": {"reviewer": "controller"}})

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("qa_independence_evidence_privacy" in hold for hold in payload["holds"])


def test_missing_or_hold_visual_review_blocks_true_deep_research_pass(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, receipt_overrides={"visual-review.json": {"status": "HOLD"}})

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("qa_receipt_visual status HOLD" in hold for hold in payload["holds"])


def test_internal_exec_exhibit_drift_blocks_pass(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    brief = root / "internal" / "executive-brief.md"
    brief.write_text(brief.read_text().replace("Exhibit E2 — Option matrix", "Exhibit E2 — Different matrix"), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("visual_exhibit_E2_internal_executive_brief_anchor_missing" in hold for hold in payload["holds"])


def test_internal_context_visuals_appearing_in_external_pack_block_pass(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    visual = json.loads((root / "synthesis" / "visual-exhibits.json").read_text())
    visual["exhibits"][2]["appears_in"].append("external_full_pack")
    visual["exhibits"][2]["uses_internal_context"] = True
    (root / "synthesis" / "visual-exhibits.json").write_text(json.dumps(visual), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("visual_exhibit_E3_internal_context_in_external" in hold for hold in payload["holds"])


def test_structural_rendered_diagram_without_validation_receipt_blocks_pass(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, target_type="product")
    (root / "qa" / "diagram-e1-validation.json").unlink()

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("visual_exhibit_E1_validation_receipt_missing" in hold for hold in payload["holds"])


def test_verifier_rejects_visual_ledger_outside_campaign_root(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    visual = root / "synthesis" / "visual-exhibits.json"
    outside = tmp_path / "outside-visual-exhibits.json"
    outside.write_bytes(visual.read_bytes())
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["visual_exhibits"]["path"] = "../outside-visual-exhibits.json"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert any("visual_exhibits_unsafe_path" in hold for hold in json.loads(proc.stdout)["holds"])


def test_off_vocabulary_tag_and_missing_frontmatter_block_pass(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, receipt_overrides={"taxonomy-review.json": {"tags": {"allowed": ["source/adapted"], "invalid": ["made/up"]}}})
    pack = root / "internal" / "full-research-pack.md"
    pack.write_text("# Missing frontmatter\n" + pack.read_text(), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("frontmatter_missing internal/full-research-pack.md" in hold for hold in payload["holds"])
    assert any("export_tags_invalid" in hold for hold in payload["holds"])


def test_export_suppressed_run_allows_export_only_receipts_na_with_reason(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root, deliverable_export_enabled=False)

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "PASS"
    assert payload["metrics"]["deliverable_export_enabled"] is False


def test_exporter_hard_fails_when_external_or_qa_artifact_is_presented_for_export(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": ".", "extra_staging": "external/original-full-research-pack.md"})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run_frozen_legacy_export(root, export, "--dry-run")

    assert proc.returncode == 3
    assert "not in deliverable export allowlist" in proc.stdout


def test_exporter_rejects_missing_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].pop("allowlist")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run_frozen_legacy_export(root, export, "--dry-run")

    assert proc.returncode == 3
    assert "deliverable export allowlist is required" in proc.stdout


def test_verifier_rejects_symlinked_manifest(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    manifest = root / "manifest.json"
    outside = tmp_path / "outside-manifest.json"
    outside.write_bytes(manifest.read_bytes())
    manifest.unlink()
    manifest.symlink_to(outside)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode != 0
    assert "manifest not found" in proc.stdout


def test_exporter_rejects_absolute_or_traversing_destination(tmp_path: Path) -> None:
    for intended in [str(tmp_path / "outside"), "../outside"]:
        root = tmp_path / intended.replace("/", "_").replace(".", "dot")
        export = tmp_path / "export"
        prepare_test_export(export)
        write_depth_pass_fixture(root)
        manifest = json.loads((root / "manifest.json").read_text())
        manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": intended})
        (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        from test_acceptance_repairs import seal_test_legacy_pack_acceptance
        seal_test_legacy_pack_acceptance(root)

        proc = run_frozen_legacy_export(root, export, "--dry-run")

        assert proc.returncode == 3
        assert "intended_directory must be exactly ." in proc.stdout


def test_exporter_rejects_export_root_inside_campaign(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = root / "fake-export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": "."})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    from test_acceptance_repairs import seal_test_legacy_pack_acceptance
    seal_test_legacy_pack_acceptance(root)

    dry_run = run_frozen_legacy_export(root, export, "--dry-run")
    live = run_frozen_legacy_export(root, export)

    assert dry_run.returncode == 3
    assert live.returncode == 3
    assert "export root must be outside the campaign root" in dry_run.stdout
    assert "export root must be outside the campaign root" in live.stdout


def test_exporter_rejects_symlinked_internal_source(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    source = root / "internal" / "full-research-pack.md"
    outside = tmp_path / "outside-secret.md"
    outside.write_text("secret private content", encoding="utf-8")
    source.unlink()
    source.symlink_to(outside)

    proc = run_frozen_legacy_export(root, export, "--dry-run")

    assert proc.returncode == 3
    assert "symlink not allowed" in proc.stdout


def test_exporter_rejects_existing_export_destination_without_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": "."})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    from test_acceptance_repairs import seal_test_legacy_pack_acceptance
    seal_test_legacy_pack_acceptance(root)
    destination = export / "full-research-pack.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("existing owner note", encoding="utf-8")

    dry_run = run_frozen_legacy_export(root, export, "--dry-run")
    live = run_frozen_legacy_export(root, export)

    assert dry_run.returncode == 3
    assert live.returncode == 3
    assert "export destination already exists" in dry_run.stdout
    assert destination.read_text(encoding="utf-8") == "existing owner note"


def test_exporter_preflights_all_destinations_before_any_copy(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": "."})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    from test_acceptance_repairs import seal_test_legacy_pack_acceptance
    seal_test_legacy_pack_acceptance(root)
    target_dir = export
    target_dir.mkdir(parents=True)
    second_destination = target_dir / "executive-brief.md"
    second_destination.write_text("existing brief", encoding="utf-8")
    first_destination = target_dir / "full-research-pack.md"

    proc = run_frozen_legacy_export(root, export)

    assert proc.returncode == 3
    assert "export destination already exists" in proc.stdout
    assert not first_destination.exists()
    assert second_destination.read_text(encoding="utf-8") == "existing brief"


def test_exporter_copy_one_uses_atomic_no_clobber(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    export_module = load_export_module()
    dest = tmp_path / "export" / "full-research-pack.md"
    dest.parent.mkdir(parents=True)
    real_open = Path.open

    def racing_open(self: Path, *args, **kwargs):
        if self == dest and args and args[0] == "xb" and not self.exists():
            self.write_text("racing owner note", encoding="utf-8")
        return real_open(self, *args, **kwargs)

    export_module.Path.open = racing_open

    try:
        try:
            export_module.copy_one(root, "internal/full-research-pack.md", dest)
        except SystemExit as exc:
            assert "export destination already exists" in str(exc)
        else:
            raise AssertionError("copy_one overwrote a racing destination")
    finally:
        export_module.Path.open = real_open

    assert dest.read_text(encoding="utf-8") == "racing owner note"


def test_exporter_copy_failure_preserves_partial_destination_for_inspection(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    export_module = load_export_module()
    dest = tmp_path / "export" / "full-research-pack.md"
    dest.parent.mkdir(parents=True)
    real_copystat = export_module.shutil.copystat

    def failing_copystat(*args, **kwargs):
        raise OSError("simulated metadata failure")

    export_module.shutil.copystat = failing_copystat

    try:
        try:
            export_module.copy_one(root, "internal/full-research-pack.md", dest)
        except SystemExit as exc:
            assert "inspect partial destination before retry" in str(exc)
        else:
            raise AssertionError("copy_one unexpectedly succeeded after copystat failure")
    finally:
        export_module.shutil.copystat = real_copystat

    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == (root / "internal" / "full-research-pack.md").read_text(encoding="utf-8")


def test_exporter_rejects_self_attested_or_stale_qa(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    receipt = root / "qa" / "evidence-privacy-review.json"
    data = json.loads(receipt.read_text())
    data["reviewer"] = "controller"
    receipt.write_text(json.dumps(data), encoding="utf-8")

    proc = run_frozen_legacy_export(root, export, "--dry-run")

    assert proc.returncode == 3
    assert "reviewer" in proc.stdout or "QA receipt" in proc.stdout


def test_verifier_rejects_missing_context_and_qa_identity_fields(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    (root / "context" / "context-brief.md").unlink()
    evidence = root / "qa" / "evidence-privacy-review.json"
    data = json.loads(evidence.read_text())
    data.pop("reviewer")
    data.pop("producers")
    evidence.write_text(json.dumps(data), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert any("context_brief_missing" in hold for hold in payload["holds"])
    assert any("qa_receipt_evidence_privacy reviewer_missing" in hold for hold in payload["holds"])
    assert any("qa_receipt_evidence_privacy producers_missing" in hold for hold in payload["holds"])


def test_exporter_rejects_missing_context_brief(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    (root / "context" / "context-brief.md").unlink()

    proc = run_frozen_legacy_export(root, export, "--dry-run")

    assert proc.returncode == 3
    assert "context/context-brief.md" in proc.stdout


def test_verifier_and_exporter_reject_placeholder_context_item_values(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    context = root / "context" / "context-brief.json"
    data = json.loads(context.read_text())
    data["items"] = [{
        "source_path": "",
        "freshness": "TBD",
        "authority": "",
        "privacy_classification": "TODO",
        "exclusions": [],
    }]
    context.write_text(json.dumps(data), encoding="utf-8")

    verify = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])
    export = run_frozen_legacy_export(root, export, "--dry-run")

    assert verify.returncode == 2
    holds = json.loads(verify.stdout)["holds"]
    assert "context_brief_item_1_placeholder_value" in holds
    assert "context_brief_item_1_exclusions_incomplete" in holds
    assert export.returncode == 3
    assert "Context Brief item" in export.stdout


def test_verifier_and_exporter_reject_prefixed_placeholder_context_values(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    context = root / "context" / "context-brief.json"
    data = json.loads(context.read_text())
    data["items"] = [{
        "source_path": "TBD after Taxonomy reviewer retrieval",
        "freshness": "N/A - no relevant context",
        "authority": "TODO: confirm owner",
        "privacy_classification": "TBD pending classification",
        "exclusions": ["N/A - no exclusions"],
    }]
    context.write_text(json.dumps(data), encoding="utf-8")

    verify = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])
    export = run_frozen_legacy_export(root, export, "--dry-run")

    assert verify.returncode == 2
    holds = json.loads(verify.stdout)["holds"]
    assert "context_brief_item_1_placeholder_value" in holds
    assert "context_brief_item_1_exclusions_incomplete" in holds
    assert export.returncode == 3
    assert "Context Brief item" in export.stdout


def test_exporter_rejects_tainted_external_pack_even_with_updated_hashes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    external = root / "external" / "original-full-research-pack.md"
    external.write_text(external.read_text() + "\nLeaked [[Private Note]] and ISSUE-520.\n", encoding="utf-8")
    digest = __import__("hashlib").sha256(external.read_bytes()).hexdigest()
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["external_output"]["sha256"] = digest
    manifest["external_output"]["frozen_sha256"] = digest
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    for name in ["evidence-privacy-review.json", "final-assurance.json"]:
        receipt = root / "qa" / name
        data = json.loads(receipt.read_text())
        data["external_pack_sha256"] = digest
        receipt.write_text(json.dumps(data), encoding="utf-8")

    proc = run_frozen_legacy_export(root, export, "--dry-run")

    assert proc.returncode == 3
    assert "privacy scan failed" in proc.stdout


def test_verifier_rejects_empty_context_without_reason_and_self_attested_final_assurance(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    context = root / "context" / "context-brief.json"
    data = json.loads(context.read_text())
    data["items"] = []
    data.pop("no_relevant_context_reason", None)
    context.write_text(json.dumps(data), encoding="utf-8")
    final = root / "qa" / "final-assurance.json"
    assurance = json.loads(final.read_text())
    assurance["reviewer"] = "controller"
    final.write_text(json.dumps(assurance), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert "context_brief_empty_without_reason" in payload["holds"]
    assert any("qa_independence_final_assurance" in hold for hold in payload["holds"])


def test_verifier_rejects_missing_actual_deliverable_export_readback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].pop("exports")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert "deliverable_export_readback_missing" in payload["holds"]


def test_verifier_rejects_symlinked_export_readback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    first = manifest["deliverable_exports"]["exports"][0]
    path = Path(first["export_path"])
    outside = tmp_path / "outside-note.md"
    outside.write_bytes((root / first["staged_path"]).read_bytes())
    path.unlink()
    path.symlink_to(outside)
    first["readback_sha256"] = __import__("hashlib").sha256(outside.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert any(
        hold in json.loads(proc.stdout)["holds"]
        for hold in ["deliverable_export_destination_symlink", "deliverable_export_destination_readback_invalid", "deliverable_export_destination_outside_allowlisted_directory"]
    )


def test_verifier_rejects_forged_export_readback_inside_campaign(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"]["export_root"] = str(root)
    forged_dir = root
    for item in manifest["deliverable_exports"]["exports"]:
        source = root / item["staged_path"]
        forged = forged_dir / source.name
        forged.write_bytes(source.read_bytes())
        digest = __import__("hashlib").sha256(forged.read_bytes()).hexdigest()
        item["export_path"] = str(forged)
        item["sha256"] = digest
        item["readback_sha256"] = digest
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert "deliverable_export_root_inside_campaign" in json.loads(proc.stdout)["holds"]


def test_verifier_accepts_configured_custom_export_destination_basename(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    item = manifest["deliverable_exports"]["exports"][0]
    old = Path(item["export_path"])
    custom = old.with_name("custom-full-pack-name.md")
    custom.write_bytes(old.read_bytes())
    item["export_path"] = str(custom)
    item["readback_sha256"] = __import__("hashlib").sha256(custom.read_bytes()).hexdigest()
    manifest["deliverable_exports"]["full_pack_intended_export_path"] = str(custom)
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    refresh_fixture_acceptance_bindings(root)

    proc = run_frozen_legacy_cli(root, VERIFY, "--campaign-root", str(root), "--mode", "true_deep_research", "--json")

    assert proc.returncode == 0
    assert json.loads(proc.stdout)["status"] == "PASS"


def test_verifier_rejects_unconfigured_custom_export_destination_path(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    item = manifest["deliverable_exports"]["exports"][0]
    old = Path(item["export_path"])
    wrong = old.with_name("wrong-note.md")
    wrong.write_bytes(old.read_bytes())
    item["export_path"] = str(wrong)
    item["readback_sha256"] = __import__("hashlib").sha256(wrong.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert "deliverable_export_destination_path_mismatch" in json.loads(proc.stdout)["holds"]


def test_verifier_rejects_stale_deliverable_export_source_hash(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    write_depth_pass_fixture(root)
    source = root / "internal" / "full-research-pack.md"
    source.write_text(source.read_text(encoding="utf-8") + "\nChanged after recorded deliverable export.\n", encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])

    assert proc.returncode == 2
    assert "deliverable_export_staged_source_hash_mismatch" in json.loads(proc.stdout)["holds"]


def test_malformed_manifest_fails_cleanly_without_traceback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "manifest.json").write_text("{not-json", encoding="utf-8")

    verify = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])
    export = run([sys.executable, str(EXPORT), "--campaign-root", str(root), "--dry-run"])

    assert verify.returncode == 3
    assert json.loads(verify.stdout)["classification"] == "invalid_manifest"
    assert "Traceback" not in verify.stdout
    assert export.returncode != 0
    assert "Traceback" not in export.stdout


def test_exporter_copies_only_two_internal_allowlisted_files_and_reads_back_hashes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": "."})
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    from test_acceptance_repairs import seal_test_legacy_pack_acceptance
    seal_test_legacy_pack_acceptance(root)

    proc = run_frozen_legacy_export(root, export)
    assert proc.returncode == 0, proc.stdout

    payload = json.loads(proc.stdout)
    assert [Path(item["staged_path"]).as_posix() for item in payload["exports"]] == [
        "internal/full-research-pack.md",
        "internal/executive-brief.md",
    ]
    assert all(Path(item["export_path"]).exists() for item in payload["exports"])
    assert not (export / "original-full-research-pack.md").exists()
    updated = json.loads((root / "manifest.json").read_text())
    assert updated["deliverable_exports"]["status"] == "copied_to_export"
    assert all(item["sha256"] == item["readback_sha256"] for item in updated["deliverable_exports"]["exports"])


def test_exporter_rejects_missing_visual_output_class_results(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    export = tmp_path / "export"
    prepare_test_export(export)
    write_depth_pass_fixture(root)
    receipt = root / "qa" / "visual-review.json"
    data = json.loads(receipt.read_text())
    data["output_classes"] = {"internal_full_pack": "PASS"}
    receipt.write_text(json.dumps(data), encoding="utf-8")

    proc = run_frozen_legacy_export(root, export, "--dry-run")

    assert proc.returncode == 3
    assert "Visual QA output classes" in proc.stdout


# ISSUE-534 Lane B/C durable campaign controller contracts
CONTROLLER = SCRIPT_DIR / "campaign_controller.py"


def load_controller_module():
    spec = importlib.util.spec_from_file_location("campaign_controller", CONTROLLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _init_controller_campaign(root: Path) -> None:
    run([sys.executable, str(INIT), "--target", "Example Product", "--target-type", "product", "--output-layout", "legacy", "--root", str(root)], check=True)


def test_init_campaign_creates_durable_controller_state_with_stable_fingerprints_and_proofs(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)

    state = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))

    assert state["schema_version"] == "deep-research-campaign-controller-v1"
    assert state["phase_order"] == ["intake", "lanes", "external_freeze", "internal_synthesis", "qa", "plain_brief", "export_closeout"]
    assert state["phases"]["intake"]["status"] == "complete"
    assert state["phases"]["lanes"]["status"] == "pending"
    assert state["metrics"]["status_poll_loop_api_calls"] == 0
    assert state["export_closeout"] == {
        "requested": True,
        "local_copy": False,
        "local_commit": False,
        "remote_push": False,
        "remote_readback": False,
        "complete": False,
        "terminal_reason": "missing_proofs:local_copy,local_commit,remote_push,remote_readback",
    }
    assert manifest["controller_state"]["path"] == "campaign_state.json"
    assert manifest["controller_state"]["no_poll_loop_api"] is True
    first = state["lanes"][0]
    assert first["lane_fingerprint"]
    assert first["input_fingerprint"]
    assert first["status"] == "pending"
    assert first["attempts"] == []
    assert first["dependencies"] == []
    assert first["timestamps"]["created_at"]

    _init_controller_campaign(root)
    state_again = json.loads((root / "campaign_state.json").read_text(encoding="utf-8"))
    assert [lane["lane_fingerprint"] for lane in state_again["lanes"]] == [lane["lane_fingerprint"] for lane in state["lanes"]]
    assert [lane["input_fingerprint"] for lane in state_again["lanes"]] == [lane["input_fingerprint"] for lane in state["lanes"]]


def test_reinitialization_invalidates_completed_lane_when_its_input_changes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    state = controller.load_state(root)
    lane_id = state["lanes"][0]["id"]
    claim = controller.claim_lane(root, lane_id, route="primary", worker_id="worker-a")
    output = root / "lanes" / f"{lane_id}.md"
    output.write_text("# completed under old inputs\n", encoding="utf-8")
    controller.ingest_completion_event(
        root,
        {"lane_id": lane_id, "attempt_id": claim["attempt_id"], "status": "completed", "output_path": f"lanes/{lane_id}.md"},
    )
    original_fingerprint = next(item for item in controller.load_state(root)["lanes"] if item["id"] == lane_id)["input_fingerprint"]

    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["decision_question"] = "A materially changed decision question"
    controller.initialize_campaign_state(root, manifest)
    refreshed = next(item for item in controller.load_state(root)["lanes"] if item["id"] == lane_id)
    selection = controller.resume_selection(root)

    assert refreshed["input_fingerprint"] != original_fingerprint
    assert refreshed["status"] == "stale"
    assert refreshed["terminal_reason"] == "input_fingerprint_changed"
    assert lane_id in selection["selected_lanes"]
    assert lane_id not in selection["reused_lanes"]


def test_controller_claim_join_complete_and_resume_reuses_valid_completed_lanes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    lane_id = json.loads((root / "campaign_state.json").read_text())["lanes"][0]["id"]

    claim = controller.claim_lane(root, lane_id, route="web", worker_id="worker-a")
    duplicate_join = controller.claim_lane(root, lane_id, route="web", worker_id="worker-b", duplicate_policy="join")
    duplicate_reject = controller.claim_lane(root, lane_id, route="web", worker_id="worker-c", duplicate_policy="reject")

    assert claim["status"] == "claimed"
    assert duplicate_join["status"] == "joined"
    assert duplicate_join["attempt_id"] == claim["attempt_id"]
    assert duplicate_reject["status"] == "rejected"

    output = root / "lanes" / f"{lane_id}.md"
    output.write_text("# Lane\n\n## Findings\nEvidence [S1].\n", encoding="utf-8")
    done = controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": claim["attempt_id"], "status": "completed", "output_path": f"lanes/{lane_id}.md"})
    selected = controller.resume_selection(root)
    state = controller.load_state(root)

    assert done["status"] == "completed"
    lane = next(item for item in state["lanes"] if item["id"] == lane_id)
    assert lane["status"] == "completed"
    assert lane["output_hash"] == controller.sha256_path(output)
    assert lane["attempts"][0]["status"] == "completed"
    assert lane_id in selected["reused_lanes"]
    assert lane_id not in selected["selected_lanes"]
    assert state["metrics"]["reused_lanes"] >= 1
    assert state["metrics"]["status_poll_loop_api_calls"] == 0
    assert not hasattr(controller, "poll_worker_status")


def test_controller_retry_ceiling_allows_one_same_route_retry_and_one_fallback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    lane_id = controller.load_state(root)["lanes"][0]["id"]

    first = controller.claim_lane(root, lane_id, route="primary", worker_id="w1")
    controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": first["attempt_id"], "status": "failed", "terminal_reason": "provider_timeout"})
    second = controller.claim_lane(root, lane_id, route="primary", worker_id="w2")
    assert second["status"] == "claimed"
    assert second["attempt_kind"] == "same_route_retry"
    controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": second["attempt_id"], "status": "failed", "terminal_reason": "provider_timeout"})
    third = controller.claim_lane(root, lane_id, route="fallback", worker_id="w3", fallback=True)
    assert third["status"] == "claimed"
    assert third["attempt_kind"] == "fallback"
    controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": third["attempt_id"], "status": "failed", "terminal_reason": "fallback_exhausted"})

    denied = controller.claim_lane(root, lane_id, route="fallback", worker_id="w4", fallback=True)
    state = controller.load_state(root)
    lane = next(item for item in state["lanes"] if item["id"] == lane_id)
    assert denied["status"] == "rejected"
    assert "retry_ceiling_exhausted" in denied["reason"]
    assert lane["status"] == "terminal"
    assert lane["terminal_reason"] == "retry_ceiling_exhausted"
    assert state["metrics"]["retries"] == 1
    assert state["metrics"]["fallback_attempts"] == 1


def test_controller_dependency_aware_invalidation_targets_only_downstream_dependents(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    state = controller.load_state(root)
    upstream, downstream, independent = [lane["id"] for lane in state["lanes"][:3]]
    controller.set_lane_dependencies(root, downstream, [upstream])

    for lane_id in [upstream, downstream, independent]:
        claim = controller.claim_lane(root, lane_id, route="primary", worker_id=f"w-{lane_id}")
        output = root / "lanes" / f"{lane_id}.md"
        output.write_text(f"# {lane_id}\n", encoding="utf-8")
        controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": claim["attempt_id"], "status": "completed", "output_path": f"lanes/{lane_id}.md"})

    invalidated = controller.invalidate_artifact(root, f"lanes/{upstream}.md", reason="source changed")
    selection = controller.resume_selection(root)

    assert invalidated["invalidated_lanes"] == [downstream]
    assert upstream in selection["reused_lanes"]
    assert independent in selection["reused_lanes"]
    assert downstream in selection["selected_lanes"]


def test_controller_restart_packet_and_export_lifecycle_require_all_requested_proofs(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()

    packet = controller.restart_packet(root, interruption_reason="session interrupted")
    assert packet["interruption_reason"] == "session interrupted"
    assert packet["next_phase"] == "lanes"
    assert packet["resume"]["selected_lanes"]
    assert packet["metrics"]["workers_total"] == 0
    assert packet["no_poll_loop_api"] is True
    assert (root / "receipts" / "restart-packet.json").exists()

    digest = controller.sha256_path(root / "internal" / "full-research-pack.md")
    readback = tmp_path / "export" / "full-research-pack.md"
    readback.parent.mkdir(parents=True)
    readback.write_bytes((root / "internal" / "full-research-pack.md").read_bytes())

    partial = controller.record_export_proof(root, local_copy={"staged_path": "internal/full-research-pack.md", "sha256": digest}, local_commit={"commit": "abc1234", "ref": "refs/heads/main"})
    assert partial["complete"] is False
    assert partial["terminal_reason"] == "missing_proofs:remote_push,remote_readback"
    complete = controller.record_export_proof(root, remote_push={"remote": "origin", "ref": "main", "commit": "abc1234"}, remote_readback={"path": str(readback), "sha256": digest})
    assert complete["local_copy"] == {"staged_path": "internal/full-research-pack.md", "sha256": digest}
    assert complete["local_commit"] == {"commit": "abc1234", "ref": "refs/heads/main"}
    assert complete["remote_push"] == {"remote": "origin", "ref": "main", "commit": "abc1234"}
    assert complete["remote_readback"] == {"path": str(readback), "sha256": digest}
    assert complete["complete"] is True
    assert complete["terminal_reason"] == "complete"


def test_controller_rejects_remote_readback_inside_campaign(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    internal = root / "internal" / "full-research-pack.md"
    digest = controller.sha256_path(internal)

    result = controller.record_export_proof(
        root,
        local_copy={"staged_path": "internal/full-research-pack.md", "sha256": digest},
        local_commit={"commit": "abcdef1234567890", "ref": "refs/heads/main"},
        remote_push={"remote": "origin", "ref": "main", "commit": "abcdef1234567890"},
        remote_readback={"path": str(internal), "sha256": digest},
    )

    assert result["complete"] is False
    assert result["validated"]["remote_readback"] is False


# ISSUE-534 Lane B independent review regression tests

def test_controller_resume_recovers_interrupted_running_lane_after_lease_expiry(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    lane_id = controller.load_state(root)["lanes"][0]["id"]
    claim = controller.claim_lane(root, lane_id, route="primary", worker_id="worker-a", lease_seconds=1)
    assert claim["status"] == "claimed"

    selection = controller.resume_selection(root, stale_after_seconds=0)
    state = controller.load_state(root)
    lane = next(item for item in state["lanes"] if item["id"] == lane_id)

    assert lane_id in selection["selected_lanes"]
    assert lane["status"] == "stale"
    assert lane["attempts"][-1]["status"] == "stale"
    assert lane["terminal_reason"] == "lease_expired"
    assert state["metrics"]["stale_lanes"] >= 1


def test_controller_terminal_retry_ceiling_lane_is_not_selected_on_resume_and_blocks_phase(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    lane_id = controller.load_state(root)["lanes"][0]["id"]

    for route, worker, fallback in [("primary", "w1", False), ("primary", "w2", False), ("fallback", "w3", True)]:
        claim = controller.claim_lane(root, lane_id, route=route, worker_id=worker, fallback=fallback)
        controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": claim["attempt_id"], "status": "failed", "terminal_reason": "material_failure"})

    selection = controller.resume_selection(root)
    packet = controller.restart_packet(root, interruption_reason="retry ceiling")

    assert lane_id not in selection["selected_lanes"]
    assert selection["blocked_lanes"] == [lane_id]
    assert selection["status"] == "HOLD"
    assert selection["hold_reason"] == "terminal_lanes_block_resume"
    assert packet["resume"]["status"] == "HOLD"
    assert packet["next_phase"] == "lanes"


def test_controller_rejects_completion_output_path_escape_absolute_and_symlink(tmp_path: Path) -> None:
    controller = load_controller_module()
    for unsafe in ["../outside.md", str(tmp_path / "outside.md"), "lanes/link.md"]:
        root = tmp_path / unsafe.replace("/", "_").replace(":", "_")
        _init_controller_campaign(root)
        lane_id = controller.load_state(root)["lanes"][0]["id"]
        claim = controller.claim_lane(root, lane_id, route="primary", worker_id="w1")
        if unsafe == "lanes/link.md":
            outside = tmp_path / "outside-secret.md"
            outside.write_text("secret", encoding="utf-8")
            link = root / "lanes" / "link.md"
            link.unlink(missing_ok=True)
            link.symlink_to(outside)
        else:
            (tmp_path / "outside.md").write_text("outside", encoding="utf-8")

        try:
            controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": claim["attempt_id"], "status": "completed", "output_path": unsafe})
        except ValueError as exc:
            assert "unsafe output_path" in str(exc) or "symlink" in str(exc)
        else:
            raise AssertionError(f"unsafe output accepted: {unsafe}")


def test_controller_completion_events_are_transition_checked_and_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    lane_id = controller.load_state(root)["lanes"][0]["id"]
    claim = controller.claim_lane(root, lane_id, route="primary", worker_id="w1")
    output = root / "lanes" / f"{lane_id}.md"
    output.write_text("# Lane\n", encoding="utf-8")
    event = {"lane_id": lane_id, "attempt_id": claim["attempt_id"], "status": "completed", "output_path": f"lanes/{lane_id}.md", "event_id": "evt-1"}

    first = controller.ingest_completion_event(root, event)
    duplicate = controller.ingest_completion_event(root, event)
    state = controller.load_state(root)

    assert first["status"] == "completed"
    assert duplicate["status"] == "duplicate_ignored"
    assert state["metrics"]["completion_events"] == 1
    assert sum(1 for item in state["events"] if item.get("event_id") == "evt-1") == 1

    try:
        controller.ingest_completion_event(root, {"lane_id": lane_id, "attempt_id": claim["attempt_id"], "status": "failed", "event_id": "evt-2"})
    except ValueError as exc:
        assert "invalid event transition" in str(exc)
    else:
        raise AssertionError("completed attempt accepted later failed event")


def test_controller_duplicate_claims_are_atomic_under_concurrency(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    lane_id = controller.load_state(root)["lanes"][0]["id"]

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: controller.claim_lane(root, lane_id, route="primary", worker_id=f"w{i}"), range(8)))

    claimed = [r for r in results if r["status"] == "claimed"]
    rejected = [r for r in results if r["status"] == "rejected"]
    state = controller.load_state(root)
    lane = next(item for item in state["lanes"] if item["id"] == lane_id)

    assert len(claimed) == 1
    assert len(rejected) == 7
    assert len(lane["attempts"]) == 1
    assert state["metrics"]["workers_total"] == 1


def test_controller_export_proof_requires_real_local_hash_commit_push_and_readback(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()
    internal = root / "internal" / "full-research-pack.md"
    export = tmp_path / "export"
    export.mkdir(parents=True)
    exported = export / "full-research-pack.md"
    exported.write_bytes(internal.read_bytes())
    digest = controller.sha256_path(internal)

    rejected = controller.record_export_proof(root, local_copy=True, local_commit="abc", remote_push="origin/main", remote_readback={"path": str(exported), "sha256": digest})
    assert rejected["complete"] is False
    assert "local_copy" in rejected["terminal_reason"]
    assert "local_commit" in rejected["terminal_reason"]
    assert "remote_push" in rejected["terminal_reason"]

    complete = controller.record_export_proof(
        root,
        local_copy={"staged_path": "internal/full-research-pack.md", "sha256": digest},
        local_commit={"commit": "abcdef1234567890", "ref": "refs/heads/main"},
        remote_push={"remote": "origin", "ref": "main", "commit": "abcdef1234567890"},
        remote_readback={"path": str(exported), "sha256": digest},
    )
    assert complete["complete"] is True
    assert complete["terminal_reason"] == "complete"


def test_controller_active_idle_timing_api_records_active_and_idle_seconds(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()

    controller.record_idle(root, seconds=12, reason="waiting for owner gate")
    controller.record_active(root, seconds=7, reason="synthesis")
    metrics = controller.timing_metrics(root)

    assert metrics["active_time_seconds"] == 7
    assert metrics["idle_time_seconds"] == 12
    assert metrics["active_events"] == 1
    assert metrics["idle_events"] == 1


def test_controller_repair_cycle_caps_material_failure_classes(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()

    first = controller.record_repair_cycle(root, failure_class="privacy_leak", artifact_path="external/original-full-research-pack.md", max_cycles=2)
    second = controller.record_repair_cycle(root, failure_class="privacy_leak", artifact_path="external/original-full-research-pack.md", max_cycles=2)
    third = controller.record_repair_cycle(root, failure_class="privacy_leak", artifact_path="external/original-full-research-pack.md", max_cycles=2)

    assert first["status"] == "repair_allowed"
    assert second["status"] == "repair_allowed"
    assert third["status"] == "repair_blocked"
    assert third["reason"] == "repair_cycle_cap_exceeded:privacy_leak"
    assert controller.load_state(root)["repair_cycles"]["privacy_leak"]["blocked"] is True


def test_controller_repair_cycle_defaults_to_one_cycle_per_failure_class(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    _init_controller_campaign(root)
    controller = load_controller_module()

    first = controller.record_repair_cycle(root, failure_class="citation_gap", artifact_path="internal/full-research-pack.md")
    second = controller.record_repair_cycle(root, failure_class="citation_gap", artifact_path="internal/full-research-pack.md")

    assert first["status"] == "repair_allowed"
    assert first["max_cycles"] == 1
    assert second["status"] == "repair_blocked"


# --- ISSUE-572: research-orchestration artifacts wired into the scaffold ------- #

def test_init_campaign_scaffolds_research_orchestration_block(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    run([
        sys.executable, str(INIT),
        "--target", "Example Product",
        "--target-type", "product",
        "--root", str(root),
    ], check=True)

    manifest = json.loads((root / "manifest.json").read_text())
    ro = manifest["research_orchestration"]

    assert ro["prior_work_context"]["path"] == "orchestration/prior-work-context.json"
    assert ro["playbook"]["path"] == "orchestration/implementation-operating-playbook.json"
    assert ro["improvement_record"]["path"] == "orchestration/research-improvement-record.json"

    assert ro["prior_work_context"]["required"] is True
    assert ro["playbook"]["required"] is True
    assert ro["prior_work_context"]["status"] == "not_started"
    assert ro["playbook"]["status"] == "not_started"

    assert ro["automatic_mutation_allowed"] is False
    assert ro["activation"] == "proposal_only"

    assert (root / "orchestration" / "prior-work-context.json").exists()
    assert (root / "orchestration" / "implementation-operating-playbook.json").exists()
    assert (root / "orchestration" / "research-improvement-record.json").exists()

    pwc = json.loads((root / "orchestration" / "prior-work-context.json").read_text())
    assert pwc["schema_version"] == "prior-work-context-v1"
    assert pwc["status"] in {"HOLD", "FAIL", "not_started"}


def test_verify_holds_on_scaffolded_research_orchestration(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    run([
        sys.executable, str(INIT),
        "--target", "Example Product",
        "--target-type", "product",
        "--root", str(root),
    ], check=True)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])
    assert proc.returncode == 2
    payload = json.loads(proc.stdout)
    assert payload["status"] == "HOLD"
    # The scaffolded (not_started) prior-work and playbook artifacts must HOLD.
    assert any("research_orchestration_prior_work" in h for h in payload["holds"])
    assert any("research_orchestration_playbook" in h for h in payload["holds"])


def _fill_valid_orchestration(root: Path) -> None:
    """Overwrite the scaffolded orchestration artifacts with valid content."""
    import hashlib as _hl

    def _h(value) -> str:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return _hl.sha256(payload).hexdigest()

    ctx = {
        "schema_version": "prior-work-context-v1",
        "campaign_id": "example-product",
        "query_terms": ["example product"],
        "surfaces": [
            {"surface": "linear", "required": True, "checked": True, "checked_at": "2026-09-11T00:00:00+00:00", "freshness": "live", "status": "checked", "blocker_or_exclusion": None},
            {"surface": "knowledge_base", "required": True, "checked": True, "checked_at": "2026-09-11T00:00:00+00:00", "freshness": "fresh", "status": "checked", "blocker_or_exclusion": None},
        ],
        "matches": [
            {"prior_work_id": "PW-000001", "surface": "linear", "ref": "ISSUE-458", "title": "Quality harness", "relationship": "extend_existing", "observed_state": "Done", "observed_at": "2026-09-11T00:00:00+00:00", "evidence_hash": "a" * 64, "owner": "linear://ISSUE-458", "reusable_assets": ["benchmark"], "decision_or_gate": None, "notes": None},
        ],
        "material_gaps": [],
        "context_to_use": ["benchmark"],
        "context_to_exclude": [],
        "status": "PASS",
        "generated_at": "2026-09-11T00:00:00+00:00",
    }
    playbook = {
        "schema_version": "implementation-or-operating-playbook-v1",
        "campaign_id": "example-product",
        "playbook_id": "PB-example-product",
        "target_form": "source_system_workflow",
        "input_bundle_sha256": "b" * 64,
        "prior_work_context_sha256": _h(ctx),
        "target_outcome": "Integrate the evidence-to-output assurance controller cleanly.",
        "definition_of_done": ["outputs PASS"],
        "strategy": "extend",
        "strategy_reason": "reuse existing pipeline",
        "stages": [
            {"stage_id": "S1", "name": "Wire adapter", "outcome": "adapter emits context", "claim_ids": ["CLM-001"], "prior_work_ids": ["PW-000001"], "novelty_or_duplicate_verdict": "extension", "owners": ["research"], "dependencies": [], "acceptance": ["test passes"], "rollback": "revert", "owner_gate": None, "activation_authorized": False},
        ],
        "controls": ["no auto mutation"],
        "tests": ["pytest"],
        "operations": {"runbook": ["run"], "monitoring": ["review"], "support_owner": "research", "maintenance_burden": "low"},
        "risks": [{"risk": "dup", "mitigation": "scan", "recovery": "hold"}],
        "improvement_experiment": {"baseline": "manual", "hypothesis": "reduces rework", "measures": ["rework"], "thresholds": ["decrease"], "review_trigger": "after pilot"},
        "owner_gates": ["proposal only"],
        "status": "PASS",
        "promotion_guard": {"requires_observed_evidence": True, "requires_independent_review": True, "automatic_mutation_allowed": False},
    }
    (root / "orchestration" / "prior-work-context.json").write_text(json.dumps(ctx), encoding="utf-8")
    (root / "orchestration" / "implementation-operating-playbook.json").write_text(json.dumps(playbook), encoding="utf-8")
    # Flip manifest statuses to filled.
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["research_orchestration"]["prior_work_context"]["status"] = "complete"
    manifest["research_orchestration"]["playbook"]["status"] = "complete"
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_verify_clears_research_orchestration_holds_when_filled_valid(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    run([
        sys.executable, str(INIT),
        "--target", "Example Product",
        "--target-type", "product",
        "--root", str(root),
    ], check=True)
    _fill_valid_orchestration(root)

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])
    payload = json.loads(proc.stdout)
    # Other depth holds (sources/words/visuals) may remain, but the
    # research-orchestration holds must be gone.
    assert not any("research_orchestration_prior_work" in h for h in payload["holds"]), payload["holds"]
    assert not any("research_orchestration_playbook" in h for h in payload["holds"]), payload["holds"]


def _write_frozen_bundle_block(root: Path) -> dict:
    """Build+freeze a real evidence bundle under the campaign root and return the
    manifest research_orchestration.evidence_bundle block referencing it."""
    import hashlib as _hl

    eb_spec = importlib.util.spec_from_file_location("evidence_bundle", SCRIPT_DIR / "evidence_bundle.py")
    assert eb_spec and eb_spec.loader
    eb = importlib.util.module_from_spec(eb_spec)
    sys.modules["evidence_bundle"] = eb
    eb_spec.loader.exec_module(eb)

    body = b"frozen canonical evidence\n"
    (root / "orchestration").mkdir(parents=True, exist_ok=True)
    (root / "evidence").mkdir(parents=True, exist_ok=True)
    (root / "evidence" / "e.md").write_bytes(body)
    raw = {
        "schema_version": eb.SCHEMA_VERSION,
        "campaign_id": "example-product",
        "decision_question": "adopt?",
        "target_type": "product",
        "created_at": "2026-09-11T00:00:00+00:00",
        "source_envelopes": [{"source_id": "s1", "lineage_group": "g1"}],
        "claims": [{"claim_id": "c1", "evidence_refs": ["evidence/e.md"], "evidence_class": "primary", "confidence": "high", "caveats": []}],
        "contradictions": [], "unknowns": [], "requirements": [],
        "candidate_implications": [],
        "artifact_manifest": [{"path": "evidence/e.md", "size": len(body), "sha256": _hl.sha256(body).hexdigest(), "privacy_class": "internal"}],
    }
    built = eb.build_bundle(raw, root=root)
    frozen = eb.freeze_and_verify(built, root=root)
    (root / "orchestration" / "evidence-bundle.json").write_text(json.dumps(frozen), encoding="utf-8")
    return {"required": True, "status": "complete", "path": "orchestration/evidence-bundle.json"}


def test_verify_backward_compatible_without_evidence_bundle_block(tmp_path: Path) -> None:
    """A campaign with no evidence_bundle block must be unaffected by the new gate."""
    mod = load_verify_module()
    root = tmp_path / "camp"
    root.mkdir()
    assert mod.evidence_bundle_holds(root, {}) == []
    assert mod.evidence_bundle_holds(root, {"research_orchestration": {}}) == []


def test_verify_evidence_bundle_gate_passes_for_clean_frozen_bundle(tmp_path: Path) -> None:
    mod = load_verify_module()
    root = tmp_path / "camp"
    root.mkdir()
    block = _write_frozen_bundle_block(root)
    manifest = {"research_orchestration": {"automatic_mutation_allowed": False, "evidence_bundle": block}}
    assert mod.evidence_bundle_holds(root, manifest) == []


def test_verify_evidence_bundle_gate_holds_when_not_started(tmp_path: Path) -> None:
    mod = load_verify_module()
    root = tmp_path / "camp"
    root.mkdir()
    manifest = {"research_orchestration": {"automatic_mutation_allowed": False,
                "evidence_bundle": {"required": True, "status": "not_started",
                                    "path": "orchestration/evidence-bundle.json"}}}
    holds = mod.evidence_bundle_holds(root, manifest)
    assert any("evidence_bundle" in h and "not_started" in h for h in holds), holds


def test_verify_evidence_bundle_gate_detects_toctou_tamper(tmp_path: Path) -> None:
    mod = load_verify_module()
    root = tmp_path / "camp"
    root.mkdir()
    block = _write_frozen_bundle_block(root)
    manifest = {"research_orchestration": {"automatic_mutation_allowed": False, "evidence_bundle": block}}
    # tamper a frozen manifest artifact after freeze
    (root / "evidence" / "e.md").write_bytes(b"TAMPERED\n")
    holds = mod.evidence_bundle_holds(root, manifest)
    assert any("blocked_stale_hash" in h or "byte drift" in h or "evidence/e.md" in h for h in holds), holds


def test_verify_holds_when_filled_playbook_material_stage_lacks_citations(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    run([
        sys.executable, str(INIT),
        "--target", "Example Product",
        "--target-type", "product",
        "--root", str(root),
    ], check=True)
    _fill_valid_orchestration(root)
    # Break the playbook: a material stage that cites no claim id.
    pb = json.loads((root / "orchestration" / "implementation-operating-playbook.json").read_text())
    pb["stages"][0]["claim_ids"] = []
    (root / "orchestration" / "implementation-operating-playbook.json").write_text(json.dumps(pb), encoding="utf-8")

    proc = run([sys.executable, str(VERIFY), "--campaign-root", str(root), "--mode", "true_deep_research", "--json"])
    payload = json.loads(proc.stdout)
    assert any("research_orchestration_playbook" in h for h in payload["holds"]), payload["holds"]


class stage817DeepResearchUpliftTests(unittest.TestCase):
    def test_quantitative_retention_hold_when_lane_numbers_dropped(self):
        vcd = load_verify_module()
        lane_claims = [
            {"id": "03-F1", "claim": "Starter costs A$500/month and Growth costs A$4,000/month."},
            {"id": "03-F2", "claim": "SCX reported A$8.3 million ARR and 100 commercial customers."},
            {"id": "02-F3", "claim": "Matilda Decision v1 scored 65.38 on Decision Index 0.3 across 140,178 requests."},
            {"id": "02-F4", "claim": "Maincoder-1B scored 76.22% on HumanEval and 30.54% on MMLU."},
            {"id": "03-F5", "claim": "Isaacus Embedder costs US$0.35 per million tokens."},
        ]
        thin_pack = "# Reference pack\n\nSeveral vendors offer models and hosting, but details are not established.\n"
        metrics, holds = vcd.quantitative_retention_holds(lane_claims, thin_pack, target_type="market")
        self.assertGreaterEqual(metrics["lane_quantitative_claims"], 5)
        self.assertEqual(metrics["pack_quantitative_tokens"], 0)
        self.assertIn("quantitative_evidence_dropped_in_synthesis", holds[0])

    def test_quantitative_retention_skips_qualitative_lanes(self):
        vcd = load_verify_module()
        qualitative_claims = [
            {"id": "01-F1", "claim": "The committee recommended clearer governance definitions."},
            {"id": "01-F2", "claim": "Witnesses disagreed on statutory scope."},
        ]
        pack = "# Reference pack\n\nThe committee recommended clearer governance definitions.\n"
        metrics, holds = vcd.quantitative_retention_holds(qualitative_claims, pack, target_type="policy")
        self.assertEqual(holds, [])
        self.assertEqual(metrics["lane_quantitative_claims"], 0)

    def test_load_lane_findings_ignores_unreadable_json(self):
        vcd = load_verify_module()
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "a.json"
            good.write_text(json.dumps({"findings": [{"claim": "x 5%"}, "skip"]}), encoding="utf-8")
            bad = Path(tmp) / "b.json"
            bad.write_text("{not json", encoding="utf-8")
            self.assertEqual(vcd.load_lane_findings([bad, good]), [{"claim": "x 5%"}])

    def test_export_deliverables_overwrite_approved_archives_prior_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "campaign"
            export = base / "export"
            prepare_test_export(export)
            write_depth_pass_fixture(root)
            manifest = json.loads((root / "manifest.json").read_text())
            manifest["deliverable_exports"].update({"export_root": str(export), "intended_directory": "."})
            (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            from test_acceptance_repairs import seal_test_legacy_pack_acceptance
            seal_test_legacy_pack_acceptance(root)
            auto = export
            auto.mkdir(parents=True)
            names = ["full-research-pack.md", "executive-brief.md"]
            for name in names:
                (auto / name).write_text(f"older {name}", encoding="utf-8")
            refused = run_frozen_legacy_export(root, export)
            self.assertEqual(refused.returncode, 3, refused.stdout)
            self.assertIn("owner approval required", refused.stdout)
            self.assertEqual((auto / names[0]).read_text(encoding="utf-8"), f"older {names[0]}")

            approved = run_frozen_legacy_export(root, export, "--overwrite-approved")
            self.assertEqual(approved.returncode, 0, approved.stdout)
            archive = root / "orchestration" / "prior-deliverable-exports"
            for name in names:
                self.assertEqual((archive / name).read_text(encoding="utf-8"), f"older {name}")
                self.assertNotEqual((auto / name).read_text(encoding="utf-8"), f"older {name}")
            updated = json.loads((root / "manifest.json").read_text())
            priors = updated["deliverable_exports"]["prior_exports"]
            self.assertEqual(len(priors), 2)
            self.assertTrue(updated["deliverable_exports"]["overwrite_approved"])
            self.assertEqual({Path(item["archived_path"]).name for item in priors}, set(names))
            self.assertEqual(json.loads(approved.stdout)["prior_exports"], priors)
            self.assertEqual(updated["deliverable_exports"]["status"], "copied_to_export")


if __name__ == "__main__":
    unittest.main()
