#!/usr/bin/env python3
"""Fail-closed controls for research review and bounded repair.

The functions are deterministic and side-effect free. They operate on saved
review records, explicit dependency graphs, exact file lists and question-bound
source candidates. Publication runners can call them before expensive QA.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SCHEMA_VERSION = "research-qa-repair-v1"
DECISION_CLASSES = {
    "apply_correction",
    "already_resolved",
    "mistaken_reviewer",
    "optional_style",
}


def _normalise_note(value: Any) -> str:
    return " ".join(str(value or "").split())


def evaluate_editor_review(
    review: dict,
    *,
    evidence_decisions: list[dict],
    decisions_brief_sha256: str | None = None,
    reader_text: str | None = None,
    retained_findings: list[str] | None = None,
    classification_checks: dict[str, dict] | None = None,
) -> dict:
    """Decide if publication QA may start for one editor review.

    A PASS label is insufficient while any editor note lacks an evidence-backed
    decision. Proposed corrections explicitly found unsupported are rejected.
    """
    if decisions_brief_sha256 is not None and decisions_brief_sha256 != review.get("brief_sha256"):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "BLOCKED_STALE_DECISIONS",
            "publication_qa_allowed": False,
            "findings": ["editor-note decisions do not match current brief_sha256"],
            "decisions": [],
        }

    if (not isinstance(evidence_decisions, list)
            or any(not isinstance(item, dict) for item in evidence_decisions)
            or any(not isinstance(review.get(key, []), list) for key in ("editor_blocking", "editor_minor"))):
        return {"status": "BLOCKED_INVALID_DECISIONS", "publication_qa_allowed": False,
                "requires_rewrite": False, "findings": ["malformed review or decisions"], "decisions": []}
    blocking = [_normalise_note(note) for note in review.get("editor_blocking", []) if _normalise_note(note)]
    notes = list(dict.fromkeys([_normalise_note(note) for note in
                              [*review.get("editor_minor", []), *(retained_findings or [])]
                              if _normalise_note(note)]))
    classification_checks = classification_checks or {}
    by_note: dict[str, dict] = {}
    duplicate_decisions: list[str] = []
    for decision in evidence_decisions:
        note = _normalise_note(decision.get("note"))
        if not note:
            continue
        if note in by_note:
            duplicate_decisions.append(note)
        by_note[note] = decision

    if str(review.get("status") or "").upper() != "PASS" or blocking:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "BLOCKED_EDITOR",
            "publication_qa_allowed": False,
            "findings": blocking or ["editor review did not pass"],
        }

    unresolved: list[str] = []
    unsupported: list[str] = []
    invalid: list[str] = list(duplicate_decisions)
    decisions: list[dict] = []
    for note in notes:
        decision = by_note.get(note)
        if not decision:
            unresolved.append(note)
            continue
        classification = str(decision.get("classification") or "")
        refs = decision.get("evidence_refs")
        evidence_refs = refs if isinstance(refs, list) and all(isinstance(ref, str) and ref.strip() for ref in refs) else []
        if classification not in DECISION_CLASSES or not evidence_refs:
            invalid.append(note)
            continue
        verdict = decision.get("evidence_verdict")
        if verdict != "supported":
            (unsupported if verdict in ("unsupported", "partial") else invalid).append(note)
        check = classification_checks.get(note, {})
        checked = (check.get("note") == note and check.get("classification") == classification
                   and check.get("evidence_verdict") == "supported"
                   and check.get("classification_verdict") == "supported")
        if classification == "apply_correction":
            applied = checked and check.get("correction_applied") is True and check.get("factual_impact") == "resolved"
            if not applied:
                unresolved.append(note)
        else:
            applied = True
            impact = {"optional_style": "none", "already_resolved": "resolved", "mistaken_reviewer": "reviewer_error"}[classification]
            if not checked or check.get("factual_impact") != impact:
                invalid.append(note)
        if classification == "mistaken_reviewer" and reader_text is not None:
            forbidden = str(decision.get("forbidden_reader_phrase") or "").strip()
            if forbidden and forbidden in reader_text:
                unsupported.append(note)
        decisions.append({
            "note": note,
            "classification": classification,
            "evidence_refs": evidence_refs,
            "evidence_verdict": verdict,
            "requires_rewrite": classification == "apply_correction" and not applied,
        })

    unexpected = sorted(set(by_note) - set(notes))
    invalid.extend(unexpected)
    if unsupported:
        status, findings = "BLOCKED_UNSUPPORTED_CORRECTION", unsupported
    elif unresolved:
        status, findings = "BLOCKED_FACTUAL_FINDINGS", unresolved
    elif invalid:
        status, findings = "BLOCKED_INVALID_DECISIONS", sorted(set(invalid))
    else:
        status, findings = "PASS", []
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "publication_qa_allowed": status == "PASS",
        "requires_rewrite": any(d.get("requires_rewrite") for d in decisions),
        "findings": findings,
        "decisions": decisions,
    }


def validate_section_qualifications(
    section_text: str,
    *,
    required_qualifiers: list[dict] | None = None,
    forbidden_phrases: list[str] | None = None,
) -> dict:
    """Check a reader section for lost uncertainty/date/scope qualifications and banned phrases."""
    failures: list[str] = []
    for rule in required_qualifiers or []:
        unqualified = str(rule.get("unqualified") or "").strip()
        qualified = str(rule.get("qualified") or "").strip()
        if unqualified and unqualified in section_text and (not qualified or qualified not in section_text):
            failures.append(f"lost qualification: found {unqualified!r} without {qualified!r}")
        elif qualified and qualified not in section_text and rule.get("required", False):
            failures.append(f"missing required qualification: {qualified!r}")
    for phrase in forbidden_phrases or []:
        if phrase and phrase in section_text:
            failures.append(f"unsupported phrase present: {phrase!r}")
    return {
        "status": "PASS" if not failures else "BLOCKED_SECTION_QUALIFICATION",
        "failures": failures,
    }


def find_phrase_occurrences(files_by_path: dict[str, str], phrases: Iterable[str]) -> dict:
    """Search all occurrences of phrases across reader sections and files."""
    hits: list[dict] = []
    for path, content in sorted(files_by_path.items()):
        current_section = "(preamble)"
        for line_no, line in enumerate(content.splitlines(), 1):
            if line.startswith("#"):
                current_section = line.lstrip("#").strip()
            for phrase in phrases:
                if phrase and phrase in line:
                    hits.append({
                        "path": path,
                        "line": line_no,
                        "section": current_section,
                        "phrase": phrase,
                    })
    return {
        "status": "PASS" if not hits else "BLOCKED_REMAINING_OCCURRENCES",
        "occurrence_count": len(hits),
        "hits": hits,
    }


def validate_grant_rules_unit(unit: dict) -> dict:
    """Verify a single-grant rules sheet before cross-grant comparison."""
    failures: list[str] = []
    for field in ("grant_id", "amount", "application_status", "programme_period", "project_limit"):
        if not str(unit.get(field) or "").strip():
            failures.append(f"missing {field}")
    if unit.get("programme_period") and unit.get("programme_period") == unit.get("project_limit"):
        failures.append("programme_period and project_limit must remain distinct")
    versions = unit.get("official_versions") or []
    if len(versions) > 1:
        measures = {str(v.get("financial_test") or "").strip() for v in versions if str(v.get("financial_test") or "").strip()}
        if len(measures) < len(versions):
            failures.append("conflicting official versions must preserve distinct financial tests")
        if not unit.get("conflict_unresolved"):
            failures.append("conflicting official versions must mark conflict_unresolved=true")
        if not str(unit.get("applicant_reliance_warning") or "").strip():
            failures.append("conflicting official versions require applicant_reliance_warning")
    return {
        "status": "PASS" if not failures else "BLOCKED_GRANT_UNIT",
        "failures": failures,
    }


def verify_append_only_index(
    *,
    index_bytes: bytes,
    expected_original_sha256: str,
    original_prefix_bytes: int,
) -> dict:
    """Verify that a source index preserves its original byte prefix when derived views are appended."""
    prefix = index_bytes[:original_prefix_bytes]
    prefix_sha = hashlib.sha256(prefix).hexdigest()
    if prefix_sha != expected_original_sha256:
        return {
            "status": "BLOCKED_INDEX_DRIFT",
            "original_prefix_matches": False,
            "computed_prefix_sha256": prefix_sha,
        }
    return {
        "status": "PASS",
        "original_prefix_matches": True,
        "computed_prefix_sha256": prefix_sha,
        "current_sha256": hashlib.sha256(index_bytes).hexdigest(),
    }


def validate_worker_cli_command(cmd: list[str]) -> dict:
    """Reject creation-only flags (--max-turns, --run-budget) when --resume is used."""
    tokens = [str(t) for t in cmd]
    has_resume = any(t == "--resume" or t.startswith("--resume=") for t in tokens)
    forbidden_on_resume = []
    if has_resume:
        for flag in ("--max-turns", "--run-budget"):
            if any(t == flag or t.startswith(flag + "=") for t in tokens):
                forbidden_on_resume.append(flag)
    return {
        "status": "PASS" if not forbidden_on_resume else "BLOCKED_RESUME_FLAGS",
        "forbidden_flags": forbidden_on_resume,
    }


def plan_targeted_repair(*, changed_units: Iterable[str], dependency_graph: dict[str, list[str]],
                         checked_units: Iterable[str]) -> dict:
    """Invalidate changed units and only their transitive dependants."""
    changed = set(changed_units)
    known = set(dependency_graph)
    for dependants in dependency_graph.values():
        known.update(dependants)
    unknown = sorted(changed - known)
    if unknown:
        return {"status": "BLOCKED_UNKNOWN_UNIT", "unknown": unknown, "recheck": [], "reuse": []}

    affected = set(changed)
    frontier = list(changed)
    while frontier:
        current = frontier.pop(0)
        for dependant in dependency_graph.get(current, []):
            if dependant not in affected:
                affected.add(dependant)
                frontier.append(dependant)

    checked = set(checked_units)
    return {
        "status": "PASS",
        "recheck": sorted(affected),
        "reuse": sorted(checked - affected),
    }


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _exact_file(path: Path, root: Path) -> tuple[Path, str]:
    root = root.resolve(strict=True)
    if ".." in path.parts or "\\" in str(path):
        raise ValueError(f"unsafe review input path: {path}")
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"review input escapes root: {path}") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink not allowed in review input: {path}")
    if not candidate.is_file():
        raise ValueError(f"review input must be a regular non-symlink file: {path}")
    resolved = candidate.resolve(strict=True)
    try:
        rel = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"review input escapes root: {path}") from exc
    pure = PurePosixPath(rel.as_posix())
    if ".." in pure.parts:
        raise ValueError(f"unsafe review input path: {path}")
    return resolved, pure.as_posix()


def build_review_packet(paths: Iterable[Path | str], *, root: Path) -> dict:
    """Bind an exact admitted file list. Neighbouring files are never discovered."""
    files = []
    seen = set()
    for raw in paths:
        path, rel = _exact_file(Path(raw), root)
        if rel in seen:
            continue
        seen.add(rel)
        if rel == "manifest.json":
            snapshot = semantic_manifest_snapshot(_read_object(root, rel))
            files.append({"path": rel, "semantic_manifest": snapshot})
        else:
            files.append({"path": rel, "sha256": _sha256(path), "bytes": path.stat().st_size})
    if not files:
        raise ValueError("review packet requires at least one exact input file")
    return {"schema_version": SCHEMA_VERSION, "files": files}


def verify_review_packet(packet: dict, *, root: Path) -> dict:
    """Recheck only files named by a versioned review packet."""
    changed, missing = [], []
    files = packet.get("files") if isinstance(packet, dict) else None
    if not isinstance(files, list) or not files or any(not isinstance(item, dict) for item in files):
        return {"status": "BLOCKED_CHANGED_INPUT", "changed": [], "missing": ["invalid or empty input packet"]}
    seen = set()
    for item in files:
        rel = str(item.get("path") or "")
        if not rel or Path(rel).is_absolute() or rel in seen:
            missing.append(rel or "empty path")
            continue
        seen.add(rel)
        try:
            path, checked_rel = _exact_file(Path(rel), root)
        except ValueError:
            missing.append(rel)
            continue
        try:
            if rel == "manifest.json":
                matches = _same_json(semantic_manifest_snapshot(_read_object(root, rel)), item.get("semantic_manifest"))
            else:
                matches = _sha256(path) == item.get("sha256")
            if checked_rel != rel or not matches:
                changed.append(rel)
        except (OSError, ValueError, TypeError):
            changed.append(rel)
    status = "PASS" if not changed and not missing else "BLOCKED_CHANGED_INPUT"
    return {"status": status, "changed": sorted(changed), "missing": sorted(missing)}


def select_sources(*, unanswered_questions: list[str], candidates: list[dict],
                   preferred_by_question: dict[str, str] | None = None) -> dict:
    """Select one preferred source plus independent corroboration per question.

    Candidates that answer no current question are ignored. Duplicate URLs or
    records from the same lineage do not count as independent origins.
    """
    preferred_by_question = preferred_by_question or {}
    selected: list[str] = []
    independent_counts: dict[str, int] = {}
    unresolved: list[str] = []
    by_id = {str(c.get("source_id")): c for c in candidates if c.get("source_id")}

    for question in unanswered_questions:
        relevant = [c for c in candidates if question in (c.get("question_ids") or [])]
        if not relevant:
            unresolved.append(question)
            independent_counts[question] = 0
            continue
        preferred = by_id.get(preferred_by_question.get(question, ""))
        if preferred not in relevant:
            preferred = relevant[0]
        preferred_id = str(preferred["source_id"])
        if preferred_id not in selected:
            selected.append(preferred_id)

        used_lineages = {str(preferred.get("lineage_group") or preferred_id)}
        independent = []
        for candidate in relevant:
            cid = str(candidate.get("source_id"))
            lineage = str(candidate.get("lineage_group") or cid)
            if not candidate.get("independent") or lineage in used_lineages:
                continue
            used_lineages.add(lineage)
            independent.append(candidate)
        if independent:
            cid = str(independent[0]["source_id"])
            if cid not in selected:
                selected.append(cid)
        independent_counts[question] = len({str(c.get("lineage_group") or c.get("source_id")) for c in independent})

    return {
        "schema_version": SCHEMA_VERSION,
        "selected_ids": selected,
        "independent_origins_by_question": independent_counts,
        "unresolved_questions": unresolved,
    }


def validate_reference_sources_section(pack_text: str) -> dict:
    """Ensure the reference pack does not contain duplicate ## Sources headings."""
    headings = re.findall(r"^##\s+Sources\s*$", pack_text, flags=re.M | re.I)
    if len(headings) > 1:
        return {
            "status": "BLOCKED_DUPLICATE_SOURCES_HEADING",
            "heading_count": len(headings),
            "failures": [f"found {len(headings)} '## Sources' headings; expected at most 1"],
        }
    return {
        "status": "PASS",
        "heading_count": len(headings),
        "failures": [],
    }


