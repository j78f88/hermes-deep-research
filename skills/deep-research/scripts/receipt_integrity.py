#!/usr/bin/env python3
"""Receipt integrity checks for deep-research review records.

A review record is a JSON file, so any writer can author one by hand. These checks decide whether a
record could have come from the review script at all, and (online) whether the model calls it names
really happened at the provider.

Two layers:

* ``static_check`` (offline, deterministic): FAIL ``receipt_fabricated`` for placeholder model names
  (``mock``, ``fake``, ``dummy``...), hash fields that are not SHA-256 hex, or a PASS record that binds
  no file hash. The verifier and the exporter both run it.
* ``verify_provenance`` (online, used by completion and export): every model call the record lists
  must exist at OpenRouter under the same model, and the editor call must have read at least the
  size of the brief plus the pack. An unknown generation id is FAIL ``receipt_fabricated``; a record
  with no call list, or a provider that cannot be reached, is HOLD ``receipt_unverified``.

No secret is read unless ``verify_provenance`` is called with ``online=True``.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

FAKE_MARKER = re.compile(r"\b(?:mock|fake|dummy|placeholder|stub|todo|tbd|example|lorem)\b|mock-hash|xxx+", re.I)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEXISH = re.compile(r"^[0-9a-f]{6,64}$")
MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*$", re.I)
GENERATION_ID = re.compile(r"^gen-[A-Za-z0-9_-]{8,}$")
MODEL_FIELDS = ("writer_model", "editor_model", "reviewer", "reviewer_model", "model", "editor", "writer")
GENERATION_URL = "https://openrouter.ai/api/v1/generation?id="
# The editor prompt carries the whole brief and pack. Characters per token is about 4 for English;
# demand at least a third of that so tokenizer differences never fail a real call.
MIN_PROMPT_TOKEN_SHARE = 1 / 12
# OpenRouter can take several minutes to publish a generation record. A missing
# record for a call made within this window is unverified (HOLD), never PASS.
# Older, future-dated or undated ids that are missing stay receipt_fabricated.
PUBLICATION_LAG_SECONDS = 30 * 60
# Modern receipts retain shared-client roles and bind every provider generation to
# its own served identity. Absence of this marker keeps the original ISSUE-763 rules.
SERVED_CALLS_SCHEMA = 'deep-research-served-calls-v1'


def _author_attempts_match(authors: Any, attempts: Any) -> bool:
    """Every in-run writer attempt must retain its exact JSON author record."""
    if any(not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
           for rows in (authors, attempts)):
        return False
    saved = {json.dumps(author, sort_keys=True) for author in authors}
    return all(json.dumps(call, sort_keys=True) in saved for call in attempts
               if call.get('role') in ('writer', 'rewriting_editor'))


def _served_call_contract(record: dict, provenance: dict) -> list[str]:
    """Reject dropped attempts, role laundering and ambiguous final generations."""
    calls, attempts, authors = (provenance.get('calls'), provenance.get('attempts'),
                                record.get('artifact_writers'))
    if (not isinstance(calls, list) or not isinstance(attempts, list) or not isinstance(authors, list)
            or any(not isinstance(row, dict) for rows in (calls, attempts, authors) for row in rows)):
        return ['served-call contract requires calls, attempts and artifact_writers arrays']
    if not _author_attempts_match(authors, attempts):
        return ['artifact_writers must include every writer/rewriting_editor attempt with exact JSON identity']
    attempt_json = {json.dumps(call, sort_keys=True) for call in attempts}
    expected = [author for author in authors if json.dumps(author, sort_keys=True) not in attempt_json] + attempts
    expected = [call for call in expected if call.get('generation_id')]
    if json.dumps(calls, sort_keys=True) != json.dumps(expected, sort_keys=True):
        return ['served-call list differs from actual attempts and supplied artifact writers']
    ids = [call.get('generation_id') for call in calls]
    if len(set(ids)) != len(ids):
        return ['served-call generation ids must be unique']
    for call in calls:
        if (call.get('role') not in ('writer', 'rewriting_editor', 'brief_reviewer', 'reviewer')
                or not isinstance(call.get('served_model'), str)
                or not MODEL_ID.fullmatch(call['served_model'])):
            return ['served-call role or served_model is invalid']
    editors = [call for call in attempts if call.get('role') == 'brief_reviewer' and call.get('status') == 'success']
    writers = [call for call in authors if call.get('status') == 'success']
    if (not editors or not provenance.get('final_editor_generation_id')
            or provenance['final_editor_generation_id'] != editors[-1].get('generation_id')):
        return ['final editor generation must name the latest successful brief_reviewer attempt']
    final = editors[-1]
    if final.get('identity_verified') is not True or final.get('served_model') != record.get('editor_model'):
        return ['final editor identity is unverified or differs from declared editor_model']
    if not writers or writers[-1].get('served_model') != record.get('writer_model'):
        return ['final writer differs from declared writer_model or has no saved author call']
    from model_client import writer_provenance
    from research_settings import load_settings
    settings = load_settings(environ={})
    developers, known = writer_provenance(settings, authors)
    editor_developer = settings.developer(record.get('editor_model'))
    if not known or not editor_developer or editor_developer in developers:
        return ['served author identity is unknown or final editor is not independent of every contributing writer']
    # Classification judgements use the existing shared-client reviewer role.
    # They may share the comments editor's developer, but never an actual writer's.
    for call in attempts:
        if call.get('role') == 'reviewer' and call.get('status') == 'success':
            developer = settings.developer(call.get('served_model'))
            if (call.get('identity_verified') is not True or not developer or developer in developers
                    or not isinstance(call.get('generation_id'), str)
                    or not GENERATION_ID.fullmatch(call['generation_id'])):
                return ['classification reviewer identity is unknown or not independent of every contributing writer']
    return []


def _recent_generation(generation_id: str, now: float) -> bool:
    match = re.fullmatch(r"gen-(\d{9,11})-[A-Za-z0-9]+", generation_id or "")
    if not match:
        return False
    age = now - int(match.group(1))
    return 0 <= age < PUBLICATION_LAG_SECONDS


def _hash_fields(record: dict) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key.endswith("sha256") or key.endswith("_hash"):
            out[key] = value
    hashes = record.get("hashes")
    if isinstance(hashes, dict):
        for key, value in hashes.items():
            out[f"hashes.{key}"] = value
    return out


def static_check(record: Any, *, require_hash_binding: bool = True, require_model_ids: bool = False) -> dict:
    """Offline fabrication check for one review or QA record.

    Returns ``{"verdict": PASS|FAIL, "class": ok|receipt_fabricated, "reasons": [...]}``.
    """
    if not isinstance(record, dict):
        return {"verdict": "FAIL", "class": "receipt_fabricated", "reasons": ["record is not a JSON object"]}
    reasons: list[str] = []
    for field in MODEL_FIELDS:
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            if FAKE_MARKER.search(value):
                reasons.append(f"{field} is a placeholder: {value!r}")
            elif require_model_ids and field in ("writer_model", "editor_model") and not MODEL_ID.match(value.strip()):
                reasons.append(f"{field} is not a provider model id: {value!r}")
    hashes = _hash_fields(record)
    for key, value in hashes.items():
        if value in (None, ""):
            continue
        # *sha256 fields must be SHA-256; other hash fields (git commits, short ids) must at least be hex.
        pattern = HEX64 if key.endswith("sha256") else HEXISH
        if not isinstance(value, str) or not pattern.match(value) or FAKE_MARKER.search(value):
            reasons.append(f"{key} is not a {'SHA-256' if pattern is HEX64 else 'hex'} hash: {value!r}")
    passed = str(record.get("status") or record.get("verdict") or "").upper() in {"PASS", "PASSED"}
    if passed and require_hash_binding and not any(isinstance(v, str) and HEX64.match(v) for v in hashes.values()):
        reasons.append("PASS record binds no file hash")
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    for call in provenance.get("calls") or []:
        gid = str((call or {}).get("generation_id") or "") if isinstance(call, dict) else ""
        if gid and not GENERATION_ID.match(gid):
            reasons.append(f"generation id is not a provider id: {gid!r}")
    if reasons:
        return {"verdict": "FAIL", "class": "receipt_fabricated", "reasons": reasons}
    return {"verdict": "PASS", "class": "ok", "reasons": []}


def _api_key() -> str:
    env_key = os.environ.get("OPENROUTER_API_KEY")
    if env_key:
        return env_key
    with open(os.path.expanduser("~/.hermes/.env")) as fh:
        for line in fh:
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("OPENROUTER_API_KEY not found")


def fetch_generation(generation_id: str, *, attempts: int = 6, wait: float = 5.0) -> dict | None:
    """Provider record for one generation id; None when the provider says it does not exist."""
    request = urllib.request.Request(GENERATION_URL + generation_id, headers={"Authorization": "Bearer " + _api_key()})
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response).get("data") or {}
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                last = exc
            elif attempt == attempts - 1:
                return None
        except Exception as exc:  # noqa: BLE001 - network trouble is reported as unverified, never PASS
            last = exc
        time.sleep(wait)
    raise RuntimeError(f"provider unreachable: {type(last).__name__}: {last}")


MODEL_CATALOGUE_URL = 'https://openrouter.ai/api/v1/models'


def fetch_model_catalogue() -> list[dict]:
    """Fresh public official HTTPS read; saved routing/receipt catalogues are not authentication.

    urllib's default TLS verification authenticates the fixed host. Do not accept
    redirected content or send credentials. No process-global catalogue cache.
    """
    request = urllib.request.Request(MODEL_CATALOGUE_URL, headers={'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=60) as response:
        if response.geturl() != MODEL_CATALOGUE_URL:
            raise ValueError('official model catalogue redirected')
        data = json.load(response).get('data')
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
        raise ValueError('invalid official model catalogue')
    return data


class CatalogueUnavailable(RuntimeError):
    """A fresh catalogue lookup could not authenticate a non-exact identity."""


class ModelIdentityVerifier:
    """One verification's lazy catalogue; only an unambiguous exact id/slug pair matches."""

    def __init__(self):
        self._catalogue: list[dict] | None = None
        self._unavailable = False

    def matches(self, declared: Any, served: Any) -> bool:
        if (not isinstance(declared, str) or not isinstance(served, str)
                or not MODEL_ID.fullmatch(declared) or not MODEL_ID.fullmatch(served)):
            return False
        if declared == served:
            return True
        # A different developer cannot become equivalent through catalogue data.
        if declared.split('/', 1)[0] != served.split('/', 1)[0]:
            return False
        if self._catalogue is None and not self._unavailable:
            try:
                self._catalogue = fetch_model_catalogue()
            except Exception:
                self._unavailable = True
        if self._unavailable:
            raise CatalogueUnavailable('fresh official model catalogue unavailable')
        identities = {declared, served}
        mappings: dict[str, set[str]] = {}
        for row in self._catalogue or []:
            pair = (row.get('id'), row.get('canonical_slug'))
            if declared not in pair and served not in pair:
                continue
            identifier, canonical = pair
            if (not isinstance(identifier, str) or not isinstance(canonical, str)
                    or not MODEL_ID.fullmatch(identifier) or not MODEL_ID.fullmatch(canonical)):
                return False
            if identifier in identities:
                mappings.setdefault(identifier, set()).add(canonical)
        # Several official aliases may share a canonical slug. Each endpoint
        # identity must still have one consistent mapping, with a direct pair.
        if any(len(values) != 1 for values in mappings.values()):
            return False
        for alias, canonical in ((declared, served), (served, declared)):
            if (mappings.get(alias) == {canonical}
                    and mappings.get(canonical, {canonical}) == {canonical}):
                return True
        return False


