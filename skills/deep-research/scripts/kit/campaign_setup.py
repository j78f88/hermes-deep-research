#!/usr/bin/env python3
"""Mechanical campaign setup steps for the phased run driver (deep-research kit).

General versions of what each campaign's control/setup_campaign.py used to rewrite by hand:
scaffold with init_campaign.py, keep only the chosen lanes, record controls that do not apply to a
factual external-only run (with reasons), sync the export flag when dry-run or skip is used, write
the owned-assets list, run the tool preflight and copy the worker helpers and the lane schema into
<root>/workers/. No model calls happen here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

KIT = Path(__file__).resolve().parent
SCRIPTS = KIT.parent
SKILL = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

CONTROLLER_LANES = ("08-controller-synthesis-actionregister", "09-board-brief-verification")
WORKER_HELPERS = ("save_source.py", "gh_get.py", "check_my_lane.py")
STOPWORDS = {"the", "and", "for", "with", "what", "which", "does", "how", "who", "why", "that", "this", "from",
             "into", "protocol", "framework", "frameworks", "system", "tool", "tools", "product", "company",
             "market", "open", "source", "agent", "agents"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def run_init(root: Path, request: dict) -> dict:
    """Scaffold with init_campaign.py unless a manifest already exists. Returns a step record."""
    if (root / "manifest.json").is_file():
        return {"step": "init_campaign", "status": "reused", "reason": "manifest.json already exists"}
    cmd = [sys.executable, str(SCRIPTS / "init_campaign.py"), "--root", str(root), "--target", request["target"],
           "--target-type", request["target_type"], "--decision-question", request["question"],
           "--report-purpose", request["purpose"], "--date", request["date"]]
    if request.get("primary_source"):
        cmd += ["--primary-source", request["primary_source"]]
    if request.get("audience"):
        cmd += ["--audience", request["audience"]]
    if request.get("no_deliverable_export"):
        cmd.append("--no-deliverable-export")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not (root / "manifest.json").is_file():
        raise RuntimeError(f"init_campaign.py failed ({proc.returncode}): {proc.stderr.strip()[-600:]}")
    return {"step": "init_campaign", "status": "done", "command": cmd}


def research_lane_ids(manifest: dict) -> list[str]:
    return [lane["id"] for lane in manifest.get("lanes", []) if lane.get("id") not in CONTROLLER_LANES]


def prune_lanes(root: Path, keep: list[str]) -> dict:
    """Keep only the chosen research lanes (plus the two controller-phase records) everywhere."""
    import campaign_controller as cc

    manifest = load_json(root / "manifest.json")
    known = research_lane_ids(manifest)
    unknown = [lane for lane in keep if lane not in known]
    if unknown:
        raise ValueError(f"unknown lane ids {unknown}; scaffold lanes are {known}")
    drop = [lane for lane in known if lane not in keep]
    if not drop:
        return {"step": "prune_lanes", "status": "reused", "kept": keep, "dropped": []}
    manifest["lanes"] = [lane for lane in manifest["lanes"] if lane.get("id") not in drop]
    removed = []
    for lane in drop:
        for rel in (f"lanes/{lane}.json", f"lanes/{lane}.md", f"workers/{lane}-packet.md", f"linear/children/{lane}.md"):
            path = root / rel
            if path.is_file():
                path.unlink()
                removed.append(rel)
    manifest["files"] = [f for f in manifest.get("files", []) if f.get("path") not in removed]
    for key in ("worker_packets",):
        if isinstance(manifest.get(key), list):
            manifest[key] = [f for f in manifest[key] if not any(d in str(f) for d in drop)]
    manifest["lane_selection"] = {"selected": keep, "not_run": drop, "recorded_by": "run_campaign.py setup",
                                  "reason": "the campaign request named these lanes; the others were removed "
                                            "from the scaffold before dispatch"}
    save_json(root / "manifest.json", manifest)
    plan_path = root / "lane_plan.json"
    if plan_path.is_file():
        plan = load_json(plan_path)
        plan["lanes"] = [lane for lane in plan.get("lanes", []) if lane.get("id") not in drop]
        save_json(plan_path, plan)
    create = root / "linear" / "create_plan.json"
    if create.is_file():
        data = load_json(create)
        data["children"] = [c for c in data.get("children", []) if c.get("lane_id") not in drop]
        save_json(create, data)
    cc.initialize_campaign_state(root, manifest)
    return {"step": "prune_lanes", "status": "done", "kept": keep, "dropped": drop, "removed_files": removed}


def apply_factual_controls(root: Path, purpose: str, preseed_dir: Path | None = None,
                           export_mode: str = "live", export_reason: str | None = None) -> dict:
    """Record controls that do not apply, with reasons, or install supplied pre-seed artifacts."""
    import preseed

    manifest = load_json(root / "manifest.json")
    record = {"step": "controls", "status": "done", "changes": []}
    if preseed_dir is not None:
        for name, spec in manifest["pre_seed"]["artifacts"].items():
            for key in ("json", "markdown"):
                src = preseed_dir / Path(spec[key]).name
                if not src.is_file():
                    raise FileNotFoundError(f"pre-seed artifact missing: {src}")
                shutil.copyfile(src, root / spec[key])
            spec["sha256"] = sha256(root / spec["json"])
            spec["status"] = load_json(root / spec["json"]).get("status", "HOLD")
        verdict = preseed.verify_preseed(root, manifest)
        manifest["pre_seed"]["status"] = manifest["pre_seed"]["gate_status"] = verdict.status
        manifest["pre_seed"]["comparison_lanes_authorized"] = verdict.status == "PASS"
        manifest["pre_seed"]["target_specific_lanes_authorized"] = verdict.status == "PASS"
        record["changes"].append({"pre_seed": verdict.status, "holds": list(verdict.holds)})
        if verdict.status != "PASS":
            save_json(root / "manifest.json", manifest)
            raise ValueError(f"supplied pre-seed artifacts do not pass: {list(verdict.holds)[:5]}")
    elif purpose == "factual":
        if "pre_seed" in manifest:
            manifest.pop("pre_seed")
            shutil.rmtree(root / "preseed", ignore_errors=True)
            manifest["files"] = [f for f in manifest.get("files", []) if not str(f.get("path", "")).startswith("preseed/")]
        manifest.setdefault("non_applicable_controls", {})["pre_seed"] = (
            "Factual question with no supplied candidate, vendor or duration to neutralise; the pre-seed "
            "comparison controls apply to comparison or recommendation asks.")
        record["changes"].append("pre_seed not applicable (factual)")
    else:
        raise ValueError("decision_assessment campaigns need --preseed-dir with the four pre-seed artifacts")

    if "research_orchestration" in manifest:
        manifest.pop("research_orchestration", None)

    for key in ("deliverable_exports", "deliverable_exports"):
        if isinstance(manifest.get(key), dict) and export_mode != "live":
            manifest[key]["enabled"] = False
            manifest[key]["status"] = f"suppressed_{export_mode.replace('-', '_')}"
            manifest[key]["disabled_reason"] = export_reason or f"export mode is {export_mode}"
            record["changes"].append(f"{key}.enabled=False ({export_mode})")

    save_json(root / "manifest.json", manifest)
    return record


def apply_history_mode(root: Path, mode: str, reason: str) -> dict:
    """external-only: record the skipped relevant-history scan and an empty internal context, with reasons."""
    if mode != "external-only":
        raise ValueError("only --history external-only is automated in this driver version")
    stamp = now()
    hist = load_json(root / "history" / "relevant-history.json")
    hist.update(status="skipped_external_only", skipped_at=stamp, reason=reason,
                surfaces_checked=[], recommendation="external_only")
    save_json(root / "history" / "relevant-history.json", hist)
    (root / "history" / "relevant-history.md").write_text(
        f"# Relevant history\n\nStatus: skipped, external-only run.\n\nRecorded {stamp}. {reason}\n", encoding="utf-8")
    manifest = load_json(root / "manifest.json")
    manifest.setdefault("history_scan", {}).update(status="skipped_external_only", reason=reason)
    manifest.setdefault("context", {})["internal_retrieval"] = {"status": "not_run", "reason": reason}
    save_json(root / "manifest.json", manifest)
    return {"step": "history", "status": "done", "mode": mode, "reason": reason}


def keywords(target: str) -> list[str]:
    words = [w for w in re.split(r"[^a-z0-9-]+", target.lower()) if len(w) >= 3 and w not in STOPWORDS]
    whole = target.lower().strip()
    return sorted({whole, *words}) if whole else sorted(set(words))


def _frontmatter(path: Path) -> str:
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            return "".join(next(fh, "") for _ in range(20)).lower()
    except OSError:
        return ""


def write_owned_assets(root: Path, target: str, supplied: Path | None = None, home: Path | None = None) -> dict:
    """Factual owned-asset inventory. A supplied file wins; otherwise a name-level scan of skills, apps and units."""
    out_md, out_json = root / "context" / "owned-assets.md", root / "context" / "owned-assets.json"
    if supplied is not None:
        text = supplied.read_text(encoding="utf-8")
        out_md.write_text(text, encoding="utf-8")
        save_json(out_json, {"schema_version": "owned-assets-v1", "written_at": now(), "source": "supplied",
                             "markdown": "context/owned-assets.md"})
        return {"step": "owned_assets", "status": "done", "source": str(supplied)}
    home = home or Path.home()
    keys = keywords(target)
    assets = []
    try:
        hermes_bin = shutil.which("hermes")
        proc = subprocess.run([hermes_bin, "--version"], capture_output=True, text=True, timeout=30) if hermes_bin else None
        version = (proc.stdout or proc.stderr).strip().splitlines()[0] if proc and proc.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, IndexError):
        version = None
    if version:
        assets.append({"name": "Hermes Agent", "class": "platform", "evidence_level": "proven working",
                       "facts": f"Installed on PATH ({version})."})
    hermes_home = Path(os.environ.get("HERMES_HOME") or home / ".hermes").expanduser()
    for base in (hermes_home / "skills",):
        for skill in sorted(base.glob("**/SKILL.md")) if base.is_dir() else []:
            rel = skill.parent.relative_to(base).as_posix()
            head = _frontmatter(skill)
            if any(k in head or k in rel.lower() for k in keys):
                assets.append({"name": f"skill {rel}", "class": "skill", "evidence_level": "name only",
                               "facts": f"Hermes skill {rel} mentions the topic in its name or description."})
    try:
        proc = subprocess.run(["systemctl", "--user", "list-units", "--type=service,timer", "--no-legend", "--plain"],
                              capture_output=True, text=True, timeout=30)
        for line in proc.stdout.splitlines():
            unit = line.split()[0] if line.split() else ""
            if unit and any(k in unit.lower() for k in keys):
                assets.append({"name": f"user unit {unit}", "class": "running job", "evidence_level": "name only",
                               "facts": f"User systemd unit {unit} matches the topic by name."})
    except (OSError, subprocess.SubprocessError):
        pass
    data = {"schema_version": "owned-assets-v1", "written_at": now(), "source": "run_campaign.py name-level scan",
            "keywords": keys, "note": "Facts about owned tools, skills, apps and running jobs that match the topic "
                                      "by name. No opinions, decisions or private history.", "assets": assets}
    save_json(out_json, data)
    lines = ["# Owned assets (facts only)", "", f"Written {data['written_at']} by a name-level scan for: "
             + ", ".join(keys) + ".", ""]
    lines += [f"- **{a['name']}** ({a['class']}, evidence: {a['evidence_level']}). {a['facts']}" for a in assets]
    if not assets:
        lines.append("- No owned tool, skill, app or running job matched the topic by name.")
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"step": "owned_assets", "status": "done", "count": len(assets)}


def install_worker_kit(root: Path) -> dict:
    """Copy the worker helpers and the lane schema (with a coverage array allowed) into <root>/workers/."""
    workers = root / "workers"
    workers.mkdir(parents=True, exist_ok=True)
    for name in WORKER_HELPERS:
        shutil.copyfile(KIT / name, workers / name)
        (workers / name).chmod(0o755)
    schema = json.loads((SKILL / "references" / "lane-output-schema.json").read_text(encoding="utf-8"))
    schema.setdefault("properties", {})["coverage"] = {"type": "array"}
    (workers / "lane-output-schema.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
    return {"step": "worker_kit", "status": "done", "files": [f"workers/{n}" for n in WORKER_HELPERS]
            + ["workers/lane-output-schema.json"]}


def tool_preflight(root: Path, probe_url: str | None = None, probe_repo: str = "repos/octocat/Hello-World") -> dict:
    """Prove the worker helpers and binaries work before dispatch. Writes receipts/tool-preflight.json."""
    tools = []
    samples = root / "receipts" / "tool-preflight-samples"
    samples.mkdir(parents=True, exist_ok=True)
    stage = root / "workspace" / "sources" / "preflight"

    def helper(name: str, args: list[str]) -> None:
        try:
            proc = subprocess.run([sys.executable, str(root / "workers" / name), "preflight", *args],
                                  capture_output=True, text=True, timeout=180)
            row = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            row = {"ok": False, "error": type(exc).__name__}
        ok = bool(row.get("ok"))
        detail = (f"saved {row.get('url')} via {row.get('method')} ({row.get('chars')} chars)" if ok
                  else f"failed: {str(row.get('error') or row.get('last_code'))[:200]}")
        tools.append({"tool": f"workers/{name}", "status": "PASS" if ok else "FAIL", "detail": detail})

    helper("save_source.py", ["PF01", probe_url or "https://example.com/"])
    if shutil.which("gh"):
        helper("gh_get.py", ["PF02", probe_repo])
    else:
        tools.append({"tool": "workers/gh_get.py", "status": "UNAVAILABLE", "detail": "gh CLI not on PATH"})
    if stage.is_dir():
        for f in stage.iterdir():
            shutil.copy2(f, samples / f.name)
        shutil.rmtree(stage, ignore_errors=True)
    for binary in ("curl", "pdftotext", "systemd-run"):
        tools.append({"tool": binary, "status": "PASS" if shutil.which(binary) else "UNAVAILABLE",
                      "detail": shutil.which(binary) or "not on PATH"})
    hermes = shutil.which("hermes")
    tools.append({"tool": "hermes", "status": "PASS" if hermes else "FAIL", "detail": hermes or "not on PATH"})
    try:
        import jsonschema  # noqa: F401
        tools.append({"tool": "jsonschema", "status": "PASS", "detail": "importable for check_my_lane.py"})
    except ImportError:
        tools.append({"tool": "jsonschema", "status": "FAIL", "detail": "not importable"})
    tools.append({"tool": "web_search / web_extract", "status": "NOT_PROBED",
                  "detail": "Hermes tools; lane sessions use them and web_search_status.py reads the session "
                            "records at completion. Direct Apify Actor runs are not used."})
    data = {"schema_version": "tool-preflight-v1", "checked_at": now(), "tools": tools,
            "samples": "receipts/tool-preflight-samples/"}
    save_json(root / "receipts" / "tool-preflight.json", data)
    failed = [t["tool"] for t in tools if t["status"] == "FAIL"]
    return {"step": "tool_preflight", "status": "done" if not failed else "failed", "failed": failed}
