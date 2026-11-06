#!/usr/bin/env python3
"""read-only relevant-history adapter.

Builds a ``prior-work-context-v1`` artifact from injected Linear and Knowledge base
lookup runners. The adapter is deliberately transport-agnostic: it never opens a
network connection, reads a credential, or mutates any store. The caller injects
two runner callables:

    linear_runner(query_terms: list[str]) -> dict
    knowledge_base_runner(query_terms: list[str]) -> dict

Each runner returns a read-only lookup result of the shape::

    {
        "status": "ok" | "blocked" | "none_found",
        "checked_at": "<iso8601>" | None,
        "blocker": "<reason>",            # optional, when status == "blocked"
        "matches": [
            {
                "ref": str,               # e.g. "ISSUE-458" or a export path
                "title": str,
                "relationship": str,      # prior-work relationship enum value
                "observed_state": str,
                "observed_at": "<iso8601>",
                "owner": str | None,      # optional
                "reusable_assets": [str], # optional
                "decision_or_gate": str | None,  # optional
                "notes": str | None,      # optional
            },
            ...
        ],
    }

In production the runners are thin wrappers around the read-only ``linear`` /
``search_files`` tools. In tests they are pure fakes, so the whole module runs
offline with zero live side effects.

Key guarantees:
  * PW ids are stable and deterministic (SHA of surface + ref), independent of
    discovery order, so re-running the scan never renumbers prior work.
  * A blocked required surface is recorded as a material access gap with
    ``checked=false`` / ``status="blocked"`` so the shared evaluator HOLDs
    rather than treating an inaccessible surface as "no prior work".
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

REQUIRED_SURFACES = ("linear", "knowledge_base")

VALID_RELATIONSHIPS = {
    "reuse_as_is",
    "extend_existing",
    "replace_with_reason",
    "contradicts_prior_decision",
    "superseded",
    "duplicate_do_not_build",
    "failed_attempt_with_lesson",
    "unresolved_gap",
    "not_relevant",
}


def _evidence_hash(surface: str, ref: str, match: dict) -> str:
    payload = json.dumps(
        {"surface": surface, "ref": ref, "title": match.get("title", ""), "state": match.get("observed_state", "")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _pw_id(surface: str, ref: str) -> str:
    """Stable, deterministic PW id derived from surface + ref.

    Order-independent: the same (surface, ref) always maps to the same id, so a
    re-scan or a differently ordered result set never renumbers prior work. Uses a
    wide (48-bit) digest prefix so distinct refs do not collide in practice; a
    genuine collision would only ever merge two identical refs, never drop one.
    """
    digest = hashlib.sha256(f"{surface}::{ref}".encode("utf-8")).hexdigest()
    return f"PW-{int(digest[:12], 16):015d}"


def _freshness_for(surface: str, result: dict) -> str:
    if result.get("status") == "blocked":
        return "blocked"
    if result.get("status") == "none_found":
        return "fresh"
    # Linear is a live system read; Knowledge base is a freshly read curated store.
    return "live" if surface == "linear" else "fresh"


def _surface_record(surface: str, result: dict, *, required: bool = True) -> dict:
    blocked = result.get("status") == "blocked"
    return {
        "surface": surface,
        "required": required,
        "checked": not blocked,
        "checked_at": result.get("checked_at"),
        "freshness": _freshness_for(surface, result),
        "status": "blocked" if blocked else ("none_found" if result.get("status") == "none_found" else "checked"),
        "blocker_or_exclusion": result.get("blocker") if blocked else None,
    }


def _match_records(surface: str, result: dict) -> list[dict]:
    """Normalise raw matches into schema records.

    Raises ValueError on a malformed match (e.g. missing ``ref``) so the caller
    can convert it into a blocked surface / material gap instead of emitting a
    silently-clean context.
    """
    records = []
    raw_matches = result.get("matches", [])
    if not isinstance(raw_matches, list):
        raise ValueError(f"{surface} runner returned non-list matches")
    for raw in raw_matches:
        if not isinstance(raw, dict) or "ref" not in raw:
            raise ValueError(f"{surface} match missing required 'ref'")
        ref = raw["ref"]
        relationship = raw.get("relationship", "not_relevant")
        if relationship not in VALID_RELATIONSHIPS:
            relationship = "not_relevant"
        records.append(
            {
                "prior_work_id": _pw_id(surface, ref),
                "surface": surface,
                "ref": ref,
                "title": raw.get("title", ref),
                "relationship": relationship,
                "observed_state": raw.get("observed_state", "unknown"),
                "observed_at": raw.get("observed_at", ""),
                "evidence_hash": _evidence_hash(surface, ref, raw),
                "owner": raw.get("owner"),
                "reusable_assets": list(raw.get("reusable_assets", [])),
                "decision_or_gate": raw.get("decision_or_gate"),
                "notes": raw.get("notes"),
            }
        )
    return records


def scan_relevant_history(
    *,
    campaign_id: str,
    query_terms: list[str],
    linear_runner: Callable[[list[str]], dict],
    knowledge_base_runner: Callable[[list[str]], dict],
    now: str,
    extra_runners: dict[str, Callable[[list[str]], dict]] | None = None,
) -> dict:
    """Run injected read-only runners and assemble a prior-work-context-v1 dict.

    No network, credential, or mutation happens here; all IO is delegated to the
    injected runners. The returned dict is a fresh object; ``query_terms`` is not
    mutated. ``linear`` and ``knowledge_base`` are the required surfaces; any additional
    surface (sessions, source_corpus, repositories, runtime, skills,
    feedback_register) may be supplied through ``extra_runners`` and is treated as
    optional (a none_found result on an optional surface does not block).

    A runner that raises, or returns malformed match data, is treated as a
    read-only access gap: the surface is recorded as blocked (never a crash), and
    a required blocked surface produces a material gap and HOLD.
    """
    terms = list(query_terms)  # defensive copy; never mutate caller input
    runners: list[tuple[str, bool, Callable[[list[str]], dict]]] = [
        ("linear", True, linear_runner),
        ("knowledge_base", True, knowledge_base_runner),
    ]
    for name, runner in (extra_runners or {}).items():
        runners.append((name, False, runner))

    surfaces: list[dict] = []
    matches: list[dict] = []
    material_gaps: list[dict] = []

    for surface, required, runner in runners:
        try:
            result = runner(terms)
            surface_matches = _match_records(surface, result)
        except Exception as exc:  # noqa: BLE001 - a runner failure is an access gap, never a crash
            result = {"status": "blocked", "checked_at": None, "blocker": f"{type(exc).__name__}: {exc}"}
            surface_matches = []
        surfaces.append(_surface_record(surface, result, required=required))
        matches.extend(surface_matches)
        if result.get("status") == "blocked" and required:
            material_gaps.append(
                {
                    "surface": surface,
                    "impact": f"required prior-work surface {surface} inaccessible: {result.get('blocker', 'unknown blocker')}",
                    "route": "HOLD",
                }
            )

    # Sort matches by stable id so the artifact is byte-deterministic regardless
    # of discovery order across surfaces.
    matches.sort(key=lambda m: m["prior_work_id"])

    status = "HOLD" if material_gaps else "PASS"

    context_to_use = sorted(
        {asset for m in matches for asset in m["reusable_assets"]}
    )

    return {
        "schema_version": "prior-work-context-v1",
        "campaign_id": campaign_id,
        "query_terms": terms,
        "surfaces": surfaces,
        "matches": matches,
        "material_gaps": material_gaps,
        "context_to_use": context_to_use,
        "context_to_exclude": [],
        "status": status,
        "generated_at": now,
    }


# --------------------------------------------------------------------------- #
# CLI (fixture-driven; performs NO network call)                              #
# --------------------------------------------------------------------------- #

def _fixture_runner(payload: dict) -> Callable[[list[str]], dict]:
    def _runner(_query_terms: list[str]) -> dict:
        return payload

    return _runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a prior-work-context-v1 artifact from injected read-only lookups. "
        "In --fixture mode the lookup results are read from a local JSON file so no "
        "network call is performed."
    )
    parser.add_argument("--fixture", required=True, help="Path to a JSON fixture with campaign_id, query_terms, now, linear, knowledge_base")
    parser.add_argument("--out", default="", help="Optional path to write the prior-work-context JSON")
    args = parser.parse_args(argv)

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    ctx = scan_relevant_history(
        campaign_id=fixture["campaign_id"],
        query_terms=fixture["query_terms"],
        linear_runner=_fixture_runner(fixture["linear"]),
        knowledge_base_runner=_fixture_runner(fixture["knowledge_base"]),
        now=fixture["now"],
    )
    output = json.dumps(ctx, indent=2) + "\n"
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output, encoding="utf-8")
    print(output, end="")
    return 0 if ctx["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
