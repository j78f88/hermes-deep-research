"""Round 7 review: the first-answer fallback."""

import json
import string
from pathlib import Path

import pytest

import deepest_research as dr
import fast_prompts as P
from fast_research import PhaseError
from test_deepest_research import controller
from test_review_round import ARCHIVED, ReviewFake


class SynthesisStopFake(ReviewFake):
    def __init__(self, round_number, mode):
        super().__init__(verdict="accept_with_notes")
        self.stop_round = round_number
        self.stop_mode = mode

    def oneshot(self, model, prompt, **kwargs):
        res = super().oneshot(model, prompt, **kwargs)
        if prompt.startswith("You write one combined") and len(self.synthesis_prompts) >= self.stop_round:
            if self.stop_mode == "unknown_id":
                res.stdout += " Invented.[C9999]"
            elif self.stop_mode == "empty":
                res.stdout = ""
        return res


def test_failed_partial_archive_is_not_a_stopped_second_round(tmp_path, monkeypatch):
    """An archive I/O error must not advertise a half-moved first answer as a fallback."""
    original = Path.replace

    def fail_after_answer_move(source, target):
        if target.parent.name == "round1" and target.name == "evidence.md":
            raise OSError("fixture archive I/O error")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", fail_after_answer_move)
    fake = ReviewFake(verdict="accept_with_notes")
    fr = controller(tmp_path, fake)
    rec = fr.run()
    report = {"status": rec["status"], "answer_path": rec["answer_path"],
              "readiness": rec["readiness"], "syntheses": fake.count("synthesis"),
              "archive_status": fr.store.phase(fr.run_id, "review_round_archive")["status"],
              "archived_evidence_exists": fr.path("round1", "evidence.md").is_file(),
              "archived_review_exists": fr.path("round1", "review.json").is_file()}
    print("PARTIAL_ARCHIVE", json.dumps(report))
    assert rec["status"] == "failed"
    assert report["syntheses"] == 1 and report["archive_status"] == "failed"
    assert rec["answer_path"] is None, report
    assert "second round stopped" not in rec["readiness"], report



def test_fallback_cli_points_at_the_archived_review(tmp_path, monkeypatch, capsys):
    fake = SynthesisStopFake(2, "unknown_id")
    original = dr.DeepestResearch
    monkeypatch.setattr(dr, "DeepestResearch", lambda *args, **kwargs: original(
        *args, **kwargs, **fake.hooks()))
    result = dr.main(["run", "--question", "Question?", "--root", str(tmp_path), "--run-id", "fallback-cli"])
    output = capsys.readouterr().out
    summary = json.loads("{" + output.rsplit("\n{", 1)[1])
    assert result == 2 and summary["status"] == "stopped"
    assert Path(summary["answer_path"]).is_file()
    assert "first-round answer" in summary["readiness"]
    print("CLI_REVIEW", json.dumps({"review_path": summary["review_path"],
                                    "exists": Path(summary["review_path"]).exists()}))
    assert Path(summary["review_path"]).is_file()


