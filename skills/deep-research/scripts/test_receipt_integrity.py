#!/usr/bin/env python3
"""Tests for receipt_integrity.py and web_search_status.py."""
from __future__ import annotations

import json
from pathlib import Path

import receipt_integrity as ri
import web_search_status as wss

FORGED_REVIEW = {
    "status": "PASS",
    "word_count": 576,
    "cap_status": "under_cap",
    "lint_status": "clean",
    "editor_model": "claude-opus-5.5-mock",
    "hashes": {"plain_brief": "mock-hash", "full_pack": "mock-hash"},
}

APIFY_ERROR = (
    '{"status": "success", "output": "WEB SEARCH: {\'success\': False, \'error\': '
    '\\"web is configured to use \'apify\', but no registered web search provider has that name.\\"}"}'
)


def test_forged_review_and_placeholder_generation_are_detected():
    res = ri.static_check(FORGED_REVIEW)
    assert res["verdict"] == "FAIL" and res["class"] == "receipt_fabricated"
    no_hash = {"status": "PASS", "editor_model": "openai/gpt-6.1-sol-pro", "writer_model": "anthropic/claude-opus-5.5"}
    assert ri.static_check(no_hash)["class"] == "receipt_fabricated"
    mock_gen = {
        "status": "PASS",
        "brief_sha256": "a" * 64,
        "provenance": {"calls": [{"role": "editor", "generation_id": "mock"}]},
    }
    assert ri.static_check(mock_gen)["class"] == "receipt_fabricated"


def test_deterministic_mechanical_qa_receipt_passes_static_check():
    receipt = {
        "schema_version": "deep-research-qa-receipt-v1",
        "name": "taxonomy_review",
        "status": "PASS",
        "reviewer": "deterministic/mechanical_qa",
        "producers": ["anthropic/claude-opus-5.5"],
        "reference_pack_sha256": "a" * 64,
        "plain_brief_sha256": "b" * 64,
        "reviewed_files": {"internal/reference-pack.md": "a" * 64},
    }
    assert ri.static_check(receipt)["verdict"] == "PASS"


def test_web_search_status_message_scanning():
    broken = [
        {"role": "assistant", "tool_calls": json.dumps([{"id": "c1", "function": {"name": "execute_code", "arguments": json.dumps({"code": "web_search('x')"})}}])},
        {"role": "tool", "tool_name": "execute_code", "tool_call_id": "c1", "content": APIFY_ERROR},
    ]
    assert wss.scan_messages(broken)["failures"] == 1
    ok = [
        {"role": "assistant", "tool_calls": json.dumps([{"id": "w1", "function": {"name": "web_search", "arguments": "{}"}}])},
        {"role": "tool", "tool_name": "web_search", "tool_call_id": "w1", "content": '{"success": true, "data": {"web": [{"url": "https://example.com"}]}}'},
    ]
    assert wss.scan_messages(ok)["successes"] == 1


def test_web_search_declaration_wording():
    assert wss.declared_in("## What is still missing\n- General web search was unusable in several strands.")
    assert wss.declared_in("Web search was not available, so sources came from GitHub.")
    assert not wss.declared_in("We searched GitHub issues for reports.")


def test_runtime_python_and_probe_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(wss, "runtime_python", lambda *a, **k: None)
    assert wss.probe()["available"] is False
    fake = tmp_path / "python3"
    fake.write_text("#!/bin/sh\necho 'PROBE{\"success\": false, \"error\": \"no registered web search provider\"}'\n")
    fake.chmod(0o755)
    res = wss.probe(python=fake)
    assert res["available"] is False and "no registered" in res["error"]
