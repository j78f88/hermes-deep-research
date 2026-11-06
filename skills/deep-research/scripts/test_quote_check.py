#!/usr/bin/env python3
"""a cited lane finding without a saved, verbatim, hash-matching quote fails the run."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("quote_check", SCRIPT_DIR / "quote_check.py")
assert spec is not None and spec.loader is not None
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)

SOURCE_TEXT = "tmux keeps sessions alive.\nEach session is **persistent** and will survive accidental disconnection.\n"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pack(tmp_path: Path, findings: list[dict] | None = None, *, index: bool = True) -> Path:
    root = tmp_path / "campaign"
    src = root / "workspace" / "sources" / "02-current-state"
    src.mkdir(parents=True)
    (src / "tmux-man.txt").write_text(SOURCE_TEXT, encoding="utf-8")
    if index:
        row = {"id": "tmux-man", "url": "https://man.openbsd.org/tmux.1", "file": "workspace/sources/02-current-state/tmux-man.txt",
               "sha256": sha(src / "tmux-man.txt")}
        (src / "index.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (root / "lanes").mkdir()
    findings = findings if findings is not None else [{
        "id": "F1", "claim": "tmux sessions survive disconnection.", "evidence_class": "reported", "confidence": "high",
        "source_ids": ["tmux-man"], "quote": "Each session is persistent and will survive accidental disconnection",
        "source_file": "tmux-man.txt"}]
    (root / "lanes" / "02-current-state.json").write_text(json.dumps({"findings": findings}), encoding="utf-8")
    return root


def kinds(result: dict) -> set[str]:
    return {p["kind"] for p in result["problems"]}


def test_good_pack_passes(tmp_path: Path) -> None:
    result = qc.evaluate(pack(tmp_path))
    assert (result["verdict"], result["quoted"], result["findings"]) == ("PASS", 1, 1)


def test_missing_quote_fails(tmp_path: Path) -> None:
    root = pack(tmp_path)
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0].pop("quote")
    lane.write_text(json.dumps(data))
    result = qc.evaluate(root)
    assert (result["verdict"], result["class"]) == ("FAIL", "quote_check_failed")
    assert kinds(result) == {"missing_quote"}


def test_altered_quote_fails(tmp_path: Path) -> None:
    root = pack(tmp_path)
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["quote"] = "Each session is persistent and will survive a host reboot"
    lane.write_text(json.dumps(data))
    assert kinds(qc.evaluate(root)) == {"quote_not_found"}


def test_changed_source_file_fails(tmp_path: Path) -> None:
    root = pack(tmp_path)
    src = root / "workspace" / "sources" / "02-current-state" / "tmux-man.txt"
    src.write_text(SOURCE_TEXT + "Edited after it was saved.\n", encoding="utf-8")
    assert kinds(qc.evaluate(root)) == {"source_changed"}


def test_source_missing_from_index_fails(tmp_path: Path) -> None:
    root = pack(tmp_path)
    other = root / "workspace" / "sources" / "02-current-state" / "unindexed.txt"
    other.write_text(SOURCE_TEXT, encoding="utf-8")
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["source_file"] = "unindexed.txt"
    lane.write_text(json.dumps(data))
    assert kinds(qc.evaluate(root)) == {"source_not_indexed"}


def test_symlinked_source_outside_root_fails(tmp_path: Path) -> None:
    root = pack(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(SOURCE_TEXT, encoding="utf-8")
    link = root / "workspace" / "sources" / "02-current-state" / "link.txt"
    os.symlink(outside, link)
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["source_file"] = "link.txt"
    lane.write_text(json.dumps(data))
    assert kinds(qc.evaluate(root)) == {"missing_source_file"}


def test_uncited_fact_fails_but_labelled_inference_is_allowed(tmp_path: Path) -> None:
    findings = [{"id": "F2", "claim": "tmux is the best choice.", "evidence_class": "reported", "source_ids": []},
                {"id": "F3", "claim": "tmux fits the home lab.", "evidence_class": "analyst_inference", "source_ids": []}]
    result = qc.evaluate(pack(tmp_path, findings))
    assert [(p["id"], p["kind"]) for p in result["problems"]] == [("F2", "uncited_fact")]
    assert result["inference_or_gap"] == 1


def test_quote_matching_ignores_markdown_case_and_curly_quotes(tmp_path: Path) -> None:
    root = pack(tmp_path)
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["quote"] = "EACH SESSION IS PERSISTENT AND WILL SURVIVE ACCIDENTAL DISCONNECTION"
    lane.write_text(json.dumps(data))
    assert qc.evaluate(root)["verdict"] == "PASS"


def test_legacy_campaign_without_quotes_or_index_holds(tmp_path: Path) -> None:
    findings = [{"id": "F1", "claim": "tmux sessions survive disconnection.", "evidence_class": "reported", "source_ids": ["S1"]}]
    root = pack(tmp_path, findings, index=False)
    result = qc.evaluate(root)
    assert (result["verdict"], result["class"]) == ("HOLD", "quote_check_legacy")


def test_absolute_path_from_the_build_location_is_re_anchored_after_a_copy(tmp_path: Path) -> None:
    root = pack(tmp_path)
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["source_file"] = "/somewhere/else/campaign/workspace/sources/02-current-state/tmux-man.txt"
    lane.write_text(json.dumps(data))
    assert qc.evaluate(root)["verdict"] == "PASS"
    data["findings"][0]["source_file"] = "/somewhere/else/other-run/workspace/sources/02-current-state/tmux-man.txt"
    lane.write_text(json.dumps(data))
    assert kinds(qc.evaluate(root)) == {"missing_source_file"}