def _reader_body(text: str) -> str:
    """Brief text without YAML frontmatter or the 'Detail and sources' footer, whitespace collapsed."""
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    body = text[m.end():] if m else text
    return " ".join(body.split("\n---\nDetail and")[0].split())


def _corrected_draft_matches(record: dict, root: Path) -> bool:
    """True only when the record names a corrected draft inside the campaign root whose body is the brief's body."""
    if record.get("writer_skipped") != "corrected_draft" or not record.get("corrected_draft_path") or not record.get("brief_path"):
        return False
    base = root.resolve()
    draft = Path(str(record["corrected_draft_path"]))
    draft = (draft if draft.is_absolute() else base / draft).resolve()
    brief = (base / str(record["brief_path"])).resolve()
    if base not in draft.parents or base not in brief.parents or not draft.is_file() or not brief.is_file():
        return False
    return _reader_body(draft.read_text(encoding="utf-8", errors="ignore")) == _reader_body(brief.read_text(encoding="utf-8", errors="ignore"))


def verify_provenance(record: dict, root: Path, *, online: bool = True,
                      fetch: Callable[[str], dict | None] | None = None, now: float | None = None) -> dict:
    """Check that the calls a plain-brief review names really happened.

    Returns ``{"verdict": PASS|HOLD|FAIL, "class": ok|receipt_unverified|receipt_fabricated, "reasons", "calls"}``.
    """
    static = static_check(record, require_model_ids=True)
    if static["verdict"] == "FAIL":
        return {**static, "calls": []}
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    calls = [c for c in provenance.get("calls") or [] if isinstance(c, dict)]
    if not calls:
        return {"verdict": "HOLD", "class": "receipt_unverified", "calls": [],
                "reasons": ["record lists no provider calls (written before ISSUE-763 or by hand); rerun plain_brief_rewrite.py --campaign-root"]}
    modern = provenance.get('schema_version') == SERVED_CALLS_SCHEMA
    legacy = False
    if not modern:
        from report_purpose import resolve_report_purpose
        try:
            manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
            legacy = resolve_report_purpose(manifest, root) == 'legacy'
        except (OSError, ValueError, TypeError):
            pass
        if not legacy:
            return {'verdict': 'HOLD', 'class': 'receipt_unverified', 'calls': [],
                    'reasons': ['current campaign requires served-call identity evidence; legacy provenance is restricted to the frozen historical inventory']}
    if 'schema_version' in provenance:
        contract_errors = (_served_call_contract(record, provenance) if modern
                           else ['unsupported provenance schema_version'])
        if contract_errors:
            return {'verdict': 'FAIL', 'class': 'receipt_fabricated', 'calls': [], 'reasons': contract_errors}
        if (record.get('acceptance_eligible') is not True or record.get('status') != 'PASS'
                or record.get('editor_verdict') != 'PASS'):
            return {'verdict': 'HOLD', 'class': 'receipt_unverified', 'calls': [],
                    'reasons': ['final editor review was not accepted']}
    roles = {str(c.get("role")) for c in calls}
    if modern:
        missing_roles = set()
        if not roles.intersection({'writer', 'rewriting_editor'}):
            missing_roles.add('writer')
        if 'brief_reviewer' not in roles:
            missing_roles.add('brief_reviewer')
    else:
        missing_roles = {"writer", "editor"} - roles
    if legacy and missing_roles == {"writer"} and _corrected_draft_matches(record, root):
        missing_roles = set()  # a corrected draft is checked, not rewritten, so no writer call is made
    if missing_roles:
        return {"verdict": "FAIL", "class": "receipt_fabricated", "calls": [],
                "reasons": [f"no {', '.join(sorted(missing_roles))} call recorded"]}
    if not online:
        return {"verdict": "HOLD", "class": "receipt_unverified", "calls": [], "reasons": ["provider check skipped (offline)"]}
    fetch = fetch or fetch_generation
    brief_chars = full_chars = 0
    for key, rel_key in (("brief", "brief_path"), ("full", "full_pack_path")):
        rel = record.get(rel_key)
        path = (root / rel) if rel else None
        size = len(path.read_text(encoding="utf-8", errors="ignore")) if path and path.is_file() else 0
        if key == "brief":
            brief_chars = size
        else:
            full_chars = size
    min_editor_tokens = int((brief_chars + full_chars) * MIN_PROMPT_TOKEN_SHARE)
    reasons, checked, fabricated, unpublished = [], [], False, []
    identities = ModelIdentityVerifier()
    now = time.time() if now is None else now
    declared = {"writer": record.get("writer_model"), "editor": record.get("editor_model")}
    editor_ok = False
    for call in calls:
        gid, role = str(call.get("generation_id") or ""), str(call.get("role"))
        try:
            data = fetch(gid)
        except Exception as exc:  # noqa: BLE001
            return {"verdict": "HOLD", "class": "receipt_unverified", "calls": checked, "reasons": [f"provider check failed: {exc}"]}
        if data is None:
            if _recent_generation(gid, now):
                unpublished.append(f"{role} generation {gid} is not yet published at the provider; rerun the completion check later")
                continue
            fabricated = True
            reasons.append(f"{role} generation {gid} does not exist at the provider")
            continue
        served = str(data.get("model") or "")
        row = {"role": role, "generation_id": gid, "served_model": served, "created_at": data.get("created_at"),
               "tokens_prompt": data.get("tokens_prompt") or data.get("native_tokens_prompt")}
        checked.append(row)
        expected_model = call.get('served_model') if modern else declared.get(role)
        try:
            model_matches = identities.matches(expected_model, served)
        except CatalogueUnavailable:
            unpublished.append(f'{role} call {gid}: fresh official model catalogue unavailable')
            model_matches = None
        if expected_model and model_matches is False:
            fabricated = True
            reasons.append(f"{role} call {gid} was served by {served}, not the declared {expected_model}")
        budget_editor = (gid == provenance['final_editor_generation_id'] and role == 'brief_reviewer') if modern else role == 'editor'
        if budget_editor and int(row["tokens_prompt"] or 0) >= min_editor_tokens:
            editor_ok = True
    if fabricated:
        return {"verdict": "FAIL", "class": "receipt_fabricated", "calls": checked, "reasons": reasons + unpublished}
    if unpublished:
        return {"verdict": "HOLD", "class": "receipt_unverified", "calls": checked, "reasons": unpublished}
    if not editor_ok:
        fabricated = True
        label = 'final editor' if modern else 'editor'
        reasons.append(f"no {label} call read the brief and pack (needs at least {min_editor_tokens} prompt tokens)")
    if fabricated:
        return {"verdict": "FAIL", "class": "receipt_fabricated", "calls": checked, "reasons": reasons}
    return {"verdict": "PASS", "class": "ok", "calls": checked, "reasons": []}


