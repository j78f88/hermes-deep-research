#!/usr/bin/env python3
"""Offline QA regressions using explicitly synthetic public test data.

No fixture is a real campaign, provider response or publication evidence.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import tempfile
import unittest

try:
    import pytest
except ModuleNotFoundError:  # allow `python3 -m unittest` without pytest installed
    class _PytestStub:
        class mark:
            @staticmethod
            def parametrize(*_args, **_kwargs):
                return lambda func: func

    pytest = _PytestStub()

HERE = Path(__file__).resolve().parent
# Required checked-in data. Never discover private campaigns or read live settings.
FIXTURE_ROOT = HERE / "fixtures/public-qa"
CAMPAIGN = FIXTURE_ROOT / "synthetic-campaign"


def _require_saved_fixture() -> None:
    assert CAMPAIGN.is_dir(), "Required synthetic public QA fixture is missing"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


workflow = _load("research_qa_repair")
plain = _load("plain_brief_rewrite")


def _saved_review(generation: int) -> dict:
    _require_saved_fixture()
    if generation == 8:
        pass1 = CAMPAIGN / "orchestration/pass1-backup/qa/plain-brief-review.json"
        path = pass1 if pass1.is_file() else CAMPAIGN / "qa/plain-brief-review.json"
    else:
        path = CAMPAIGN / "receipts/qa-history" / f"plain-brief-generation{generation}" / "plain-brief-review.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _saved_gen8_brief() -> str:
    _require_saved_fixture()
    pass1 = CAMPAIGN / "orchestration/pass1-backup/internal/plain-brief.md"
    path = pass1 if pass1.is_file() else CAMPAIGN / "internal/plain-brief.md"
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("generation", [1, 2, 3, 4, 5, 6, 7])
def test_saved_defective_drafts_block_before_publication(generation: int) -> None:
    result = workflow.evaluate_editor_review(
        _saved_review(generation),
        evidence_decisions=[],
    )
    expected = "BLOCKED_EDITOR" if generation == 5 else "BLOCKED_FACTUAL_FINDINGS"
    assert result["status"] == expected
    assert result["publication_qa_allowed"] is False


def test_saved_final_draft_without_explicit_judgements_is_rejected() -> None:
    review = _saved_review(8)
    decisions = [
        {
            "note": note,
            "classification": "mistaken_reviewer" if "MC-2" in note else "optional_style",
            "evidence_refs": ["qa/packaging-review-findings.md"],
        }
        for note in review["editor_minor"]
    ]
    result = workflow.evaluate_editor_review(review, evidence_decisions=decisions)
    assert result["status"] == "BLOCKED_INVALID_DECISIONS"
    assert result["publication_qa_allowed"] is False
    assert result["requires_rewrite"] is False


def test_stale_editor_note_decisions_are_blocked() -> None:
    review = _saved_review(8)
    decisions = [
        {
            "note": note,
            "classification": "optional_style",
            "evidence_refs": ["qa/packaging-review-findings.md"],
        }
        for note in review["editor_minor"]
    ]
    result = workflow.evaluate_editor_review(
        review,
        evidence_decisions=decisions,
        decisions_brief_sha256="0" * 64,
    )
    assert result["status"] == "BLOCKED_STALE_DECISIONS"
    assert result["publication_qa_allowed"] is False


def test_incorrect_reviewer_correction_is_rejected() -> None:
    review = _saved_review(8)
    wrong = next(note for note in review["editor_minor"] if "MC-2" in note)
    decisions = [
        {
            "note": note,
            "classification": "apply_correction" if note == wrong else "optional_style",
            "evidence_refs": ["qa/packaging-review-findings.md"],
            "evidence_verdict": "unsupported" if note == wrong else "supported",
        }
        for note in review["editor_minor"]
    ]
    result = workflow.evaluate_editor_review(review, evidence_decisions=decisions)
    assert result["status"] == "BLOCKED_UNSUPPORTED_CORRECTION"
    assert wrong in result["findings"]


def test_changed_section_invalidates_only_transitive_dependants() -> None:
    graph = {
        "grant_rules": ["funding_section"],
        "funding_section": ["plain_brief"],
        "model_section": ["plain_brief"],
        "plain_brief": ["publication_qa"],
        "publication_qa": [],
    }
    result = workflow.plan_targeted_repair(
        changed_units=["grant_rules"],
        dependency_graph=graph,
        checked_units={"grant_rules", "funding_section", "model_section", "plain_brief", "publication_qa"},
    )
    assert result["recheck"] == ["funding_section", "grant_rules", "plain_brief", "publication_qa"]
    assert result["reuse"] == ["model_section"]


def test_exact_review_packet_ignores_unlisted_neighbour(tmp_path: Path) -> None:
    admitted = tmp_path / "source.md"
    unrelated = tmp_path / "finish_lane.py"
    admitted.write_text("public evidence\n", encoding="utf-8")
    unrelated.write_text("private generator\n", encoding="utf-8")

    packet = workflow.build_review_packet([admitted], root=tmp_path)
    assert [item["path"] for item in packet["files"]] == ["source.md"]
    assert "finish_lane.py" not in json.dumps(packet)

    admitted.write_text("changed evidence\n", encoding="utf-8")
    result = workflow.verify_review_packet(packet, root=tmp_path)
    assert result["status"] == "BLOCKED_CHANGED_INPUT"
    assert result["changed"] == ["source.md"]


def test_source_plan_counts_independent_origins_not_urls() -> None:
    candidates = [
        {"source_id": "vendor-card", "question_ids": ["Q1"], "lineage_group": "vendor", "independent": False},
        {"source_id": "vendor-blog", "question_ids": ["Q1"], "lineage_group": "vendor", "independent": False},
        {"source_id": "independent-test", "question_ids": ["Q1"], "lineage_group": "lab", "independent": True},
        {"source_id": "irrelevant-url", "question_ids": [], "lineage_group": "other", "independent": True},
    ]
    result = workflow.select_sources(
        unanswered_questions=["Q1"],
        candidates=candidates,
        preferred_by_question={"Q1": "vendor-card"},
    )
    assert result["selected_ids"] == ["vendor-card", "independent-test"]
    assert result["independent_origins_by_question"] == {"Q1": 1}
    assert result["unresolved_questions"] == []


def test_publication_runner_uses_adjudication_gate() -> None:
    runner = (HERE / "run_publication_reviews.py").read_text(encoding="utf-8")
    assert "campaign_readiness" in runner
    assert "publication_qa_allowed" in runner
    assert "editor-note-decisions.json" in runner


def test_plain_brief_generation_resets_note_decisions(tmp_path: Path) -> None:
    from test_acceptance_repairs import accepted

    manifest, review, decisions = accepted(tmp_path, 'Retain the supported qualification.')
    assert decisions['decisions']
    review_path = tmp_path / 'qa/plain-brief-review.json'
    saved_review = review_path.read_bytes()
    path = plain.reset_editor_note_decisions(
        tmp_path,
        brief_sha256=review['brief_sha256'],
        review_rel="qa/plain-brief-review.json",
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["status"] == "pending"
    assert record["brief_sha256"] == review['brief_sha256']
    assert record["full_pack_sha256"] == review['full_pack_sha256']
    assert record["semantic_manifest"] == workflow.semantic_manifest_snapshot(manifest)
    assert record["retained_findings"] == review['editor_minor']
    assert {item['path'] for item in record['review_inputs']['files']} >= {
        'manifest.json', 'qa/plain-brief-review.json', review['brief_path'], review['full_pack_path']}
    assert workflow.verify_review_packet(record['review_inputs'], root=tmp_path)['status'] == 'PASS'
    assert review_path.read_bytes() == saved_review
    assert record["decisions"] == []
    assert not workflow.campaign_readiness(tmp_path)['publication_qa_allowed']


def test_planned_event_becoming_certainty_is_rejected_by_section_review() -> None:
    _require_saved_fixture()
    gen6 = (CAMPAIGN / "receipts/qa-history/plain-brief-generation6/plain-brief.md").read_text(encoding="utf-8")
    gen8 = _saved_gen8_brief()
    rule = [{"unqualified": "Round 28 opens in 2027", "qualified": "Round 28 is planned to open in 2027", "required": True}]
    bad = workflow.validate_section_qualifications(gen6, required_qualifiers=rule)
    good = workflow.validate_section_qualifications(gen8, required_qualifiers=rule)
    assert bad["status"] == "BLOCKED_SECTION_QUALIFICATION"
    assert good["status"] == "PASS"


def test_repeated_wrong_qualification_detected_across_all_occurrences() -> None:
    _require_saved_fixture()
    gen6 = (CAMPAIGN / "receipts/qa-history/plain-brief-generation6/plain-brief.md").read_text(encoding="utf-8")
    gen8 = _saved_gen8_brief()
    phrase = "despite attempts across its advertised pages"
    found6 = workflow.find_phrase_occurrences({"brief": gen6}, [phrase])
    found8 = workflow.find_phrase_occurrences({"brief": gen8}, [phrase])
    assert found6["status"] == "BLOCKED_REMAINING_OCCURRENCES"
    assert found6["occurrence_count"] >= 1
    assert found8["status"] == "PASS"


def test_grant_rules_unit_keeps_project_limits_and_conflicting_rules_distinct() -> None:
    bad = workflow.validate_grant_rules_unit({
        "grant_id": "seed-start",
        "amount": "$50k-$500k",
        "application_status": "EOI open; full application by invitation",
        "programme_period": "ongoing",
        "project_limit": "up to 2-3 years",
        "official_versions": [
            {"source": "SA guidelines", "financial_test": "average annual net sales under $2m over 3 years"},
            {"source": "federal directory", "financial_test": "turnover under $1m in each of 3 years"},
        ],
        "conflict_unresolved": True,
        "applicant_reliance_warning": "",
    })
    assert bad["status"] == "BLOCKED_GRANT_UNIT"
    good = workflow.validate_grant_rules_unit({
        "grant_id": "seed-start",
        "amount": "$50k-$500k",
        "application_status": "EOI open; full application by invitation",
        "programme_period": "ongoing",
        "project_limit": "up to 2-3 years",
        "official_versions": [
            {"source": "SA guidelines", "financial_test": "average annual net sales under $2m over 3 years"},
            {"source": "federal directory", "financial_test": "turnover under $1m in each of 3 years"},
        ],
        "conflict_unresolved": True,
        "applicant_reliance_warning": "Confirm with the programme owner before relying on either rule.",
    })
    assert good["status"] == "PASS"


def test_append_only_source_index_and_resume_command_guards() -> None:
    _require_saved_fixture()
    proof = json.loads((CAMPAIGN / "receipts/source-index-reconciliation/append-only-proof.json").read_text(encoding="utf-8"))
    index_bytes = (CAMPAIGN / "workspace/sources/02-model-lineage/index.jsonl").read_bytes()
    verified = workflow.verify_append_only_index(
        index_bytes=index_bytes,
        expected_original_sha256=proof["original_index_sha256"],
        original_prefix_bytes=proof["original_prefix_bytes"],
    )
    assert verified["status"] == "PASS"
    assert verified["current_sha256"] == proof["current_index_sha256"]

    bad_cmd = workflow.validate_worker_cli_command(["hermes", "chat", "--resume", "abc", "--max-turns", "24", "--run-budget", "600"])
    good_cmd = workflow.validate_worker_cli_command(["hermes", "chat", "--resume", "abc"])
    assert bad_cmd["status"] == "BLOCKED_RESUME_FLAGS"
    assert bad_cmd["forbidden_flags"] == ["--max-turns", "--run-budget"]
    assert good_cmd["status"] == "PASS"


class ResearchQaRepairstage817Tests(unittest.TestCase):
    def test_validate_reference_sources_section_rejects_duplicate_heading(self):
        bad_pack = "# Pack\n\n## Sources\n\n## Sources\n1. [Example](https://example.invalid)\n"
        res = workflow.validate_reference_sources_section(bad_pack)
        self.assertEqual(res["status"], "BLOCKED_DUPLICATE_SOURCES_HEADING")
        self.assertEqual(workflow.validate_reference_sources_section("# Pack\n")["status"], "PASS")
        self.assertEqual(workflow.validate_reference_sources_section("# Pack\n\n## Sources\n1. x\n")["status"], "PASS")

    def test_prepare_preexport_assurance_stub_resets_stale_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "qa").mkdir()
            target = root / "qa" / "final-assurance.json"
            target.write_text(json.dumps({
                "schema_version": "deep-research-final-assurance-v1",
                "status": "PASS",
                "reviewer": "independent-reviewer",
                "producers": ["controller"],
                "reference_pack_sha256": "old-pack",
                "brief_sha256": "old-brief",
            }), encoding="utf-8")
            res = workflow.prepare_preexport_assurance_stub(root, brief_sha256="new-brief", pack_sha256="new-pack")
            self.assertEqual(res["status"], "RESET")
            written = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(written["status"], "PENDING")
            self.assertEqual(written["reference_pack_sha256"], "new-pack")
            self.assertEqual(written["brief_sha256"], "new-brief")
            again = workflow.prepare_preexport_assurance_stub(root, brief_sha256="new-brief", pack_sha256="new-pack")
            self.assertEqual(again["status"], "UNCHANGED")


def _repaired_decisions() -> dict:
    path = HERE / "fixtures/sovereign-decisions-repaired.json"
    assert path.is_file(), "Required synthetic decisions fixture is missing"
    return json.loads(path.read_text(encoding="utf-8"))


def _replay_fixture(tmp_path: Path) -> Path:
    import shutil

    _require_saved_fixture()
    root = tmp_path / "campaign"
    for rel in ("orchestration/pass1-backup/qa/plain-brief-review.json",
                "orchestration/pass1-backup/internal/plain-brief.md",
                "orchestration/pass1-backup/qa/packaging-review-findings.md",
                "qa/plain-brief-review.json", "internal/plain-brief.md"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CAMPAIGN / rel, target)
    shutil.copytree(CAMPAIGN / "receipts/qa-history", root / "receipts/qa-history")
    return root


@pytest.mark.parametrize("entry_point", ["research_qa_repair.py", "replay_research_qa.py"])
def test_replay_cli_selects_preserved_pass1_not_refreshed_pass2(entry_point: str) -> None:
    import subprocess

    _require_saved_fixture()
    command = [sys.executable, str(HERE / entry_point)]
    if entry_point == "research_qa_repair.py":
        command.append("--replay-saved")
    proc = subprocess.run([*command, "--campaign-root", str(CAMPAIGN)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "FAIL"
    assert result["historical_selection"] == "preserved_pass1"
    assert len(result["rows"]) == 9
    assert all(row["pass"] for row in result["rows"] if row["generation"] != 8)
    assert result["rows"][7]["actual"] == "BLOCKED_INVALID_DECISIONS"
    assert result["rows"][-1]["actual"] == "BLOCKED_UNSUPPORTED_CORRECTION"
    assert all(path.startswith("orchestration/pass1-backup/") for path in result["selected_inputs"].values())
    assert workflow.verify_review_packet(result["input_packet"], root=CAMPAIGN)["status"] == "PASS"
    current = json.loads((CAMPAIGN / "qa/plain-brief-review.json").read_text())
    assert not any("MC-2" in note for note in current["editor_minor"])


def test_replay_ignores_modified_pass2_readers(tmp_path: Path) -> None:
    root = _replay_fixture(tmp_path)
    (root / "qa/plain-brief-review.json").write_text("not a historical review")
    (root / "internal/plain-brief.md").write_text("not a historical reader")
    result = workflow.replay_saved(root)
    assert result["status"] == "FAIL"
    assert result["rows"][7]["actual"] == "BLOCKED_INVALID_DECISIONS"


def test_replay_incomplete_pass1_does_not_mix_in_pass2(tmp_path: Path) -> None:
    root = _replay_fixture(tmp_path)
    brief = root / "orchestration/pass1-backup/internal/plain-brief.md"
    brief.rename(brief.with_suffix(".held"))
    result = workflow.replay_saved(root)
    assert result["status"] == "BLOCKED_HISTORICAL_INPUTS"
    assert result["historical_selection"] == "preserved_pass1"
    assert result["rows"] == []
    assert any("missing historical input" in finding for finding in result["findings"])


def test_replay_missing_mc2_returns_explicit_block_not_stopiteration(tmp_path: Path) -> None:
    import subprocess

    root = _replay_fixture(tmp_path)
    review_path = root / "orchestration/pass1-backup/qa/plain-brief-review.json"
    review = json.loads(review_path.read_text())
    review["editor_minor"] = [note for note in review["editor_minor"] if "MC-2" not in note]
    review_path.write_text(json.dumps(review))
    proc = subprocess.run([sys.executable, str(HERE / "research_qa_repair.py"), "--replay-saved",
                           "--campaign-root", str(root)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1
    assert "StopIteration" not in proc.stderr
    result = json.loads(proc.stdout)
    assert result["status"] == "BLOCKED_HISTORICAL_INPUTS"
    assert "MC-2" in result["findings"][0]


def test_replay_tampered_pass1_reader_fails_closed(tmp_path: Path) -> None:
    root = _replay_fixture(tmp_path)
    (root / "orchestration/pass1-backup/internal/plain-brief.md").write_text("changed historical bytes")
    result = workflow.replay_saved(root)
    assert result["status"] == "BLOCKED_HISTORICAL_INPUTS"
    assert "does not match" in result["findings"][0]


def test_replay_original_unrefresh_campaign_is_still_supported(tmp_path: Path) -> None:
    import shutil

    _require_saved_fixture()
    root = tmp_path / "original"
    original = CAMPAIGN / "orchestration/pass1-backup"
    for rel in ("qa/plain-brief-review.json", "qa/packaging-review-findings.md", "internal/plain-brief.md"):
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(original / rel, target)
    shutil.copytree(CAMPAIGN / "receipts/qa-history", root / "receipts/qa-history")
    result = workflow.replay_saved(root)
    assert result["status"] == "FAIL"
    assert result["historical_selection"] == "original_final"


def test_synthetic_legacy_decisions_still_fail_closed() -> None:
    _require_saved_fixture()
    review = json.loads((CAMPAIGN / "qa/plain-brief-review.json").read_text())
    original = json.loads((CAMPAIGN / "qa/editor-note-decisions.json").read_text())
    result = workflow.evaluate_editor_review(review, evidence_decisions=original["decisions"],
                                             decisions_brief_sha256=original["brief_sha256"])
    assert not result["publication_qa_allowed"]
    assert result["findings"]
    assert "style_only_non_factual" not in workflow.DECISION_CLASSES


def test_synthetic_repaired_decisions_have_exact_source_and_reader_bindings() -> None:
    import hashlib

    _require_saved_fixture()
    review = json.loads((CAMPAIGN / "qa/plain-brief-review.json").read_text())
    original = json.loads((CAMPAIGN / "qa/editor-note-decisions.json").read_text())
    repaired = _repaired_decisions()
    # Fixed synthetic bytes must exercise every binding assertion, never exit early.
    assert repaired["brief_sha256"] == review["brief_sha256"]
    assert repaired["full_pack_sha256"] == review["full_pack_sha256"]
    assert [item["note"] for item in repaired["decisions"]] == [item["note"] for item in original["decisions"]]
    assert len(repaired["decisions"]) == 5
    for decision in repaired["decisions"]:
        assert decision["classification"] == "optional_style"
        assert decision["evidence_refs"]
        assert set(decision["evidence_refs"]) == {binding["path"] for binding in decision["evidence_bindings"]}
        source_bindings = [binding for binding in decision["evidence_bindings"] if binding["path"].startswith("workspace/sources/")]
        assert source_bindings
        for binding in decision["evidence_bindings"]:
            path = CAMPAIGN / binding["path"]
            assert path.resolve().is_relative_to(CAMPAIGN.resolve())
            assert hashlib.sha256(path.read_bytes()).hexdigest() == binding["sha256"]
        for binding in source_bindings:
            index = CAMPAIGN / Path(binding["path"]).parent / "index.jsonl"
            assert any(row.get("file") == binding["path"] and row.get("sha256") == binding["sha256"]
                       and row.get("url") == binding["url"] for row in map(json.loads, index.read_text().splitlines()))
    result = workflow.evaluate_editor_review(review, evidence_decisions=repaired["decisions"],
                                             decisions_brief_sha256=repaired["brief_sha256"],
                                             reader_text=(CAMPAIGN / review["brief_path"]).read_text())
    assert result["status"] == "BLOCKED_INVALID_DECISIONS"
    assert result["findings"]
    assert len(result["decisions"]) == 5
    assert result["requires_rewrite"] is False


@pytest.mark.parametrize("decision_index", range(5))
def test_supported_style_without_evidence_is_not_a_repair(decision_index: int) -> None:
    repaired = _repaired_decisions()
    repaired["decisions"][decision_index]["evidence_refs"] = []
    _require_saved_fixture()
    review = {"status": "PASS", "editor_minor": [item["note"] for item in repaired["decisions"]]}
    result = workflow.evaluate_editor_review(review, evidence_decisions=repaired["decisions"])
    assert result["status"] == "BLOCKED_INVALID_DECISIONS"
    assert repaired["decisions"][decision_index]["note"] in result["findings"]


def test_repaired_decisions_reject_a_newer_synthetic_reader() -> None:
    import hashlib

    repaired = _repaired_decisions()
    review = json.loads((CAMPAIGN / "qa/plain-brief-review.json").read_text())
    review["brief_sha256"] = hashlib.sha256(b"A newer synthetic reader.\n").hexdigest()
    result = workflow.evaluate_editor_review(
        review, evidence_decisions=repaired["decisions"],
        decisions_brief_sha256=repaired["brief_sha256"],
    )
    assert result["status"] == "BLOCKED_STALE_DECISIONS"
    assert not result["publication_qa_allowed"]


def test_required_campaign_fixture_fails_instead_of_skipping(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "CAMPAIGN", tmp_path / "absent")
    with pytest.raises(AssertionError, match="Required synthetic public QA fixture"):
        _saved_review(8)


def test_required_decisions_fixture_fails_instead_of_skipping(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "HERE", tmp_path)
    with pytest.raises(AssertionError, match="Required synthetic decisions fixture"):
        _repaired_decisions()


def test_public_fixture_records_are_explicitly_synthetic() -> None:
    records = list(CAMPAIGN.rglob("*.json"))
    assert records
    for path in records:
        record = json.loads(path.read_text())
        assert record["fixture_kind"] == "synthetic_public_regression", path
        assert record["provider_evidence"] is False, path
        assert record["publication_approval"] is False, path
    repaired = _repaired_decisions()
    assert repaired["fixture_kind"] == "synthetic_public_regression"
    assert repaired["provider_evidence"] is False
    assert repaired["publication_approval"] is False
    for path in CAMPAIGN.rglob("*.md"):
        assert "SYNTHETIC REGRESSION DATA" in path.read_text(), path
    for decision in repaired["decisions"]:
        assert all(binding.get("url", "https://example.invalid/").startswith("https://example.invalid/")
                   for binding in decision["evidence_bindings"])


def test_repaired_provenance_hash_binds_the_synthetic_original() -> None:
    import hashlib

    repaired = _repaired_decisions()
    original = CAMPAIGN / "qa/editor-note-decisions.json"
    assert repaired["repair_provenance"]["source_decisions_sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert repaired["repair_provenance"]["publication_approval"] is False


def test_public_fixture_generation_is_byte_reproducible(tmp_path: Path) -> None:
    import shutil
    import subprocess

    # Copy only the generator. It must regenerate every saved byte without history.
    generator = tmp_path / "public-qa/generate.py"
    generator.parent.mkdir()
    shutil.copy2(FIXTURE_ROOT / "generate.py", generator)
    proc = subprocess.run([sys.executable, str(generator)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    expected = {path.relative_to(CAMPAIGN): path.read_bytes()
                for path in CAMPAIGN.rglob("*") if path.is_file()}
    actual_root = generator.parent / "synthetic-campaign"
    actual = {path.relative_to(actual_root): path.read_bytes()
              for path in actual_root.rglob("*") if path.is_file()}
    assert actual == expected
    assert (tmp_path / "sovereign-decisions-repaired.json").read_bytes() == (
        HERE / "fixtures/sovereign-decisions-repaired.json").read_bytes()


def test_modified_original_index_prefix_is_rejected() -> None:
    proof = json.loads((CAMPAIGN / "receipts/source-index-reconciliation/append-only-proof.json").read_text())
    original = (CAMPAIGN / "workspace/sources/02-model-lineage/index.jsonl").read_bytes()
    modified = b"!" + original[1:]
    result = workflow.verify_append_only_index(
        index_bytes=modified, expected_original_sha256=proof["original_index_sha256"],
        original_prefix_bytes=proof["original_prefix_bytes"],
    )
    assert result["status"] == "BLOCKED_INDEX_DRIFT"
    assert result["original_prefix_matches"] is False


def test_appending_a_synthetic_index_row_preserves_original_prefix() -> None:
    import hashlib

    proof = json.loads((CAMPAIGN / "receipts/source-index-reconciliation/append-only-proof.json").read_text())
    original = (CAMPAIGN / "workspace/sources/02-model-lineage/index.jsonl").read_bytes()
    appended = original + b'{"fixture_kind":"synthetic_public_regression","file":"additional.md"}\n'
    result = workflow.verify_append_only_index(
        index_bytes=appended, expected_original_sha256=proof["original_index_sha256"],
        original_prefix_bytes=proof["original_prefix_bytes"],
    )
    assert result["status"] == "PASS"
    assert result["current_sha256"] == hashlib.sha256(appended).hexdigest()
    assert result["current_sha256"] != proof["current_index_sha256"]


@pytest.mark.parametrize("rel", [
    "orchestration/pass1-backup/qa/plain-brief-review.json",
    "orchestration/pass1-backup/qa/packaging-review-findings.md",
])
def test_replay_packet_detects_changed_selected_inputs(tmp_path: Path, rel: str) -> None:
    root = _replay_fixture(tmp_path)
    result = workflow.replay_saved(root)
    assert workflow.verify_review_packet(result["input_packet"], root=root)["status"] == "PASS"
    (root / rel).write_text("Changed synthetic fixture bytes.\n")
    verification = workflow.verify_review_packet(result["input_packet"], root=root)
    assert verification["status"] == "BLOCKED_CHANGED_INPUT"
    assert verification["changed"] == [rel]


@pytest.mark.parametrize("rel", ["internal/plain-brief.md", "internal/reference-pack.md",
                                     "source.md", "qa/disposition.json"])
def test_current_synthetic_acceptance_rejects_bound_file_drift(tmp_path: Path, rel: str) -> None:
    from test_acceptance_repairs import accepted

    _, review, decisions = accepted(tmp_path, "Retain the supported qualification.")
    assert workflow.validate_saved_review_bindings(tmp_path, review, decisions)["status"] == "PASS"
    assert workflow.campaign_readiness(tmp_path)["publication_qa_allowed"] is True
    (tmp_path / rel).write_text("Changed synthetic acceptance input.\n")
    result = workflow.validate_saved_review_bindings(tmp_path, review, decisions)
    assert result["status"] == "BLOCKED_CHANGED_INPUT"
    assert result["findings"]
    assert workflow.campaign_readiness(tmp_path)["publication_qa_allowed"] is False


def test_current_synthetic_served_judgement_is_a_positive_control(tmp_path: Path) -> None:
    from test_acceptance_repairs import accepted

    _, review, decisions = accepted(tmp_path, "Retain the supported qualification.")
    bound = workflow.validate_saved_review_bindings(tmp_path, review, decisions)
    assert bound["status"] == "PASS"
    result = workflow.evaluate_editor_review(
        review, evidence_decisions=decisions["decisions"],
        decisions_brief_sha256=decisions["brief_sha256"],
        reader_text=(tmp_path / review["brief_path"]).read_text(),
        classification_checks=bound["classification_checks"],
    )
    assert result["status"] == "PASS"
    assert result["publication_qa_allowed"] is True
    assert result["requires_rewrite"] is False
    assert workflow.campaign_readiness(tmp_path)["status"] == "PASS"


if __name__ == "__main__":
    unittest.main()