def confined_output_path(root: Path, rel: str) -> Path:
    """Validate an explicit output name, including nonexistent paths, without writes."""
    root = Path(root).resolve(strict=True)
    if (not isinstance(rel, str) or not rel or Path(rel).is_absolute()
            or '..' in Path(rel).parts or '\\' in rel or Path(rel).as_posix() != rel):
        raise ValueError(f"unsafe output path: {rel!r}")
    target = root / rel
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"symlink not allowed in output path: {rel}")
    return target


def prepare_preexport_assurance_stub(
    root: Path,
    *,
    brief_sha256: str,
    pack_sha256: str,
    assurance_rel: str = "qa/final-assurance.json",
) -> dict:
    """Reset stale final-assurance hash bindings before pre-export QA runs."""
    root = Path(root).resolve(strict=True)
    target = confined_output_path(root, assurance_rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if target.is_file() and not target.is_symlink():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    stale = (
        existing.get("reference_pack_sha256") != pack_sha256
        or existing.get("brief_sha256") not in (None, brief_sha256)
    )
    stub = {
        "schema_version": str(existing.get("schema_version") or "deep-research-final-assurance-v1"),
        "status": "PENDING" if stale or str(existing.get("status") or "").upper() not in {"PASS", "PASSED"} else existing.get("status"),
        "reviewer": existing.get("reviewer"),
        "producers": existing.get("producers") or ["controller"],
        "reference_pack_sha256": pack_sha256,
        "brief_sha256": brief_sha256,
    }
    if stale:
        target.write_text(json.dumps(stub, indent=2) + "\n", encoding="utf-8")
    return {"status": "RESET" if stale else "UNCHANGED", "assurance_path": assurance_rel, "stub": stub}


__all__ = [
    "DECISION_CLASSES",
    "campaign_readiness",
    "build_pending_editor_decisions",
    "semantic_manifest_snapshot",
    "confined_output_path",
    "evaluate_editor_review",
    "validate_section_qualifications",
    "find_phrase_occurrences",
    "validate_grant_rules_unit",
    "plan_targeted_repair",
    "build_review_packet",
    "verify_review_packet",
    "verify_append_only_index",
    "validate_worker_cli_command",
    "select_sources",
    "validate_reference_sources_section",
    "prepare_preexport_assurance_stub",
    "replay_saved",
]


def validate_saved_review_bindings(root: Path, review: dict, decisions: dict) -> dict:
    """Bind both reader files, every evidence file and independently checked dispositions.

    `classification_review` names an already saved evidence judgement, not a new
    review stage. A label authored in the decisions file is not that judgement.
    """
    findings, inputs, checks = [], [], {}
    for path_key, hash_key in (("brief_path", "brief_sha256"), ("full_pack_path", "full_pack_sha256")):
        try:
            path, rel = _exact_file(Path(review[path_key]), root)
            inputs.append(rel)
            actual = _sha256(path)
            if actual != review.get(hash_key) or actual != decisions.get(hash_key):
                findings.append(f"{hash_key} does not bind current reader bytes")
        except (KeyError, TypeError, ValueError, OSError) as exc:
            findings.append(f"invalid {path_key}: {exc}")
    entries = decisions.get("decisions")
    if not isinstance(entries, list):
        findings.append("decisions must be a list")
        entries = []
    for decision in entries:
        if not isinstance(decision, dict):
            findings.append("decision must be an object")
            continue
        refs, rows = decision.get("evidence_refs"), decision.get("evidence_bindings")
        if (not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref for ref in refs)
                or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)):
            findings.append("decision evidence requires explicit references and bindings")
            continue
        bindings = {item.get("path"): item.get("sha256") for item in rows if isinstance(item.get("path"), str)}
        if len(bindings) != len(rows) or set(refs) != set(bindings) or len(set(refs)) != len(refs):
            findings.append("decision evidence references and bindings must match exactly")
        for ref in refs:
            try:
                path, rel = _exact_file(Path(ref), root)
                inputs.append(rel)
                if rel != ref or _sha256(path) != bindings.get(rel):
                    findings.append(f"changed or unbound decision evidence: {rel}")
            except (TypeError, ValueError, OSError) as exc:
                findings.append(f"invalid decision evidence: {exc}")
        check_ref = decision.get("classification_review")
        if not isinstance(check_ref, str) or check_ref not in refs:
            findings.append(f"missing independent classification evidence: {decision.get('note')}")
            continue
        try:
            check = _read_object(root, check_ref)
            note = _normalise_note(decision.get("note"))
            if check.get("note") != note or check.get("classification") != decision.get("classification"):
                raise ValueError("classification judgement does not match decision")
            for key in ("brief_sha256", "full_pack_sha256"):
                if check.get(key) != review.get(key):
                    raise ValueError(f"classification judgement is stale: {key}")
            if check.get("evidence_verdict") != "supported" or not str(check.get("rationale") or "").strip():
                raise ValueError("classification judgement is missing, partial or unsupported")
            validate_served_judgement(root, review, check)
            evidence_rows = check.get("evidence_bindings")
            if not isinstance(evidence_rows, list) or not evidence_rows:
                raise ValueError("classification judgement binds no supporting evidence")
            for row in evidence_rows:
                if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                        or row["path"] not in bindings or row["path"] == check_ref
                        or not isinstance(row.get("sha256"), str)
                        or row["sha256"] != bindings.get(row["path"])):
                    raise ValueError("classification judgement evidence is not bound")
            checks[note] = check
        except (KeyError, TypeError, ValueError, OSError) as exc:
            findings.append(f"invalid classification evidence: {exc}")
    return {"status": "BLOCKED_CHANGED_INPUT" if findings else "PASS",
            "findings": findings, "inputs": list(dict.fromkeys(inputs)), "classification_checks": checks}


