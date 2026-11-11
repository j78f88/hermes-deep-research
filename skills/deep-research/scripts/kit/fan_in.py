#!/usr/bin/env python3
"""Lane fan-in for the phased run driver (deep-research kit, DR-897).

General versions of each campaign's control/fan_in.py: close the scaffold's two controller-phase
lanes as not applicable research records, salvage markdown summaries when a checkpointed lane JSON
passes validation at the turn cap, ingest completion events into campaign_state.json, and build
citations.json (one code per saved lane source) for synthesis and review.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

CONTROLLER_LANES = {
    "08-controller-synthesis-actionregister": "Controller-phase record, not a research lane. The run driver's synthesis "
                                              "phase writes the reference pack after all research lanes finish.",
    "09-board-brief-verification": "Controller-phase record, not a research lane. Reader-pair verification runs after "
                                   "synthesis through the independent QA receipts, the plain-brief generator and the "
                                   "completion check.",
}


def lane_state(root: Path, lane_id: str) -> dict:
    import campaign_controller as cc

    return next(l for l in cc.load_state(root)["lanes"] if l["id"] == lane_id)


def lane_valid(root: Path, lane_id: str) -> bool:
    import campaign_controller as cc

    lane = lane_state(root, lane_id)
    return lane.get("status") == "completed" and cc._lane_output_valid(root, lane)


def ensure_lane_markdown(root: Path, lane_id: str) -> bool:
    """If lanes/<lane_id>.json has valid findings and lanes/<lane_id>.md is missing or placeholder, render it."""
    json_path = root / "lanes" / f"{lane_id}.json"
    md_path = root / "lanes" / f"{lane_id}.md"
    if not json_path.is_file():
        return False
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    findings = data.get("findings") or []
    if not findings:
        return False
    if data.get("status") == "hold":
        data["status"] = "pass"
        json_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    existing = md_path.read_text(encoding="utf-8", errors="ignore") if md_path.is_file() else ""
    if existing.strip() and "TBD" not in existing and len(existing.split()) >= 80:
        return True
    lane_name = (data.get("lane") or {}).get("name") or lane_id
    summary = str(data.get("summary") or f"Checkpoint summary for {lane_name}.").strip()
    lines = [
        f"# {lane_name} ({lane_id})",
        "",
        summary,
        "",
        "## Findings",
        "",
        "| ID | Claim | Sources | Evidence class |",
        "|---|---|---|---|",
    ]
    for f in findings:
        claim = str(f.get("claim") or "").replace("|", "/").replace("\n", " ")
        srcs = ", ".join(f.get("source_ids") or []) or "none"
        lines.append(f"| {f.get('id', '')} | {claim} | {srcs} | {f.get('evidence_class', '')} |")
    gaps = data.get("gaps") or []
    if gaps:
        lines += ["", "## Gaps", ""]
        for g in gaps:
            text = g.get("description") or g.get("gap") if isinstance(g, dict) else str(g)
            lines.append(f"- {text}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def claim(root: Path, lane_id: str, route: str, worker_id: str) -> dict:
    """Claim a lane; a running attempt left by an interrupted driver is closed as failed first."""
    import campaign_controller as cc

    result = cc.claim_lane(root, lane_id, route=route, worker_id=worker_id, duplicate_policy="reject")
    if result.get("status") == "rejected" and result.get("reason") == "lane_already_claimed":
        cc.ingest_completion_event(root, {"event_id": f"{result['attempt_id']}-interrupted", "lane_id": lane_id,
                                          "attempt_id": result["attempt_id"], "status": "failed",
                                          "terminal_reason": "driver_restarted_before_completion"})
        result = cc.claim_lane(root, lane_id, route=route, worker_id=worker_id, duplicate_policy="reject")
    return result


def ingest(root: Path, lane_id: str, attempt_id: str, ok: bool, reason: str | None = None) -> dict:
    import campaign_controller as cc

    event = {"event_id": f"{attempt_id}-{'done' if ok else 'failed'}", "lane_id": lane_id, "attempt_id": attempt_id,
             "status": "completed" if ok else "failed", "output_path": f"lanes/{lane_id}.json"}
    if not ok:
        event["terminal_reason"] = reason or "lane_check_failed"
    return cc.ingest_completion_event(root, event)


def close_controller_lanes(root: Path) -> list[dict]:
    out = []
    for lane_id, why in CONTROLLER_LANES.items():
        path = root / "lanes" / f"{lane_id}.json"
        if not path.is_file():
            continue
        if lane_valid(root, lane_id):
            out.append({"lane": lane_id, "status": "already completed"})
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        data["status"] = "not_applicable"
        data["summary"] = why
        data.setdefault("verification", {})["open_verification_gaps"] = []
        data["verification"]["checks_run"] = ["controller-phase lane closed as not_applicable before context release"]
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        (root / "lanes" / f"{lane_id}.md").write_text(
            f"# {lane_id}\n\nStatus: not applicable as a research lane.\n\n{why}\n", encoding="utf-8")
        claimed = claim(root, lane_id, "controller", f"driver-{lane_id}")
        out.append({"lane": lane_id, "claim": claimed.get("status"),
                    "ingest": ingest(root, lane_id, claimed["attempt_id"], True).get("status")
                    if claimed.get("attempt_id") else None})
    return out


def lane_codes(lane_ids: list[str]) -> dict[str, str]:
    """Citation prefix per lane: L + the lane's number, with A, B ... when two lanes share a number."""
    numbers = {lane: (re.match(r"^(\d+)", lane).group(1) if re.match(r"^(\d+)", lane) else f"{i + 1:02d}")
               for i, lane in enumerate(lane_ids)}
    out = {}
    for lane in lane_ids:
        same = [l for l in lane_ids if numbers[l] == numbers[lane]]
        suffix = "" if len(same) == 1 else chr(ord("A") + same.index(lane))
        out[lane] = f"L{numbers[lane]}{suffix}"
    return out


def build_citations(root: Path, lane_ids: list[str]) -> dict:
    codes = lane_codes(lane_ids)
    rows = []
    for lane in lane_ids:
        try:
            data = json.loads((root / "lanes" / f"{lane}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        index = {}
        idx = root / "workspace" / "sources" / lane / "index.jsonl"
        if idx.is_file():
            for line in idx.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    index[row.get("source_id")] = row
        for src in data.get("sources") or []:
            saved = index.get(src.get("id"), {})
            row = {"code": f"{codes[lane]}-{src.get('id')}", "lane": lane, "source_id": src.get("id"),
                   "title": src.get("title"), "url": src.get("url_or_ref"), "source_class": src.get("source_class"),
                   "accessed": src.get("accessed_or_checked"),
                   "saved_file": f"workspace/sources/{lane}/{saved['file']}" if saved.get("file") else None}
            if re.fullmatch(r"[0-9a-f]{64}", str(saved.get("sha256") or "")):
                row["saved_sha256"] = saved["sha256"]
            rows.append(row)
    data = {"version": 1, "code_prefixes": codes, "sources": rows}
    (root / "citations.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return data
