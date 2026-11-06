"""the blind check failed, so grades are saved but hidden from the answer by default."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fast_research import FastResearch, load_config  # noqa: E402
from fast_test_support import FakeHermes, small_config  # noqa: E402
from test_fast_sentence_check import SEARCH  # noqa: E402


def test_default_config_hides_grades():
    assert load_config()["grades"]["show_in_answer"] is False


def test_hidden_grades_are_still_saved(tmp_path):
    cfg = small_config()
    cfg["grades"] = {"show_in_answer": False}
    fake = FakeHermes(SEARCH)
    fr = FastResearch(tmp_path, "How does the tool run deep research on Linux?", run_id="hidden", config=cfg, **fake.hooks())
    rec = fr.run()
    assert rec["status"] == "completed"
    answer = fr.path("answer.md").read_text()
    assert "Source grade" not in answer and "(grade " not in answer and "Fact grades" not in answer
    assert "Citation recall:" in answer
    assert fr._read_json("grades.json")["counts"]