def verify_campaign_provenance(root: Path, manifest: dict, *, online: bool = True,
                               fetch: Callable[[str], dict | None] | None = None) -> dict:
    """Shared completion/export boundary, including the single frozen legacy exception."""
    from report_purpose import resolve_report_purpose
    try:
        purpose = resolve_report_purpose(manifest, root)
        raw_pb = manifest.get('plain_brief')
        pb = raw_pb if isinstance(raw_pb, dict) else {}
        if not pb or pb.get('required', True) is False:
            if purpose == 'legacy' and manifest.get('output_layout') != 'brief-and-reference-v2':
                return {'verdict': 'PASS', 'class': 'ok', 'reasons': [], 'calls': []}
            raise ValueError('only frozen legacy campaigns may omit the required plain brief')
        rel = pb.get('review') or 'qa/plain-brief-review.json'
        if not isinstance(rel, str) or '\\' in rel:
            raise ValueError('invalid plain brief review path')
        path = Path(rel)
        if path.is_absolute() or '..' in path.parts:
            raise ValueError('plain brief review path escapes campaign root')
        current = root
        for part in path.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('symlinked plain brief review path')
        if not current.is_file():
            return {'verdict': 'HOLD', 'class': 'receipt_missing', 'reasons': [f'{rel} missing'], 'calls': []}
        record = json.loads(current.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError) as exc:
        return {'verdict': 'HOLD', 'class': 'receipt_unverified', 'reasons': [str(exc)], 'calls': []}
    return verify_provenance(record, root, online=online, fetch=fetch)


def sweep_qa_dir(root: Path) -> list[str]:
    """Fabrication findings across every JSON record under qa/ (the plain-brief round logs excluded)."""
    findings = []
    qa = root / "qa"
    if not qa.is_dir():
        return findings
    for path in sorted(qa.glob("*.json")):
        if path.is_symlink():
            findings.append(f"{path.relative_to(root)}: symlinked record")
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - malformed records are the verifier's HOLD, not fabrication
            continue
        result = static_check(record, require_hash_binding=path.name == "plain-brief-review.json")
        if result["verdict"] == "FAIL":
            findings.append(f"{path.relative_to(root)}: " + "; ".join(result["reasons"]))
    return findings


__all__ = ["static_check", "verify_provenance", "verify_campaign_provenance", "fetch_generation", "sweep_qa_dir", "FAKE_MARKER", "HEX64"]