EXPORT_BOOKKEEPING = ("status", "exported_at", "exports", "prior_exports", "overwrite_approved")


def _same_json(left: Any, right: Any) -> bool:
    """JSON identity, without Python's True == 1 and 1 == 1.0 coercion."""
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def semantic_manifest_snapshot(manifest: dict) -> dict:
    """Freeze every semantic field, excluding exactly five exporter JSON pointers."""
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    snapshot = json.loads(json.dumps(manifest, allow_nan=False))
    if isinstance(snapshot.get("deliverable_exports"), dict):
        for key in EXPORT_BOOKKEEPING:
            snapshot["deliverable_exports"].pop(key, None)
    return snapshot


def _read_object(root: Path, rel: str) -> dict:
    if not isinstance(rel, str) or not rel or Path(rel).is_absolute():
        raise ValueError(f"relative input path required: {rel!r}")
    path, checked = _exact_file(Path(rel), root)
    if checked != rel:
        raise ValueError(f"noncanonical input path: {rel}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON object required: {rel}")
    return data


def served_review_identity_errors(review: dict) -> list[str]:
    """Offline structure/capability boundary; provider authentication is still required at delivery."""
    import receipt_integrity
    from research_settings import load_settings
    provenance = review.get('provenance')
    if not isinstance(provenance, dict) or provenance.get('schema_version') != receipt_integrity.SERVED_CALLS_SCHEMA:
        return ['current review requires actual served-call identities']
    errors = receipt_integrity._served_call_contract(review, provenance)
    if errors:
        return errors
    try:
        settings = load_settings(environ={})
        settings.validate_model(review.get('editor_model'), 'brief_reviewer', settings.resolve('brief_reviewer'))
    except (ValueError, TypeError, KeyError) as exc:
        return [f'final editor capability is unverified: {exc}']
    return []


