#!/usr/bin/env python3
"""Tests for quote_check.py and acquire_youtube_apify.py."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import acquire_youtube_apify as aya
import quote_check as qc

SOURCE_TEXT = "tmux keeps sessions alive.\nEach session is **persistent** and will survive accidental disconnection.\n"


def pack(tmp_path: Path, findings: list[dict] | None = None, *, index: bool = True) -> Path:
    root = tmp_path / "campaign"
    src = root / "workspace" / "sources" / "02-current-state"
    src.mkdir(parents=True)
    f = src / "tmux-man.txt"
    f.write_text(SOURCE_TEXT, encoding="utf-8")
    if index:
        row = {
            "id": "tmux-man",
            "url": "https://man.openbsd.org/tmux.1",
            "file": "workspace/sources/02-current-state/tmux-man.txt",
            "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
        }
        (src / "index.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    (root / "lanes").mkdir()
    findings = findings if findings is not None else [{
        "id": "F1",
        "claim": "tmux sessions survive disconnection.",
        "evidence_class": "reported",
        "confidence": "high",
        "source_ids": ["tmux-man"],
        "quote": "Each session is persistent and will survive accidental disconnection",
        "source_file": "tmux-man.txt",
    }]
    (root / "lanes" / "02-current-state.json").write_text(json.dumps({"findings": findings}), encoding="utf-8")
    return root


def kinds(result: dict) -> set[str]:
    return {p["kind"] for p in result["problems"]}


def test_good_pack_passes_and_markdown_case_is_normalized(tmp_path):
    root = pack(tmp_path)
    assert qc.evaluate(root)["verdict"] == "PASS"
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["quote"] = "EACH SESSION IS PERSISTENT AND WILL SURVIVE ACCIDENTAL DISCONNECTION"
    lane.write_text(json.dumps(data))
    assert qc.evaluate(root)["verdict"] == "PASS"


def test_missing_or_altered_quote_and_changed_source_fail(tmp_path):
    root = pack(tmp_path)
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["quote"] = "Each session will survive a host reboot"
    lane.write_text(json.dumps(data))
    assert kinds(qc.evaluate(root)) == {"quote_not_found"}
    data["findings"][0].pop("quote")
    lane.write_text(json.dumps(data))
    assert kinds(qc.evaluate(root)) == {"missing_quote"}


def test_unindexed_or_symlinked_source_fails(tmp_path):
    root = pack(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(SOURCE_TEXT, encoding="utf-8")
    os.symlink(outside, root / "workspace" / "sources" / "02-current-state" / "link.txt")
    lane = root / "lanes" / "02-current-state.json"
    data = json.loads(lane.read_text())
    data["findings"][0]["source_file"] = "link.txt"
    lane.write_text(json.dumps(data))
    assert kinds(qc.evaluate(root)) == {"missing_source_file"}


def test_uncited_fact_fails_while_labelled_inference_passes(tmp_path):
    findings = [
        {"id": "F2", "claim": "tmux is best.", "evidence_class": "reported", "source_ids": []},
        {"id": "F3", "claim": "tmux fits us.", "evidence_class": "analyst_inference", "source_ids": []},
    ]
    res = qc.evaluate(pack(tmp_path, findings))
    assert [(p["id"], p["kind"]) for p in res["problems"]] == [("F2", "uncited_fact")]
    assert res["inference_or_gap"] == 1


def test_youtube_url_extraction():
    vid = aya.video_id_from("https://youtu.be/dQw4w9WgXcQ")
    assert vid == "dQw4w9WgXcQ"
