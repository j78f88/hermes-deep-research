#!/usr/bin/env python3
"""Logged edits of context, history and manifest records (deep-research kit, DR-897).

Every change saves the pre-change copy under context/edit-backups/ first and appends one line to
context/edit-log.jsonl with before and after hashes. The log is append-only: an entry is written once
and never rewritten on a rerun (SKILL.md pitfalls 23e and 30). A write that changes nothing is not logged.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def logged_edit(root: Path, rel: str, new_text: str, reason: str, actor: str = "run_campaign.py") -> dict | None:
    path = root / rel
    before = path.read_bytes() if path.is_file() else None
    after = new_text.encode("utf-8")
    if before == after:
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = None
    if before is not None:
        backup_path = root / "context" / "edit-backups" / f"{stamp}-{rel.replace('/', '__')}"
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_bytes(before)
        backup = backup_path.relative_to(root).as_posix()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(after)
    entry = {"at": datetime.now(timezone.utc).isoformat(), "path": rel, "before_sha256": _sha(before),
             "after_sha256": _sha(after), "backup": backup, "reason": reason, "actor": actor}
    log = root / "context" / "edit-log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def logged_json_edit(root: Path, rel: str, update, reason: str) -> dict | None:
    """Load JSON at rel, apply update(data) in place, and write it back through logged_edit."""
    path = root / rel
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    update(data)
    return logged_edit(root, rel, json.dumps(data, indent=2) + "\n", reason)