JUDGEMENT_FIELDS = ('note', 'classification', 'classification_verdict', 'evidence_verdict',
                    'factual_impact', 'correction_applied', 'rationale', 'evidence_bindings')


def validate_served_judgement(root: Path, review: dict, check: dict) -> None:
    """A local label is not a judgement: require the exact final served editor response.

    The comments-only editor may judge its own comments. Every contributing text
    author, including earlier fallback writers and rewriting editors, is excluded.
    This validates saved raw evidence, not remote authenticity; all these same
    generations remain subject to the normal provider check at completion/export.
    """
    errors = served_review_identity_errors(review)
    if errors:
        raise ValueError('; '.join(errors))
    provenance = review['provenance']
    gid = provenance['final_editor_generation_id']
    if check.get('generation_id') != gid or check.get('reviewer') != review.get('editor_model'):
        raise ValueError('classification reviewer is not the actual final served editor')
    raw_rel = check.get('raw_response_path')
    if raw_rel not in (provenance.get('raw_response_paths') or []):
        raise ValueError('classification raw response is not retained in review provenance')
    raw = _read_object(root, raw_rel)
    call = next(c for c in provenance['attempts'] if c.get('generation_id') == gid)
    if not any(_same_json(c, call) for c in raw.get('calls', [])):
        raise ValueError('classification raw call differs from actual served attempt')
    responses = [r for r in raw.get('raw_responses', []) if isinstance(r, dict) and r.get('id') == gid]
    if len(responses) != 1 or responses[0].get('model') != check['reviewer']:
        raise ValueError('classification raw served identity is missing or ambiguous')
    choices = responses[0].get('choices')
    if (not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict)
            or choices[0].get('finish_reason') != 'stop' or not isinstance(choices[0].get('message'), dict)):
        raise ValueError('classification raw response was not complete')
    # Reuse the application parser locally; schema repair never buys a new call.
    from plain_brief_rewrite import parse_editor_response, brief_body
    parsed = parse_editor_response(choices[0]['message']['content'])
    if (parsed.get('verdict') != 'PASS' or parsed.get('blocking') != []
            or parsed.get('minor', []) != review.get('editor_minor', [])):
        raise ValueError('raw final editor response does not support saved acceptance')
    judgement = {key: check.get(key) for key in JUDGEMENT_FIELDS}
    rows = parsed.get('note_dispositions')
    if (not isinstance(rows, list) or sum(_same_json(row, judgement) for row in rows) != 1
            or check.get('classification_verdict') != 'supported'):
        raise ValueError('classification is not supported by the exact raw served judgement')
    binding = parsed.get('review_binding')
    if (not isinstance(binding, dict) or not _same_json(binding, raw.get('review_binding'))
            or not _same_json(binding, check.get('review_binding'))
            or binding.get('full_pack_sha256') != review.get('full_pack_sha256')):
        raise ValueError('classification input binding differs from served review')
    brief, _ = _exact_file(Path(review['brief_path']), root)
    body_hash = hashlib.sha256(brief_body(brief.read_text(encoding='utf-8')).encode()).hexdigest()
    if binding.get('reader_body_sha256') != body_hash:
        raise ValueError('classification did not review the current reader body')
    evidence = binding.get('evidence_bindings')
    if not isinstance(evidence, list) or not evidence:
        raise ValueError('classification served prompt binds no evidence')
    for row in evidence:
        if not isinstance(row, dict):
            raise ValueError('malformed served evidence binding')
        path, rel = _exact_file(Path(row['path']), root)
        if row['path'] != rel or row.get('sha256') != _sha256(path):
            raise ValueError('served judgement evidence changed')
    if any(not any(_same_json(row, admitted) for admitted in evidence)
           for row in judgement['evidence_bindings']):
        raise ValueError('judgement cites evidence outside its actual review inputs')


