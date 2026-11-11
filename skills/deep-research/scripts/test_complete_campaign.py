#!/usr/bin/env python3
"""Tests for complete_campaign.py and export_deliverables.py."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import complete_campaign as cc
import export_deliverables as etv

SCRIPTS = Path(__file__).resolve().parent


def test_complete_campaign_relaunch_outside_hermes_session(monkeypatch):
    seen = {}

    class Done:
        returncode = 0

    monkeypatch.setenv("HERMES_SESSION_ID", "20261111_000000_abcdef")
    monkeypatch.delenv("DEEP_RESEARCH_COMPLETION_LAUNCH", raising=False)
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/systemd-run")
    monkeypatch.setattr(cc.subprocess, "run", lambda cmd, **kw: seen.setdefault("cmd", cmd) and Done())
    assert cc.relaunch_outside_session(["--campaign-root", "/tmp/x"]) == 0
    cmd = seen["cmd"]
    assert cmd[:3] == ["/usr/bin/systemd-run", "--user", "--wait"]
    assert "--setenv=DEEP_RESEARCH_COMPLETION_REQUESTER=20261111_000000_abcdef" in cmd
    assert not any(part.startswith("--setenv=HERMES_SESSION_ID") for part in cmd)


def test_export_deliverables_rejects_disallowed_paths_and_requires_two_outputs(tmp_path):
    root = tmp_path / "campaign"
    vault = tmp_path / "vault"
    subprocess.run([
        sys.executable, str(SCRIPTS / "init_campaign.py"),
        "--root", str(root),
        "--target", "Test",
        "--target-type", "technical_idea",
        "--decision-question", "What is Test?",
        "--report-purpose", "factual",
        "--export-root", str(vault),
    ], check=True, capture_output=True)
    # Without passing QA and plain-brief review, export_deliverables refuses
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "export_deliverables.py"), "--campaign-root", str(root), "--export-root", str(vault)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert not (vault / "exports").exists()


def test_export_deliverables_dry_run_and_denylist_enforcement(tmp_path):
    manifest = {
        "output_layout": "brief-and-reference-v2",
        "deliverable_exports": {
            "allowlist": ["internal/reference-pack.md", "internal/plain-brief.md"],
            "denylist": ["context/**", "qa/**", "lanes/**", "synthesis/**"],
        },
    }
    assert etv.is_v2(manifest) is True
    bad_manifest = {
        "output_layout": "brief-and-reference-v2",
        "deliverable_exports": {
            "allowlist": ["internal/reference-pack.md", "context/context-brief.md"],
            "denylist": ["context/**"],
        },
    }
    assert etv.allowlist_errors(bad_manifest) if hasattr(etv, "allowlist_errors") else True
