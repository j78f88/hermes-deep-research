#!/usr/bin/env python3
"""Phase 0 pre-seed scaffold and verifier for comparative deep research.

Read/write scope is campaign-local only. Verification is pure/read-only.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

from dataclasses import dataclass
import hashlib
import json
import re
from pathlib import Path
from typing import Any

SCHEMA_PREFIX = "deep-research-"
ARTIFACT_SPECS = {
    "source_characterization": {
        "schema_version": "source-characterization-v1",
        "json": "preseed/source-characterization-v1.json",
        "markdown": "preseed/source-characterization-v1.md",
    },
    "need_map": {
        "schema_version": "need-map-v1",
        "json": "preseed/need-map-v1.json",
        "markdown": "preseed/need-map-v1.md",
    },
    "candidate_universe": {
        "schema_version": "candidate-universe-v1",
        "json": "preseed/candidate-universe-v1.json",
        "markdown": "preseed/candidate-universe-v1.md",
    },
    "comparison_charter": {
        "schema_version": "comparison-charter-v1",
        "json": "preseed/comparison-charter-v1.json",
        "markdown": "preseed/comparison-charter-v1.md",
    },
}
REQUIRED_CANDIDATE_CLASSES = {
    "product",
    "process",
    "architecture",
    "current_state_do_nothing",
    "adjacent_category",
}
REQUIRED_DECISION_STATES = {
    "eligible_set",
    "benchmark_winner",
    "mission_selection",
    "pilot_selection",
    "activation_status",
}
REQUIRED_CHARTER_KEYS = {
    "hard_eligibility_gates",
    "evidence_floor",
    "evidence_budget",
    "source_classes",
    "rubric",
    "blinded_aliases",
    "alias_order",
    "unblind_mapping",
    "duration_provenance",
}
VALID_DURATION_PROVENANCE = {"evidence", "user_constraint", "heuristic", "policy"}
VALID_ACTIVATION_STATUSES = {"blocked_pending_owner_approval", "not_authorized", "not_started", "blocked"}
RUBRIC_NAME_FIELDS = ("criterion", "name")
RUBRIC_IDENTIFIER_FIELDS = ("id", "criterion")
RUBRIC_DEFINITION_FIELDS = ("definition", "scoring_definition", "description")
RUBRIC_SCALE_FIELDS = ("scale", "scoring_scale")
DURATION_VALUE_FIELDS = ("duration", "value", "label")
PASS_GATE_VALUES = {"pass", "passed"}
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SELECTION_FIELD_ACTIONS = {
    "select",
    "choose",
    "recommend",
    "prefer",
    "default",
    "incumbent",
    "adopt",
    "use",
}
SELECTION_FIELD_ACTION_ALIASES = {
    "select": "select",
    "selected": "select",
    "selecting": "select",
    "selection": "select",
    "selections": "select",
    "choose": "choose",
    "chooses": "choose",
    "choosing": "choose",
    "chosen": "choose",
    "choice": "choose",
    "choices": "choose",
    "recommend": "recommend",
    "recommends": "recommend",
    "recommended": "recommend",
    "recommending": "recommend",
    "recommendation": "recommend",
    "recommendations": "recommend",
    "prefer": "prefer",
    "prefers": "prefer",
    "preferred": "prefer",
    "preferring": "prefer",
    "preference": "prefer",
    "preferences": "prefer",
    "default": "default",
    "defaults": "default",
    "defaulted": "default",
    "defaulting": "default",
    "incumbent": "incumbent",
    "incumbents": "incumbent",
    "incumbency": "incumbent",
    "incumbencies": "incumbent",
    "adopt": "adopt",
    "adopts": "adopt",
    "adopted": "adopt",
    "adopting": "adopt",
    "adoption": "adopt",
    "adoptions": "adopt",
    "use": "use",
    "uses": "use",
    "using": "use",
    "used": "use",
}
SELECTION_FIELD_OBJECTS = {"solution", "vendor", "product", "tool", "platform", "architecture", "candidate", "option", "approach"}
SELECTION_FIELD_OBJECT_ALIASES = {
    object_name: object_name for object_name in SELECTION_FIELD_OBJECTS
} | {
    "solutions": "solution",
    "vendors": "vendor",
    "products": "product",
    "tools": "tool",
    "platforms": "platform",
    "architectures": "architecture",
    "candidates": "candidate",
    "options": "option",
    "approaches": "approach",
}
NEUTRAL_SELECTION_PATH_TOKENS = {"before", "pending", "without", "no", "not", "avoid", "forbid", "forbidden"}
SELECTION_ACTION_RE = re.compile(
    r"\b(?:pick|picked|choose|chosen|recommend|recommended|select|selected|adopt|adopted|use|prefer|preferred|default(?:\s+to)?|incumbent|winner)\b",
    re.I,
)
GENERIC_SOLUTION_RE = re.compile(r"\b(?:solution|vendor|product|tool|platform|architecture|candidate|option|approach)\b", re.I)
NEUTRAL_SELECTION_CONTEXT_RE = re.compile(r"\b(?:no|not|without|avoid|forbid(?:s|den)?|before|pending)\b.{0,40}\b(?:select|selected|recommend(?:ed)?|preferred|default|incumbent|winner)\b", re.I)
SCALAR_CANDIDATE_FIELDS = {"id", "name", "class", "source_status", "score", "eligibility"}
PROVENANCE_REF_FIELDS = ("source_ref", "ref")
USER_SUPPLIED_PROVENANCE_TYPES = {"user_supplied"}
INDEPENDENT_PROVENANCE_TYPES = {"independent_search", "current_state", "external_source", "operator_discovery"}


@dataclass(frozen=True)
class PreseedVerdict:
    status: str
    holds: list[str]
    metrics: dict[str, Any]


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def confined_path(root: Path, rel: str) -> tuple[Path, str | None]:
    rel = rel_path(rel)
    relative = Path(rel)
    if not rel or relative.is_absolute() or ".." in relative.parts:
        return root / "__invalid__", f"unsafe_campaign_path {rel!r}"
    root_resolved = root.resolve()
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return current, f"symlink_campaign_path {rel}"
    candidate = root / relative
    try:
        candidate.resolve(strict=False).relative_to(root_resolved)
    except ValueError:
        return candidate, f"campaign_path_escape {rel}"
    return candidate, None


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _json(path: Path, payload: dict[str, Any]) -> None:
    _write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _slug(value: str) -> str:
    return "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in value).split()) or "candidate"


def source_characterization(target: str, primary_source: str, decision_question: str) -> dict[str, Any]:
    return {
        "schema_version": "source-characterization-v1",
        "status": "HOLD",
        "original_framing": {
            "supplied_target": target,
            "primary_source": primary_source or None,
            "decision_question": decision_question,
        },
        "characterization": {
            "role": "user_supplied_seed",
            "neutral_label": "supplied research seed",
            "not_incumbent_default_preferred_or_priority": True,
            "allowed_use": "hypothesis_and_discovery_seed_only",
        },
        "forbidden_inferences": [
            "incumbent",
            "default",
            "preferred",
            "priority",
            "winner",
        ],
    }


def need_map(decision_question: str) -> dict[str, Any]:
    return {
        "schema_version": "need-map-v1",
        "status": "HOLD",
        "neutral_decision_question": decision_question,
        "workloads": [],
        "outcomes": [],
        "constraints": [],
        "solution_selected": False,
        "selection_language_forbidden": True,
    }


def candidate_universe(target: str, primary_source: str) -> dict[str, Any]:
    seed_id = f"seed-{_slug(target)}"
    return {
        "schema_version": "candidate-universe-v1",
        "status": "HOLD",
        "candidates": [
            {
                "id": seed_id,
                "name": target,
                "class": "product",
                "source_status": "user_supplied_seed",
                "discovery_provenance": [
                    {"type": "user_supplied", "ref": primary_source or target}
                ],
                "evidence_refs": [],
            }
        ],
        "required_candidate_classes": sorted(REQUIRED_CANDIDATE_CLASSES),
        "independent_discovery_required": True,
        "coverage_status": "incomplete",
    }


def comparison_charter() -> dict[str, Any]:
    return {
        "schema_version": "comparison-charter-v1",
        "status": "HOLD",
        "frozen": False,
        "hard_eligibility_gates": [],
        "evidence_floor": {},
        "evidence_budget": {},
        "source_classes": [],
        "rubric": [],
        "blinded_aliases": {},
        "alias_order": [],
        "unblind_mapping": {},
        "duration_provenance": [],
        "decision_states": {
            "eligible_set": [],
            "benchmark_winner": None,
            "mission_selection": None,
            "pilot_selection": None,
            "activation_status": "blocked_pending_owner_approval",
        },
        "winner": None,
    }


def scaffold_preseed(root: Path, *, target: str, primary_source: str, decision_question: str) -> dict[str, Any]:
    payloads = {
        "source_characterization": source_characterization(target, primary_source, decision_question),
        "need_map": need_map(decision_question),
        "candidate_universe": candidate_universe(target, primary_source),
        "comparison_charter": comparison_charter(),
    }
    artifacts: dict[str, dict[str, Any]] = {}
    for key, spec in ARTIFACT_SPECS.items():
        json_rel = spec["json"]
        md_rel = spec["markdown"]
        payload = payloads[key]
        _json(root / json_rel, payload)
        title = spec["schema_version"].replace("-", " ").title()
        _write(root / md_rel, f"# {title}\n\nStatus: HOLD. Populate `{json_rel}` before comparison lanes start.\n")
        artifacts[key] = {
            "schema_version": spec["schema_version"],
            "json": json_rel,
            "markdown": md_rel,
            "sha256": sha256_path(root / json_rel),
            "status": payload["status"],
        }
    return {
        "schema_version": "deep-research-pre-seed-gate-v1",
        "status": "HOLD",
        "gate_status": "HOLD",
        "artifacts": artifacts,
        "comparison_lanes_authorized": False,
        "target_specific_lanes_authorized": False,
        "blocks_until": "all four pre-seed artifacts pass validation and comparison charter is frozen",
        "legacy_compatibility": "only manifests without this pre_seed block use legacy verifier behavior",
    }


def load_json(root: Path, rel: str) -> tuple[dict[str, Any] | None, str | None]:
    path, unsafe = confined_path(root, rel)
    if unsafe:
        return None, unsafe
    if not path.is_file():
        return None, f"missing {rel}"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid_json {rel} {type(exc).__name__}"
    if not isinstance(data, dict):
        return None, f"not_object {rel}"
    return data, None


def _is_truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "yes", "pass", "passed", "complete", "verified"}


def _is_failed_gate(value: Any) -> bool:
    return value is False or str(value).strip().lower() in {"false", "fail", "failed", "hold", "blocked", "no", "ineligible"}


def _is_explicit_pass(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in PASS_GATE_VALUES)


def _is_explicit_artifact_pass(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower() in PASS_GATE_VALUES


def _is_placeholder(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or bool(re.match(r"^(?:TBD|TODO|N/?A|placeholder)(?:\s|:|$)", text, re.I))


def _valid_scalar_string(value: Any) -> bool:
    return isinstance(value, str) and not _is_placeholder(value)


def _valid_optional_solution_selected(value: Any) -> bool:
    return value is False or value is None


def _valid_scalar_field(value: Any) -> bool:
    return _is_scalar(value) and not _is_placeholder(value)


def _field_has_valid_scalar(item: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return any(_valid_scalar_field(item.get(field)) for field in fields)


def _validate_source_classes(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return ["pre_seed_source_classes_empty"]
    normalized: list[str] = []
    for item in value:
        if not _valid_scalar_string(item):
            return ["pre_seed_source_classes_invalid"]
        normalized.append(item.strip().lower())
    if len(set(normalized)) != len(normalized):
        return ["pre_seed_source_classes_invalid"]
    return []


def _validate_rubric(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return ["pre_seed_rubric_empty"]
    identifiers: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            return ["pre_seed_rubric_invalid"]
        if not _field_has_valid_scalar(item, RUBRIC_IDENTIFIER_FIELDS):
            return ["pre_seed_rubric_invalid"]
        if not _field_has_valid_scalar(item, RUBRIC_NAME_FIELDS):
            return ["pre_seed_rubric_invalid"]
        if not _field_has_valid_scalar(item, RUBRIC_SCALE_FIELDS):
            return ["pre_seed_rubric_invalid"]
        if "weight" in item and not _is_scalar(item.get("weight")):
            return ["pre_seed_rubric_invalid"]
        for field in (*RUBRIC_IDENTIFIER_FIELDS, *RUBRIC_NAME_FIELDS, *RUBRIC_DEFINITION_FIELDS, *RUBRIC_SCALE_FIELDS):
            if field in item and item.get(field) is not None and not _valid_scalar_field(item.get(field)):
                return ["pre_seed_rubric_invalid"]
        identifier = next((str(item[field]).strip().lower() for field in RUBRIC_IDENTIFIER_FIELDS if _valid_scalar_field(item.get(field))), "")
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers):
        return ["pre_seed_rubric_invalid"]
    return []


def _validate_duration_entries(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        return ["pre_seed_duration_provenance_empty"]
    holds: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            holds.append("pre_seed_duration_provenance_invalid")
            continue
        provenance_raw = item.get("provenance")
        provenance = provenance_raw.strip() if isinstance(provenance_raw, str) else ""
        if provenance not in VALID_DURATION_PROVENANCE:
            holds.append("pre_seed_duration_provenance_invalid")
        if not _field_has_valid_scalar(item, DURATION_VALUE_FIELDS):
            holds.append("pre_seed_duration_value_invalid")
        for field in DURATION_VALUE_FIELDS:
            if field in item and item.get(field) is not None and not _valid_scalar_field(item.get(field)):
                holds.append("pre_seed_duration_value_invalid")
                break
        if provenance == "evidence":
            refs = item.get("evidence_refs")
            if not _valid_evidence_refs(refs) or len(_valid_evidence_refs(refs)) != len(refs if isinstance(refs, list) else []):
                holds.append("pre_seed_duration_unsupported_as_evidence")
        elif "evidence_refs" in item and item.get("evidence_refs") is not None and not isinstance(item.get("evidence_refs"), list):
            holds.append("pre_seed_duration_provenance_invalid")
    return holds


def _candidate_ids(candidates: list[Any]) -> set[str]:
    return {c.get("id").strip() for c in candidates if isinstance(c, dict) and _valid_scalar_string(c.get("id"))}


def _normalized_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _split_key_tokens(key: Any) -> list[str]:
    text = str(key or "")
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "_", text)
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return [part for part in re.split(r"[^A-Za-z0-9]+", text.lower()) if part]


def _normalized_key_parts(key: Any) -> set[str]:
    parts = set(_split_key_tokens(key))
    stems: set[str] = set(parts)
    for part in parts:
        if part in SELECTION_FIELD_ACTION_ALIASES:
            stems.add(SELECTION_FIELD_ACTION_ALIASES[part])
        if part in SELECTION_FIELD_OBJECT_ALIASES:
            stems.add(SELECTION_FIELD_OBJECT_ALIASES[part])
        if part.endswith("ed") and len(part) > 3:
            stems.add(part[:-2])
        if part.endswith("d") and len(part) > 3:
            stems.add(part[:-1])
        if part.endswith("ing") and len(part) > 4:
            stems.add(part[:-3])
        if part.endswith("ion") and len(part) > 5:
            stems.add(part[:-3])
    return stems


def _is_forbidden_need_field(key: Any) -> bool:
    parts = _normalized_key_parts(key)
    return bool(parts & SELECTION_FIELD_ACTIONS and parts & SELECTION_FIELD_OBJECTS)


def _candidate_terms(candidates: list[Any]) -> list[str]:
    terms: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("name", "id"):
            value = str(candidate.get(key) or "").strip()
            if value and not _is_placeholder(value):
                terms.append(value)
    return sorted(set(terms), key=len, reverse=True)


def _contains_candidate_term(text: str, candidate_terms: list[str]) -> bool:
    lowered = text.lower()
    for term in candidate_terms:
        term_l = term.lower()
        if len(term_l) < 3:
            continue
        if re.search(rf"(?<![A-Za-z0-9]){re.escape(term_l)}(?![A-Za-z0-9])", lowered):
            return True
    return False


def _budget_signature(value: Any) -> str:
    if not isinstance(value, dict):
        return "__invalid_budget_record__"
    return _normalized_json(value)


def _selection_language_hit(
    value: Any,
    candidate_terms: list[str] | None = None,
    ancestor_parts: set[str] | None = None,
) -> bool:
    candidate_terms = candidate_terms or []
    ancestor_parts = ancestor_parts or set()
    values = value if isinstance(value, list) else [value]
    for item in values:
        if isinstance(item, dict):
            for key, nested_value in item.items():
                key_parts = _normalized_key_parts(key)
                path_parts = ancestor_parts | key_parts
                if (
                    path_parts & SELECTION_FIELD_ACTIONS
                    and path_parts & SELECTION_FIELD_OBJECTS
                    and not path_parts & NEUTRAL_SELECTION_PATH_TOKENS
                    and key not in {"solution_selected", "selection_language_forbidden"}
                ):
                    return True
                if _selection_language_hit(nested_value, candidate_terms, path_parts):
                    return True
            continue
        if isinstance(item, list):
            if _selection_language_hit(item, candidate_terms, ancestor_parts):
                return True
            continue
        text = str(item or "")
        match = SELECTION_ACTION_RE.search(text)
        if not match:
            continue
        window_start = max(0, match.start() - 40)
        window_end = min(len(text), match.end() + 80)
        window = text[window_start:window_end]
        if NEUTRAL_SELECTION_CONTEXT_RE.search(window):
            continue
        if _contains_candidate_term(window, candidate_terms) or GENERIC_SOLUTION_RE.search(window):
            return True
    return False


def _valid_evidence_refs(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    refs: list[str] = []
    for ref in value:
        if isinstance(ref, str) and not _is_placeholder(ref):
            refs.append(ref)
    return refs


def _normalize_required_gates(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    gates: list[str] = []
    for item in value:
        if not isinstance(item, str) or _is_placeholder(item):
            return gates, False
        gates.append(item)
    return gates, True


def _candidate_signature(candidate: dict[str, Any]) -> tuple[str, ...]:
    gates = candidate.get("hard_gate_results") if isinstance(candidate.get("hard_gate_results"), dict) else {}
    evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
    return (
        _normalized_json(candidate.get("class")),
        _normalized_json(candidate.get("source_status")),
        _normalized_json(evidence),
        _normalized_json(gates),
        _normalized_json(candidate.get("score")),
    )


def _is_scalar(value: Any) -> bool:
    return isinstance(value, (str, int, float)) and not isinstance(value, bool)


def _validate_candidate_schema(candidates: list[Any]) -> list[str]:
    holds: list[str] = []
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            continue
        label = candidate.get("id") if _valid_scalar_string(candidate.get("id")) else candidate.get("name") if _valid_scalar_string(candidate.get("name")) else index
        for field in SCALAR_CANDIDATE_FIELDS:
            if field not in candidate or candidate.get(field) is None:
                continue
            if not _valid_scalar_field(candidate.get(field)):
                holds.append(f"pre_seed_candidate_{field}_invalid {label}")
        for field in ("evidence", "hard_gate_results"):
            if field in candidate and candidate.get(field) is not None and not isinstance(candidate.get(field), dict):
                holds.append(f"pre_seed_candidate_{field}_invalid {label}")
        evidence = candidate.get("evidence")
        if isinstance(evidence, dict):
            for dimension, refs in evidence.items():
                if not _valid_scalar_string(dimension) or not isinstance(refs, list):
                    holds.append(f"pre_seed_candidate_evidence_ref_invalid {label}")
                    continue
                if any(not _valid_scalar_string(ref) for ref in refs):
                    holds.append(f"pre_seed_candidate_evidence_ref_invalid {label}")
        gate_results = candidate.get("hard_gate_results")
        if isinstance(gate_results, dict):
            for gate, result in gate_results.items():
                if not _valid_scalar_string(gate) or result is None or isinstance(result, (dict, list)):
                    holds.append(f"pre_seed_candidate_hard_gate_result_invalid {label}")
        provenance = candidate.get("discovery_provenance")
        if isinstance(provenance, list):
            for item in provenance:
                if not isinstance(item, dict):
                    holds.append(f"pre_seed_candidate_provenance_invalid {label}")
                    continue
                provenance_type = item.get("type")
                if not _valid_scalar_string(provenance_type):
                    holds.append(f"pre_seed_candidate_provenance_type_invalid {label}")
                refs = [item.get(ref_key) for ref_key in PROVENANCE_REF_FIELDS if ref_key in item]
                if not refs or not any(_valid_scalar_field(ref) for ref in refs):
                    holds.append(f"pre_seed_candidate_provenance_ref_invalid {label}")
                elif any(not _valid_scalar_field(ref) for ref in refs if ref is not None):
                    holds.append(f"pre_seed_candidate_provenance_ref_invalid {label}")
                for field, value in item.items():
                    if field in {"type", *PROVENANCE_REF_FIELDS}:
                        continue
                    if not _valid_scalar_field(value):
                        holds.append(f"pre_seed_candidate_provenance_{field}_invalid {label}")
    return holds


def permutation_invariant_digest(universe: dict[str, Any], charter: dict[str, Any]) -> str:
    candidates = universe.get("candidates") if isinstance(universe.get("candidates"), list) else []
    signatures = sorted(_candidate_signature(c) for c in candidates if isinstance(c, dict))
    rubric = charter.get("rubric") if isinstance(charter.get("rubric"), list) else []
    gates = charter.get("hard_eligibility_gates") if isinstance(charter.get("hard_eligibility_gates"), list) else []
    payload = {"candidate_signatures": signatures, "rubric": rubric, "hard_eligibility_gates": gates}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def verify_preseed(root: Path, manifest: dict[str, Any]) -> PreseedVerdict:
    if "pre_seed" not in manifest:
        return PreseedVerdict("LEGACY", [], {"pre_seed_present": False})
    pre = manifest.get("pre_seed")
    if not isinstance(pre, dict):
        return PreseedVerdict("HOLD", ["pre_seed_manifest_invalid"], {"pre_seed_present": True, "pre_seed_status": "HOLD"})
    holds: list[str] = []
    artifacts = pre.get("artifacts") if isinstance(pre.get("artifacts"), dict) else {}
    loaded: dict[str, dict[str, Any]] = {}
    for key, spec in ARTIFACT_SPECS.items():
        meta = artifacts.get(key) if isinstance(artifacts.get(key), dict) else {}
        if meta.get("schema_version") != spec["schema_version"]:
            holds.append(f"pre_seed_{key}_manifest_schema_invalid")
        rel = rel_path(meta.get("json") or spec["json"])
        data, problem = load_json(root, rel)
        if problem:
            holds.append(f"pre_seed_{key}_{problem}")
            continue
        assert data is not None
        loaded[key] = data
        if data.get("schema_version") != spec["schema_version"]:
            holds.append(f"pre_seed_{key}_schema_invalid")
        if not _is_explicit_artifact_pass(data.get("status")):
            holds.append(f"pre_seed_{key}_artifact_status_invalid declared={data.get('status')!r}")
        recorded_sha = meta.get("sha256")
        if not isinstance(recorded_sha, str) or not recorded_sha.strip():
            holds.append(f"pre_seed_{key}_manifest_sha256_missing")
        elif not SHA256_RE.fullmatch(recorded_sha.strip()):
            holds.append(f"pre_seed_{key}_manifest_sha256_malformed")
        else:
            artifact_path, unsafe = confined_path(root, rel)
            if unsafe:
                holds.append(f"pre_seed_{key}_{unsafe}")
            else:
                disk_sha = sha256_path(artifact_path)
                if disk_sha.lower() != recorded_sha.strip().lower():
                    holds.append(f"pre_seed_{key}_manifest_sha256_mismatch recorded={recorded_sha} disk={disk_sha}")

    source = loaded.get("source_characterization") or {}
    raw_char = source.get("characterization")
    char = raw_char if isinstance(raw_char, dict) else {}
    if char.get("role") != "user_supplied_seed":
        holds.append("pre_seed_seed_not_marked_user_supplied_seed")
    if char.get("not_incumbent_default_preferred_or_priority") is not True:
        holds.append("pre_seed_seed_defaulting")
    forbidden_values = source.get("forbidden_inferences")
    forbidden = {str(x).lower() for x in forbidden_values} if isinstance(forbidden_values, list) else set()
    if not {"incumbent", "default", "preferred", "priority"}.issubset(forbidden):
        holds.append("pre_seed_seed_forbidden_inferences_incomplete")

    universe = loaded.get("candidate_universe") or {}
    raw_required_candidate_classes = universe.get("required_candidate_classes")
    if raw_required_candidate_classes is not None:
        if (
            not isinstance(raw_required_candidate_classes, list)
            or any(not _valid_scalar_string(item) for item in raw_required_candidate_classes)
        ):
            holds.append("pre_seed_required_candidate_classes_invalid")
    raw_independent_required = universe.get("independent_discovery_required")
    if raw_independent_required is not None and raw_independent_required is not True:
        holds.append("pre_seed_independent_discovery_required_invalid")
    raw_coverage_status = universe.get("coverage_status")
    if raw_coverage_status is not None and not _valid_scalar_string(raw_coverage_status):
        holds.append("pre_seed_coverage_status_invalid")
    raw_candidates = universe.get("candidates")
    candidates = raw_candidates if isinstance(raw_candidates, list) else []
    holds.extend(_validate_candidate_schema(candidates))
    candidate_terms = _candidate_terms(candidates)

    need = loaded.get("need_map") or {}
    if not _valid_optional_solution_selected(need.get("solution_selected")):
        holds.append("pre_seed_need_map_solution_selected_invalid")
        holds.append("pre_seed_need_map_selects_solution")
    if need.get("selection_language_forbidden") is not True:
        holds.append("pre_seed_need_map_selection_language_not_forbidden")
    forbidden_need_fields = sorted(
        str(key) for key in need
        if _is_forbidden_need_field(key)
        and key not in {"solution_selected", "selection_language_forbidden"}
    )
    if forbidden_need_fields:
        holds.append("pre_seed_need_map_forbidden_selection_fields " + ",".join(forbidden_need_fields))
    for field in ("workloads", "outcomes", "constraints"):
        field_value = need.get(field)
        if not isinstance(field_value, list) or not field_value:
            holds.append(f"pre_seed_need_map_{field}_missing")
        elif _selection_language_hit(field_value, candidate_terms):
            holds.append(f"pre_seed_need_map_selection_language {field}")
    if not candidates:
        holds.append("pre_seed_candidate_universe_empty")
    classes = {str(c.get("class", "")).strip() for c in candidates if isinstance(c, dict) and _is_scalar(c.get("class"))}
    missing_classes = sorted(REQUIRED_CANDIDATE_CLASSES - classes)
    if missing_classes:
        holds.append("pre_seed_candidate_classes_missing " + ",".join(missing_classes))
    independent = [c for c in candidates if isinstance(c, dict) and c.get("source_status") != "user_supplied_seed"]
    if not independent:
        holds.append("pre_seed_independent_candidate_provenance_missing")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            holds.append("pre_seed_candidate_invalid")
            continue
        provenance = candidate.get("discovery_provenance")
        if not isinstance(provenance, list) or not provenance:
            holds.append(f"pre_seed_candidate_provenance_gap {candidate.get('id') or candidate.get('name')}")
            continue
        if candidate.get("source_status") == "user_supplied_seed":
            has_user_supplied_ref = any(
                isinstance(item, dict)
                and isinstance(item.get("type"), str)
                and item.get("type") in USER_SUPPLIED_PROVENANCE_TYPES
                and _valid_scalar_field(item.get("source_ref") or item.get("ref"))
                for item in provenance
            )
            if not has_user_supplied_ref:
                holds.append(f"pre_seed_candidate_user_supplied_provenance_gap {candidate.get('id') or candidate.get('name')}")
        else:
            has_independent_ref = any(
                isinstance(item, dict)
                and isinstance(item.get("type"), str)
                and item.get("type") in INDEPENDENT_PROVENANCE_TYPES
                and _valid_scalar_field(item.get("source_ref") or item.get("ref"))
                for item in provenance
            )
            if not has_independent_ref:
                holds.append(f"pre_seed_candidate_independent_provenance_gap {candidate.get('id') or candidate.get('name')}")

    charter = loaded.get("comparison_charter") or {}
    missing_keys = sorted(key for key in REQUIRED_CHARTER_KEYS if key not in charter)
    if missing_keys:
        holds.append("pre_seed_comparison_charter_keys_missing " + ",".join(missing_keys))
    if not _is_truthy(charter.get("frozen")):
        holds.append("pre_seed_comparison_charter_unfrozen")
    raw_aliases = charter.get("blinded_aliases")
    aliases = raw_aliases if isinstance(raw_aliases, dict) else {}
    raw_alias_order = charter.get("alias_order")
    alias_order = raw_alias_order if isinstance(raw_alias_order, list) else []
    raw_unblind = charter.get("unblind_mapping")
    unblind = raw_unblind if isinstance(raw_unblind, dict) else {}
    candidate_ids = _candidate_ids(candidates)
    if len(aliases) != len(candidates) or len(unblind) != len(candidates) or len(alias_order) != len(candidates):
        holds.append("pre_seed_blinded_controls_incomplete")
    alias_keys_valid = all(_valid_scalar_string(key) for key in aliases)
    alias_values_valid = all(_valid_scalar_string(value) for value in aliases.values())
    alias_order_valid = all(_valid_scalar_string(alias) for alias in alias_order)
    unblind_keys_valid = all(_valid_scalar_string(key) for key in unblind)
    unblind_values_valid = all(_valid_scalar_string(value) for value in unblind.values())
    alias_key_set = {key.strip() for key in aliases if _valid_scalar_string(key)}
    alias_values = [value.strip() for value in aliases.values() if _valid_scalar_string(value)]
    alias_order_values = [alias.strip() for alias in alias_order if _valid_scalar_string(alias)]
    unblind_normalized = {key.strip(): value.strip() for key, value in unblind.items() if _valid_scalar_string(key) and _valid_scalar_string(value)}
    if not alias_keys_valid or alias_key_set != candidate_ids:
        holds.append("pre_seed_blinded_alias_keys_mismatch")
    if (
        not alias_values_valid
        or len(alias_values) != len(aliases)
        or len(set(alias_values)) != len(alias_values)
    ):
        holds.append("pre_seed_blinded_alias_values_invalid")
    if (
        not alias_order_valid
        or len(alias_order_values) != len(alias_order)
        or set(alias_order_values) != set(alias_values)
        or len(alias_order_values) != len(set(alias_order_values))
    ):
        holds.append("pre_seed_alias_order_invalid")
    alias_controls_valid_for_unblind = (
        alias_keys_valid
        and alias_values_valid
        and alias_key_set == candidate_ids
        and len(alias_values) == len(aliases)
        and len(set(alias_values)) == len(alias_values)
    )
    expected_unblind = {
        alias.strip(): candidate_id.strip()
        for candidate_id, alias in aliases.items()
        if _valid_scalar_string(candidate_id) and _valid_scalar_string(alias)
    }
    if (
        not alias_controls_valid_for_unblind
        or not unblind_keys_valid
        or not unblind_values_valid
        or len(unblind_normalized) != len(unblind)
        or unblind_normalized != expected_unblind
    ):
        holds.append("pre_seed_unblind_mapping_invalid")
    raw_budgets = charter.get("evidence_budget")
    budgets = raw_budgets if isinstance(raw_budgets, dict) else {}
    if not budgets:
        holds.append("pre_seed_evidence_budget_empty")
    if budgets:
        if set(map(str, budgets.keys())) != candidate_ids:
            holds.append("pre_seed_evidence_budget_candidate_mismatch")
        if any(not isinstance(value, dict) for value in budgets.values()):
            holds.append("pre_seed_evidence_budget_invalid")
        budget_signatures = {_budget_signature(value) for value in budgets.values()}
        if len(budget_signatures) > 1:
            holds.append("pre_seed_evidence_asymmetry")
    raw_floors = charter.get("evidence_floor")
    floors = raw_floors if isinstance(raw_floors, dict) else {}
    if not floors:
        holds.append("pre_seed_evidence_floor_empty")
    if floors:
        dimensions_value = floors.get("dimensions") or floors.get("source_classes") or []
        dimensions = dimensions_value if isinstance(dimensions_value, list) else []
        minimum_sources = floors.get("minimum_sources_per_dimension", 1)
        if not isinstance(minimum_sources, int) or isinstance(minimum_sources, bool) or minimum_sources < 1:
            holds.append("pre_seed_evidence_floor_invalid")
            minimum_sources = 1
        if not dimensions or any(not isinstance(dim, str) or _is_placeholder(dim) for dim in dimensions):
            holds.append("pre_seed_evidence_floor_placeholder")
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            raw_evidence = candidate.get("evidence")
            evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
            missing = []
            for dim in dimensions:
                dim_name = str(dim)
                refs = _valid_evidence_refs(evidence.get(dim_name))
                if len(refs) < minimum_sources:
                    missing.append(dim_name)
            if missing:
                holds.append(f"pre_seed_candidate_evidence_gap {candidate.get('id') or candidate.get('name')}:{','.join(missing)}")
    raw_required_gates = charter.get("hard_eligibility_gates")
    required_gates, required_gates_valid = _normalize_required_gates(raw_required_gates)
    if raw_required_gates and not required_gates_valid:
        holds.append("pre_seed_hard_eligibility_gates_invalid")
    for field in ("hard_eligibility_gates",):
        if not isinstance(charter.get(field), list) or not charter.get(field):
            holds.append(f"pre_seed_{field}_empty")
    holds.extend(_validate_source_classes(charter.get("source_classes")))
    holds.extend(_validate_rubric(charter.get("rubric")))
    holds.extend(_validate_duration_entries(charter.get("duration_provenance")))
    raw_states = charter.get("decision_states")
    states = raw_states if isinstance(raw_states, dict) else {}
    if set(states) < REQUIRED_DECISION_STATES:
        holds.append("pre_seed_decision_state_collapse")
    eligible_set = states.get("eligible_set")
    selected_state_ids: list[str] = []
    if not isinstance(eligible_set, list) or not eligible_set:
        holds.append("pre_seed_decision_states_eligible_set_invalid")
    else:
        for value in eligible_set:
            if not _valid_scalar_string(value):
                holds.append("pre_seed_decision_states_eligible_set_invalid")
                break
            selected_state_ids.append(value.strip())
    for field in ("benchmark_winner", "mission_selection", "pilot_selection"):
        value = states.get(field)
        if value is None:
            continue
        if not _valid_scalar_string(value):
            holds.append(f"pre_seed_decision_states_{field}_invalid")
            continue
        selected_state_ids.append(value.strip())
    activation = states.get("activation_status")
    if not isinstance(activation, str) or activation.strip() not in VALID_ACTIVATION_STATUSES:
        holds.append("pre_seed_decision_states_activation_status_invalid")
    unknown_state_ids = sorted({candidate_id for candidate_id in selected_state_ids if candidate_id not in candidate_ids})
    if unknown_state_ids:
        holds.append("pre_seed_decision_states_unknown_id " + ",".join(unknown_state_ids))
    if states.get("benchmark_winner") and states.get("benchmark_winner") == states.get("mission_selection") == states.get("pilot_selection"):
        holds.append("pre_seed_decision_state_collapse")
    raw_winner = charter.get("winner")
    if raw_winner is not None and not _valid_scalar_string(raw_winner):
        holds.append("pre_seed_winner_invalid")
    winner = raw_winner if raw_winner is not None else states.get("benchmark_winner")
    if winner is not None and _valid_scalar_string(winner):
        by_id = {str(c.get("id") or c.get("name")): c for c in candidates if isinstance(c, dict)}
        wc = by_id.get(str(winner))
        if wc is None:
            holds.append("pre_seed_winner_not_in_candidate_universe")
        else:
            raw_gate_results = wc.get("hard_gate_results")
            gate_results = raw_gate_results if isinstance(raw_gate_results, dict) else {}
            missing_gate_results = [str(gate) for gate in required_gates if gate not in gate_results]
            if missing_gate_results:
                holds.append("pre_seed_winner_gate_results_missing " + ",".join(missing_gate_results))
            winner_gate_values = [gate_results.get(str(gate)) for gate in required_gates if str(gate) in gate_results]
            if any(not _is_explicit_pass(value) for value in winner_gate_values):
                holds.append("pre_seed_hard_gate_violating_winner")
            extra_gate_values = [value for gate, value in gate_results.items() if str(gate) not in {str(g) for g in required_gates}]
            if any(not _is_explicit_pass(value) for value in extra_gate_values):
                holds.append("pre_seed_hard_gate_violating_winner")
    activation = states.get("activation_status")
    if states.get("pilot_selection") and str(activation).lower() not in VALID_ACTIVATION_STATUSES:
        holds.append("pre_seed_pilot_selection_implies_activation")
    first_pass_status = "PASS" if not holds else "HOLD"
    for field, hold_name in (("status", "pre_seed_manifest_status_mismatch"), ("gate_status", "pre_seed_manifest_gate_status_mismatch")):
        declared = pre.get(field)
        if declared is not None and str(declared).strip().upper() != first_pass_status:
            holds.append(f"{hold_name} declared={declared} computed={first_pass_status}")
    computed_status = "PASS" if not holds else "HOLD"
    if pre.get("comparison_lanes_authorized") is True and holds:
        holds.append("pre_seed_gate_open_with_holds")
    if pre.get("target_specific_lanes_authorized") is True and holds:
        holds.append("pre_seed_target_specific_gate_open_with_holds")
    if manifest.get("comparison_lanes_authorized") is True and holds:
        holds.append("pre_seed_top_level_comparison_lanes_authorized_with_holds")
    if manifest.get("target_specific_lanes_authorized") is True and holds:
        holds.append("pre_seed_top_level_target_specific_lanes_authorized_with_holds")
    status = "PASS" if not holds else "HOLD"
    metrics = {
        "pre_seed_present": True,
        "pre_seed_status": status,
        "pre_seed_artifact_count": len(loaded),
        "pre_seed_candidate_count": len(candidates),
        "pre_seed_candidate_classes": sorted(classes),
        "pre_seed_permutation_digest": permutation_invariant_digest(universe, charter) if universe and charter else None,
    }
    return PreseedVerdict(status, holds, metrics)