def _history_findings(root: Path) -> tuple[list[str], list[str]]:
    """Retain all known notes; a later PASS never silently closes an earlier note."""
    history, inputs, notes = root / "receipts/qa-history", [], []
    if history.is_symlink():
        raise ValueError("symlinked review history")
    if history.exists() and any(path.is_symlink() for path in history.rglob("*")):
        raise ValueError("symlinked review history entry")
    paths = sorted(history.rglob("plain-brief-review.json")) if history.exists() else []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        record = _read_object(root, rel)
        inputs.append(rel)
        for key in ("editor_minor", "editor_blocking", "retained_findings"):
            values = record.get(key, [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise ValueError(f"invalid historical findings: {rel}")
            notes.extend(_normalise_note(value) for value in values if _normalise_note(value))
    return list(dict.fromkeys(notes)), inputs


def build_pending_editor_decisions(root: Path, *, review: dict,
                                   previous_decisions: dict | None = None,
                                   review_rel: str = "qa/plain-brief-review.json",
                                   decisions_rel: str = "qa/editor-note-decisions.json") -> dict:
    """Build a pending sidecar without writes; callers archive before replacing it.

    This deliberately does not carry acceptance forward. Every retained finding
    must be rechecked against current reader bytes and bound supporting evidence.
    """
    root = Path(root).resolve(strict=True)
    manifest = _read_object(root, "manifest.json")
    retained, history_inputs = _history_findings(root)
    if previous_decisions is None and (root / decisions_rel).exists():
        previous_decisions = _read_object(root, decisions_rel)
    previous = previous_decisions or {}
    if not isinstance(previous, dict):
        raise ValueError("previous decisions must be an object")
    if not _same_json(_read_object(root, review_rel), review):
        raise ValueError("pending decisions require the exact saved current editor review")
    for key in ("retained_findings",):
        retained.extend(previous.get(key) or [])
    retained.extend(item.get("note") for item in previous.get("decisions", []) if isinstance(item, dict))
    current_retained = review.get("retained_findings", [])
    if not isinstance(current_retained, list) or any(not isinstance(note, str) for note in current_retained):
        raise ValueError("current retained findings must be a list of notes")
    retained.extend(current_retained)
    retained.extend(review.get("editor_minor", []))
    retained.extend(review.get("editor_blocking", []))
    retained = list(dict.fromkeys(_normalise_note(note) for note in retained if _normalise_note(note)))
    current_evidence = review.get("evidence_inputs", [])
    if not isinstance(current_evidence, list) or any(not isinstance(path, str) for path in current_evidence):
        raise ValueError("current evidence inputs must be a list of paths")
    paths = [review["brief_path"], review["full_pack_path"], review_rel, "manifest.json", *history_inputs, *current_evidence]
    for decision in previous.get("decisions", []):
        paths.extend(decision.get("evidence_refs") or [])
    packet = build_review_packet(paths, root=root)
    hashes = {item["path"]: item["sha256"] for item in packet["files"] if "sha256" in item}
    for path_key, hash_key in (("brief_path", "brief_sha256"), ("full_pack_path", "full_pack_sha256")):
        if hashes.get(review[path_key]) != review.get(hash_key):
            raise ValueError(f"pending decisions cannot bind stale {path_key}")
    return {"schema_version": "research-editor-note-decisions-v2", "status": "pending",
            "brief_sha256": review["brief_sha256"], "full_pack_sha256": review["full_pack_sha256"],
            "semantic_manifest": semantic_manifest_snapshot(manifest), "retained_findings": retained,
            "review_inputs": packet, "decisions": []}


def campaign_readiness(root: Path, *, require_brief: bool = True,
                       review_rel: str | None = None,
                       decisions_rel: str = "qa/editor-note-decisions.json") -> dict:
    """One side-effect-free acceptance gate for publication, verification and delivery.

    `require_brief=False` is the pre-generation reference-pack check only, not
    publication acceptance. Optional legacy briefs still require a valid pack.
    No provider, quote, acquisition or privacy check is replaced by this gate.
    """
    inputs = []
    try:
        root = Path(root).expanduser().resolve(strict=True)
        manifest = _read_object(root, "manifest.json")
        from report_purpose import resolve_report_purpose
        purpose = resolve_report_purpose(manifest, root)
        configured_brief = manifest.get('plain_brief')
        configured_brief = configured_brief if isinstance(configured_brief, dict) else {}
        review_rel = review_rel or configured_brief.get('review') or 'qa/plain-brief-review.json'
        v2 = manifest.get("output_layout") == "brief-and-reference-v2"
        pack_rel = "internal/reference-pack.md" if v2 else "internal/full-research-pack.md"
        pack, _ = _exact_file(Path(pack_rel), root)
        inputs.append(pack_rel)
        sources = validate_reference_sources_section(pack.read_text(encoding="utf-8"))
        if sources["status"] != "PASS":
            return {**sources, "publication_qa_allowed": False, "findings": sources["failures"], "inputs": inputs}
        pb = manifest.get("plain_brief")
        optional = not isinstance(pb, dict) or pb.get("required", True) is False
        if optional and (v2 or purpose != 'legacy'):
            raise ValueError("only frozen historical campaigns may omit the current plain brief")
        if not require_brief:
            return {"status": "PASS", "publication_qa_allowed": False, "findings": [],
                    "inputs": inputs, "scope": "reference_pack_only"}
        history_notes, history_inputs = _history_findings(root)
        inputs.extend(history_inputs)
        if optional and not (root / review_rel).exists() and not history_notes:
            # Optional legacy brief does not waive accepted pack semantics.
            receipt_rel = ((manifest.get("qa") or {}).get("receipts") or {}).get("evidence_privacy") or "qa/evidence-privacy-review.json"
            receipt = _read_object(root, receipt_rel)
            if (receipt.get("status") != "PASS" or receipt.get("full_pack_sha256") != _sha256(pack)
                    or not _same_json(receipt.get("semantic_manifest"), semantic_manifest_snapshot(manifest))):
                raise ValueError("legacy pack evidence acceptance is absent or stale")
            return {"status": "PASS", "publication_qa_allowed": True, "findings": [],
                    "inputs": [*inputs, receipt_rel], "scope": "optional_legacy_brief"}
        review = _read_object(root, review_rel)
        decisions = _read_object(root, decisions_rel)
        if purpose != 'legacy':
            identity_errors = served_review_identity_errors(review)
            if identity_errors:
                raise ValueError('; '.join(identity_errors))
        if review.get("full_pack_path") != pack_rel:
            raise ValueError("review does not bind the campaign reference pack")
        expected_brief = (pb or {}).get("path") or "internal/plain-brief.md"
        if review.get("brief_path") != expected_brief:
            raise ValueError("review does not bind the campaign plain brief")
        if not _same_json(decisions.get("semantic_manifest"), semantic_manifest_snapshot(manifest)):
            raise ValueError("semantic manifest changed or acceptance snapshot missing")
        pending_packet = decisions.get("review_inputs")
        if decisions.get('status') != 'reviewed':
            raise ValueError('editor decisions are not reviewed')
        packet_check = verify_review_packet(pending_packet, root=root)
        required_inputs = {review_rel, 'manifest.json', review['brief_path'], review['full_pack_path'],
                           *review.get('evidence_inputs', []), *history_inputs}
        packet_paths = {row.get('path') for row in (pending_packet or {}).get('files', [])}
        if packet_check["status"] != "PASS" or not required_inputs.issubset(packet_paths):
            raise ValueError(f"pending review inputs changed or incomplete: {packet_check}")
        retained = decisions.get("retained_findings", [])
        if not isinstance(retained, list) or any(not isinstance(note, str) for note in retained):
            raise ValueError("retained findings must be a list of notes")
        bound = validate_saved_review_bindings(root, review, decisions)
        if bound["status"] != "PASS":
            return {**bound, "publication_qa_allowed": False}
        gate = evaluate_editor_review(review, evidence_decisions=decisions.get("decisions"),
                decisions_brief_sha256=decisions.get("brief_sha256"),
                reader_text=(root / expected_brief).read_text(encoding="utf-8"),
                retained_findings=[*history_notes, *retained], classification_checks=bound["classification_checks"])
        return {**gate, "inputs": list(dict.fromkeys([*inputs, review_rel, decisions_rel, *bound["inputs"]]))}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"status": "BLOCKED_CAMPAIGN_READINESS", "publication_qa_allowed": False,
                "requires_rewrite": False, "findings": [str(exc)], "inputs": inputs}


def replay_current_saved(root: Path) -> dict:
    """Replay explicitly labelled current campaign records, without inventing history."""
    result = {"status": "BLOCKED_HISTORICAL_INPUTS", "campaign_root": str(root),
              "historical_selection": "explicit_current_campaign", "replay_kind": "current_campaign",
              "scope": "Saved real readers and decisions only. Negative probes are in-memory checks.",
              "rows": []}
    try:
        review_rel, decisions_rel = "qa/plain-brief-review.json", "qa/editor-note-decisions.json"
        review = json.loads((root / review_rel).read_text(encoding="utf-8"))
        decisions = json.loads((root / decisions_rel).read_text(encoding="utf-8"))
        bound = validate_saved_review_bindings(root, review, decisions)
        if bound["status"] != "PASS":
            return {**result, "findings": bound["findings"]}
        reader = (root / review["brief_path"]).read_text(encoding="utf-8")
        gate = evaluate_editor_review(review, evidence_decisions=decisions.get("decisions", []),
                                      decisions_brief_sha256=decisions.get("brief_sha256"), reader_text=reader,
                                      classification_checks=bound["classification_checks"])
        result["gate"] = gate
        result["rows"].append({"case": "saved_real_review_and_decisions", "actual": gate["status"],
                               "pass": gate["publication_qa_allowed"] and not gate.get("requires_rewrite")})
        stale = evaluate_editor_review(review, evidence_decisions=decisions.get("decisions", []),
                                       decisions_brief_sha256="0" * 64)
        result["rows"].append({"case": "negative_probe_stale_decisions", "actual": stale["status"],
                               "pass": stale["status"] == "BLOCKED_STALE_DECISIONS"})
        if review.get("editor_minor"):
            absent = evaluate_editor_review(review, evidence_decisions=[])
            result["rows"].append({"case": "negative_probe_unadjudicated_notes", "actual": absent["status"],
                                   "pass": absent["status"] == "BLOCKED_FACTUAL_FINDINGS"})
            unsupported = [dict(item, classification="apply_correction", evidence_verdict="unsupported")
                           for item in decisions.get("decisions", [])]
            rejected = evaluate_editor_review(review, evidence_decisions=unsupported)
            result["rows"].append({"case": "negative_probe_unsupported_corrections", "actual": rejected["status"],
                                   "pass": rejected["status"] == "BLOCKED_UNSUPPORTED_CORRECTION"})
        inputs = [review_rel, decisions_rel, *bound["inputs"]]
        result["input_packet"] = build_review_packet(inputs, root=root)
        result["selected_inputs"] = {"final_review": review_rel, "decisions": decisions_rel,
                                     "final_brief": review["brief_path"], "full_pack": review["full_pack_path"]}
        result["status"] = "PASS" if all(row["pass"] for row in result["rows"]) else "FAIL"
        result["publication_approval"] = False
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**result, "findings": [f"invalid saved campaign input: {exc}"]}


