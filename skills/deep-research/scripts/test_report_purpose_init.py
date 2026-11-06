"""Offline campaign-creation regressions for explicit, topic-independent purpose."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

SCRIPTS = Path(__file__).resolve().parent
INIT = SCRIPTS / "init_campaign.py"
STYLE_REFERENCE = "references/reader-pair-style.md"


def run_init(root: Path, *extra: str, target_type: str = "idea") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(INIT), "--target", "Example research", "--target-type", target_type,
         "--root", str(root), "--date", "2026-11-01", "--evidence-cutoff", "2026-11-01",
         "--no-deliverable-export", *extra],
        capture_output=True, text=True, timeout=30,
    )


def scaffold(root: Path, *extra: str, target_type: str = "idea") -> dict:
    result = run_init(root, *extra, target_type=target_type)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads((root / "manifest.json").read_text())


@pytest.mark.parametrize("target_type", ["idea", "company", "market", "product", "architecture", "policy"])
def test_new_campaign_defaults_to_factual_independently_of_topic(tmp_path: Path, target_type: str) -> None:
    manifest = scaffold(tmp_path / "campaign", target_type=target_type)
    assert manifest.get("report_purpose") == "factual"


@pytest.mark.parametrize("purpose", ["factual", "decision_assessment"])
def test_explicit_supported_report_purpose_is_preserved(tmp_path: Path, purpose: str) -> None:
    manifest = scaffold(tmp_path / "campaign", "--report-purpose", purpose)
    assert manifest["report_purpose"] == purpose


@pytest.mark.parametrize("purpose", ["business_assessment", "evidence_reuse_only", "", "FACTUAL", "unknown"])
def test_unsupported_purpose_is_rejected_before_creating_campaign(tmp_path: Path, purpose: str) -> None:
    root = tmp_path / "campaign"
    result = run_init(root, "--report-purpose", purpose)
    assert result.returncode == 2
    assert "report-purpose" in result.stderr or "report_purpose" in result.stderr
    assert not root.exists()


def test_evidence_reuse_only_assessment_mode_remains_unsupported(tmp_path: Path) -> None:
    root = tmp_path / "campaign"
    result = run_init(root, "--report-purpose", "decision_assessment", "--evidence-reuse-only")
    assert result.returncode == 2
    assert not root.exists()


def test_ordinary_default_question_does_not_request_a_commercial_recommendation(tmp_path: Path) -> None:
    manifest = scaffold(tmp_path / "campaign", target_type="company")
    question = manifest["decision_question"].lower()
    assert "example research" in question
    assert not any(word in question for word in ("recommend", "build /", "buy /", "partner", "kill", "defer"))


@pytest.mark.parametrize("layout", ["v2", "legacy"])
def test_explicit_question_is_not_rewritten_based_on_topic_or_layout(tmp_path: Path, layout: str) -> None:
    question = "What changed in the published pricing between 2024 and 2026?"
    manifest = scaffold(tmp_path / "campaign", "--output-layout", layout, "--decision-question", question, target_type="product")
    assert manifest["decision_question"] == question
    assert manifest.get("report_purpose") == "factual"


def load_init():
    spec = importlib.util.spec_from_file_location("purpose_init_under_test", INIT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def creation_args(purpose: str = "factual", layout: str = "brief-and-reference-v2") -> SimpleNamespace:
    return SimpleNamespace(
        target="Example architecture", target_type="architecture", primary_source="https://example.org",
        decision_question="How does this architecture work?", date="2026-11-01",
        evidence_cutoff="2026-11-01", audience="the owner", output_boundary="local research",
        report_purpose=purpose, output_layout=layout,
    )


@pytest.mark.parametrize("layout", ["brief-and-reference-v2", "legacy"])
@pytest.mark.parametrize("generator", ["parent_body", "child_body", "worker_packet", "steering"])
@pytest.mark.parametrize("purpose", ["factual", "decision_assessment"])
def test_generated_instructions_declare_purpose_and_use_shared_style_reference(generator: str, layout: str, purpose: str) -> None:
    init = load_init()
    args = creation_args(purpose, layout)
    lanes = init.lane_plan(args.target_type)
    text = getattr(init, generator)(args, lanes[0] if generator in {"child_body", "worker_packet"} else lanes, Path("campaign"))
    assert f"Report purpose: `{purpose}`" in text
    assert STYLE_REFERENCE in text


@pytest.mark.parametrize("generator", ["reference_pack", "full_pack", "board_brief", "external_pack", "internal_full_pack", "internal_executive_brief"])
def test_factual_reader_scaffold_has_no_mandatory_decision_chapters(generator: str) -> None:
    text = getattr(load_init(), generator)(creation_args()).lower()
    for decision_chapter in (
        "## executive decision", "## final recommendation", "## reconciled recommendation",
        "## strategic option portfolio", "## what would change the recommendation",
        "## executive risk register", "## what the owner needs to decide", "## options compared",
        "## risks and stop", "decision portfolio / option matrix",
    ):
        assert decision_chapter not in text
    assert "## answer" in text
    assert "## sources" in text


def test_factual_reference_pack_is_question_led_instead_of_requiring_irrelevant_modules() -> None:
    text = load_init().reference_pack(creation_args())
    assert "## Findings by research question" in text
    assert "## Limits and gaps" in text
    assert "## Build-now" not in text
    assert "## Catalogue coverage and platform features" not in text
    assert "## Fit against the stated decision context" not in text


def test_factual_parent_does_not_require_commercial_recommendation_for_completion() -> None:
    init = load_init()
    args = creation_args()
    text = init.parent_body(args, init.lane_plan(args.target_type), Path("campaign"))
    assert "Final recommendation states build / borrow / buy / partner / watch / kill / defer" not in text
    assert "## Research question" in text


def test_requested_technical_assessment_scaffold_keeps_judgement_distinct_from_evidence() -> None:
    text = load_init().reference_pack(creation_args("decision_assessment"))
    assert "## Technical judgement" in text
    assert "## Alternatives and trade-offs" in text
    assert "genuine research" in text.lower()
    assert "build / borrow / buy / partner / watch / kill / defer" not in text


def test_v2_manifest_brief_instructions_use_shared_style_without_mandatory_decision_sections() -> None:
    brief = load_init().v2_manifest_outputs("internal/plain-brief.md")["plain_brief"]
    assert brief.get("style_reference") == STYLE_REFERENCE
    assert "after QA" in brief["run_after"]
    assert not any(word in json.dumps(brief).lower() for word in ("options compared", "what owner needs to decide", "risks and stop"))


@pytest.mark.parametrize("purpose", ["factual", "decision_assessment"])
def test_qa_instructions_review_the_declared_purpose_with_the_shared_reference(purpose: str) -> None:
    text = load_init().qa_packet("evidence-privacy", "Verify evidence fidelity.", creation_args(purpose))
    assert f"Report purpose: `{purpose}`" in text
    assert STYLE_REFERENCE in text


@pytest.mark.parametrize("layout", ["brief-and-reference-v2", "legacy"])
def test_factual_visual_placeholders_do_not_require_option_selection(layout: str) -> None:
    ledger = load_init().visual_exhibits(creation_args(layout=layout))
    titles = " ".join(exhibit["title"].lower() for exhibit in ledger["exhibits"])
    assert "option matrix" not in titles
    assert "decision portfolio" not in titles
    assert "evidence" in titles
    assert ledger["thresholds"]["full_pack_min_exhibits"] == 3


@pytest.mark.parametrize("purpose", ["factual", "decision_assessment"])
def test_lane_synthesis_and_verification_names_match_purpose(purpose: str) -> None:
    lanes = load_init().lane_plan("architecture", report_purpose=purpose)
    names = " ".join(lane["name"].lower() for lane in lanes)
    assert "board brief" not in names
    assert "reader pair" in names


def test_file_import_callers_without_purpose_get_factual_instructions() -> None:
    init = load_init()
    args = creation_args()
    del args.report_purpose
    text = init.worker_packet(args, init.lane_plan("idea")[0], Path("campaign"))
    assert "Report purpose: `factual`" in text


@pytest.mark.parametrize("purpose", ["factual", "decision_assessment"])
def test_lane_json_carries_campaign_purpose(purpose: str) -> None:
    init = load_init()
    payload = init.lane_json_placeholder(creation_args(purpose), init.lane_plan("idea")[0], Path("campaign"))
    assert payload["campaign"].get("report_purpose") == purpose


def test_worker_asset_selection_guidance_only_applies_to_requested_assessment() -> None:
    init = load_init()
    text = init.worker_packet(creation_args(), init.lane_plan("idea")[0], Path("campaign"))
    assert "Only for a requested `decision_assessment`" in text
