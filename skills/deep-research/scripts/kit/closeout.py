#!/usr/bin/env python3
"""Export and archive steps for the phased run driver (deep-research kit, DR-897).

Modes: live (export_deliverables.py, scoped vault git add/commit/push of exactly the exported paths,
remote readback, archive archive with rsync -rlt and a sha256 check of every file), dry-run
(export_deliverables.py --dry-run, rsync -rltn listing only, no git), skip (nothing; reason recorded).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
ARCHIVE_BASE = Path.home() / ".hermes/work/deep-research-archive"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(cmd: list[str], timeout: int = 600, cwd: Path | None = None) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    return {"command": cmd, "rc": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}


def file_hashes(base: Path) -> dict[str, str]:
    out = {}
    for path in sorted(base.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(base).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def archive(root: Path, dest: Path, dry_run: bool) -> dict:
    """rsync -rlt root/ dest/ (plain -a fails chgrp on the NAS), then sha256-verify every file."""
    record = {"destination": str(dest), "dry_run": dry_run}
    subprocess.run(["df", str(dest.parent if dest.parent.exists() else dest.parent.parent)], capture_output=True)
    if not dry_run:
        dest.mkdir(parents=True, exist_ok=True)
    flags = "-rltn" if dry_run else "-rlt"
    record["rsync"] = run(["rsync", flags, "--exclude", "__pycache__", f"{root}/", f"{dest}/"], timeout=3600)
    if dry_run or record["rsync"]["rc"] != 0:
        record["status"] = "dry_run" if dry_run and record["rsync"]["rc"] == 0 else "failed"
        return record
    src, dst = file_hashes(root), file_hashes(dest)
    src = {k: v for k, v in src.items() if "__pycache__" not in k}
    mismatched = [k for k, v in src.items() if dst.get(k) != v]
    record.update(files=len(src), mismatched=mismatched[:50], status="verified" if not mismatched else "mismatch")
    return record


def export_commit(vault: Path, paths: list[Path], message: str) -> dict:
    """Commit and push exactly the exported paths, then compare the remote main with the local HEAD."""
    rels = [str(p.relative_to(vault)) for p in paths]
    steps = [run(["git", "-C", str(vault), "fetch", "origin"], timeout=300),
             run(["git", "-C", str(vault), "add", "--", *rels]),
             run(["git", "-C", str(vault), "commit", "-m", message, "--", *rels])]
    if steps[-1]["rc"] != 0:
        return {"status": "failed", "steps": steps}
    steps.append(run(["git", "-C", str(vault), "push", "origin", "HEAD:main"], timeout=300))
    head = run(["git", "-C", str(vault), "rev-parse", "HEAD"])["stdout"].strip()
    remote = run(["git", "-C", str(vault), "ls-remote", "origin", "main"], timeout=300)["stdout"].split()
    ok = steps[-1]["rc"] == 0 and remote[:1] == [head]
    return {"status": "pushed" if ok else "failed", "commit": head, "remote_main": remote[:1], "paths": rels,
            "steps": steps}


def export(root: Path, mode: str, reason: str | None, run_id: str) -> dict:
    record = {"schema_version": "deep-research-closeout-v1", "mode": mode, "at": now(), "reason": reason}
    if mode == "skip":
        record["status"] = "skipped"
        return record
    cmd = [sys.executable, str(SCRIPTS / "export_deliverables.py"), "--campaign-root", str(root)]
    if mode == "dry-run":
        cmd.append("--dry-run")
    record["export_deliverables"] = run(cmd, timeout=900)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    record["archive"] = archive(root, ARCHIVE_BASE / month / run_id, dry_run=(mode != "live"))
    if mode == "live" and record["export_deliverables"]["rc"] == 0:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        ve = manifest.get("deliverable_exports", {})
        vault = Path(ve.get("export_root") or "./exports")
        paths = [Path(item["vault_path"]) for item in ve.get("exports", []) if item.get("vault_path")]
        record["vault_git"] = export_commit(vault, paths, f"deep research: {run_id}") if paths else \
            {"status": "failed", "reason": "export recorded no deliverable paths"}
    etv_rc = record["export_deliverables"]["rc"]
    etv_ok = etv_rc == 0 or (mode == "dry-run" and etv_rc == 2 and "suppressed" in record["export_deliverables"].get("stdout", ""))
    ok = etv_ok and record["archive"].get("status") in ("verified", "dry_run") and \
        (mode != "live" or record.get("vault_git", {}).get("status") == "pushed")
    record["status"] = ("exported" if mode == "live" else "dry_run") if ok else "failed"
    return record