def replay_saved(root: Path) -> dict:
    """Replay Pass 1 history, never a refreshed Pass 2 final.

    The preserved final review, reader and adjudication evidence are selected
    as one set. An incomplete backup fails closed rather than mixing passes.
    Older campaigns without a backup may use their original final set.
    """
    root = Path(root).expanduser().resolve(strict=True)
    backup = root / "orchestration/pass1-backup"
    current_review = root / "qa/plain-brief-review.json"
    if not backup.exists() and not (root / "receipts/qa-history").exists() and current_review.is_file():
        try:
            record = json.loads(current_review.read_text(encoding="utf-8"))
            if record.get("replay_mode") == "current_campaign":
                return replay_current_saved(root)
        except (OSError, ValueError, TypeError):
            pass  # Historical validation below returns a fail-closed result.
    final_root = backup if backup.exists() else root
    selected = {
        "final_review": final_root / "qa/plain-brief-review.json",
        "final_brief": final_root / "internal/plain-brief.md",
        "adjudication_evidence": final_root / "qa/packaging-review-findings.md",
    }
    result = {
        "status": "BLOCKED_HISTORICAL_INPUTS",
        "campaign_root": str(root),
        "scope": "Saved records only. No model, research, publication or service call.",
        "historical_selection": "preserved_pass1" if final_root == backup else "original_final",
        "selected_inputs": {key: path.relative_to(root).as_posix() for key, path in selected.items()},
        "rows": [],
    }
    missing = [path.relative_to(root).as_posix() for path in selected.values() if not path.is_file()]
    if missing:
        return {**result, "findings": [f"missing historical input: {path}" for path in missing]}
    try:
        final = json.loads(selected["final_review"].read_text(encoding="utf-8"))
        final_brief = selected["final_brief"].read_text(encoding="utf-8")
        wrong_notes = [note for note in final.get("editor_minor", []) if "MC-2" in note]
        if len(wrong_notes) != 1:
            return {**result, "findings": ["historical final must contain exactly one preserved MC-2 note"]}
        if _sha256(selected["final_brief"]) != final.get("brief_sha256"):
            return {**result, "findings": ["historical final brief does not match its saved review"]}
        result["input_packet"] = build_review_packet(selected.values(), root=root)
        rows = result["rows"]
        for generation in range(1, 8):
            path = root / "receipts/qa-history" / f"plain-brief-generation{generation}" / "plain-brief-review.json"
            review = json.loads(path.read_text(encoding="utf-8"))
            gate = evaluate_editor_review(review, evidence_decisions=[])
            expected = "BLOCKED_EDITOR" if generation == 5 else "BLOCKED_FACTUAL_FINDINGS"
            rows.append({"generation": generation, "expected": "BLOCK", "actual": gate["status"],
                         "pass": gate["status"] == expected and not gate["publication_qa_allowed"]})
        evidence_ref = result["selected_inputs"]["adjudication_evidence"]
        decisions = [
            {"note": note,
             "classification": "mistaken_reviewer" if "MC-2" in note else "optional_style",
             "evidence_refs": [evidence_ref],
             "forbidden_reader_phrase": "SCX's Melbourne MC-2" if "MC-2" in note else ""}
            for note in final.get("editor_minor", [])
        ]
        accepted = evaluate_editor_review(final, evidence_decisions=decisions,
                                         decisions_brief_sha256=final.get("brief_sha256"), reader_text=final_brief)
        rows.append({"generation": 8, "expected": "PASS", "actual": accepted["status"],
                     "pass": accepted["publication_qa_allowed"] and not accepted["requires_rewrite"]})
        wrong_note = wrong_notes[0]
        wrong_decisions = [
            {"note": note, "classification": "apply_correction" if note == wrong_note else "optional_style",
             "evidence_refs": [evidence_ref], "evidence_verdict": "unsupported" if note == wrong_note else "supported"}
            for note in final.get("editor_minor", [])
        ]
        rejected = evaluate_editor_review(final, evidence_decisions=wrong_decisions)
        rows.append({"generation": "8-wrong-reviewer-correction", "expected": "BLOCK", "actual": rejected["status"],
                     "pass": rejected["status"] == "BLOCKED_UNSUPPORTED_CORRECTION"})
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {**result, "findings": [f"invalid historical input: {exc}"]}
    result["status"] = "PASS" if all(row["pass"] for row in rows) else "FAIL"
    return result


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--replay-saved", action="store_true", required=True,
                        help="Replay preserved Pass 1 records, not the refreshed final.")
    parser.add_argument("--campaign-root", required=True)
    args = parser.parse_args(argv)
    result = replay_saved(Path(args.campaign_root))
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
