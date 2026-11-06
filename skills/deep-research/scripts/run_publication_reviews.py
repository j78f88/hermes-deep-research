#!/usr/bin/env python3
"""Guarded publication-review runner for a deep-research campaign.

This is a generic replacement for campaign-local review runners. It refuses to
start publication QA until every editor note has an evidence-backed decision.
It binds review inputs through exact file packets and never discovers neighbours.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import json
import subprocess
import sys
from pathlib import Path

from research_qa_repair import (
    build_review_packet,
    campaign_readiness,
    confined_output_path,
    prepare_preexport_assurance_stub,
    validate_reference_sources_section,
    verify_review_packet,
)


DEFAULT_REVIEW = "qa/plain-brief-review.json"
DEFAULT_DECISIONS = "qa/editor-note-decisions.json"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--campaign-root", required=True)
    parser.add_argument("--review", default=DEFAULT_REVIEW)
    parser.add_argument("--decisions", default=DEFAULT_DECISIONS)
    parser.add_argument("--input", action="append", default=[])
    parser.add_argument("--packet-out", default="qa/inputs/publication-review-inputs.json")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Validate the saved review and exact inputs without starting a command.")
    parser.add_argument("--reset-assurance-stub", action="store_true",
                        help="Reset stale qa/final-assurance.json hash bindings before building the packet.")
    parser.add_argument("--command", nargs=argparse.REMAINDER,
                        help="Publication QA command. Omit for a guarded dry check.")
    args = parser.parse_args()
    if args.preflight_only and args.command:
        parser.error("--preflight-only cannot be combined with --command")
    root = Path(args.campaign_root).expanduser().resolve(strict=True)
    gate = campaign_readiness(root, review_rel=args.review, decisions_rel=args.decisions)
    if not gate["publication_qa_allowed"]:
        print(json.dumps(gate, indent=2))
        return 2
    # The shared gate validates confinement before any manifest-controlled read.
    optional_legacy = gate.get("scope") == "optional_legacy_brief"
    review = {} if optional_legacy else load_json(root / args.review)

    full_pack_path = review.get("full_pack_path")
    pack_file = root / full_pack_path if full_pack_path else None
    if pack_file is not None and pack_file.is_file():
        sources_check = validate_reference_sources_section(pack_file.read_text(encoding="utf-8"))
        if sources_check["status"] != "PASS":
            print(json.dumps(sources_check, indent=2))
            return 2
    if args.reset_assurance_stub and not optional_legacy:
        prepare_preexport_assurance_stub(
            root,
            brief_sha256=str(review.get("brief_sha256") or ""),
            pack_sha256=str(review.get("full_pack_sha256") or ""),
        )

    # Always protect every shared-gate input and semantic settings, even with --input.
    inputs = [*args.input, "manifest.json", *gate.get("inputs", [])]
    packet = build_review_packet(inputs, root=root)
    # Stub resets must not silently replace any evidence used by acceptance.
    readiness = campaign_readiness(root, review_rel=args.review, decisions_rel=args.decisions)
    if not readiness["publication_qa_allowed"]:
        print(json.dumps(readiness, indent=2))
        return 2
    try:
        packet_path = confined_output_path(root, args.packet_out)
        if not args.packet_out.startswith("qa/inputs/") or not args.packet_out.endswith(".json"):
            raise ValueError("publication packets must be JSON sidecars under qa/inputs/")
        if args.packet_out in {item["path"] for item in packet["files"]}:
            raise ValueError("publication packet cannot overwrite an admitted review input")
    except ValueError as exc:
        print(json.dumps({"status": "BLOCKED_CHANGED_INPUT", "findings": [str(exc)]}, indent=2))
        return 2
    packet_path.parent.mkdir(parents=True, exist_ok=True)
    packet_path.write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    checked = verify_review_packet(packet, root=root)
    if checked["status"] != "PASS":
        print(json.dumps(checked, indent=2))
        return 2
    if not args.command:
        print(json.dumps({"status": "PASS", "publication_qa_started": False,
                          "input_packet": args.packet_out}, indent=2))
        return 0
    proc = subprocess.run(args.command, cwd=root)
    if proc.returncode:
        return proc.returncode
    checked = verify_review_packet(packet, root=root)
    readiness = campaign_readiness(root, review_rel=args.review, decisions_rel=args.decisions)
    status = checked["status"] if checked["status"] != "PASS" else readiness["status"]
    allowed = checked["status"] == "PASS" and readiness["publication_qa_allowed"]
    print(json.dumps({"status": status, "publication_qa_started": True,
                      "findings": readiness.get("findings", []), "input_packet": args.packet_out}, indent=2))
    return 0 if allowed else 2


if __name__ == "__main__":
    raise SystemExit(main())