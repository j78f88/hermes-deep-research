#!/usr/bin/env python3
"""Durable file-backed deep-research campaign controller state.

Small helper for restart-safe lane dispatch/fan-in. It intentionally exposes only
completion-driven event ingestion; there is no polling primitive.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import contextlib
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "deep-research-campaign-controller-v1"
STATE_FILE = "campaign_state.json"
LOCK_FILE = "campaign_state.lock"
RESTART_PACKET = "receipts/restart-packet.json"
# plain_brief runs after QA has accepted the final internal full pack and before export: the plain
# brief is what the owner reads first, and export_deliverables.py refuses to copy it until it passes.
PHASE_ORDER = ["intake", "lanes", "external_freeze", "internal_synthesis", "qa", "plain_brief", "export_closeout"]
# Layout v2 has no external pack, so no external_freeze phase. Legacy campaigns keep PHASE_ORDER.
OUTPUT_LAYOUT_V2 = "brief-and-reference-v2"
PHASE_ORDER_V2 = ["intake", "lanes", "internal_synthesis", "qa", "plain_brief", "export_closeout"]
TERMINAL_STATUSES = {"completed", "terminal"}
SELECTABLE_STATUSES = {"pending", "failed", "stale", "invalid"}
VALID_ATTEMPT_TRANSITIONS = {
    "running": {"completed", "failed", "invalid", "stale"},
    "stale": set(),
    "completed": set(),
    "failed": set(),
    "invalid": set(),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sha256_path(path: Path) -> str | None:
    if not path.is_file() or path.is_symlink():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _state_path(root: Path | str) -> Path:
    return Path(root) / STATE_FILE


def _lock_path(root: Path | str) -> Path:
    return Path(root) / LOCK_FILE


def _restart_path(root: Path | str) -> Path:
    return Path(root) / RESTART_PACKET


@contextlib.contextmanager
def _file_lock(root: Path | str):
    """Serialize state writers with a small advisory file lock."""
    lock_path = _lock_path(root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        if os.name == "posix":
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - current runtime is Linux; keep portable fallback.
            yield


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
        if os.name == "posix":
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def load_state(root: Path | str) -> dict[str, Any]:
    path = _state_path(root)
    if not path.is_file() or path.is_symlink():
        raise FileNotFoundError(f"campaign controller state missing: {path}")
    return load_json(path)


def save_state(root: Path | str, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(_state_path(root), state)


def is_v2_manifest(manifest: dict[str, Any] | None) -> bool:
    return isinstance(manifest, dict) and manifest.get("output_layout") == OUTPUT_LAYOUT_V2


def phase_order_for(manifest: dict[str, Any] | None) -> list[str]:
    return list(PHASE_ORDER_V2 if is_v2_manifest(manifest) else PHASE_ORDER)


def _state_phase_order(state: dict[str, Any]) -> list[str]:
    order = state.get("phase_order")
    return list(order) if isinstance(order, list) and order else list(PHASE_ORDER)


def _campaign_inputs(manifest: dict[str, Any]) -> dict[str, Any]:
    inputs: dict[str, Any] = {
        "target": manifest.get("target"),
        "target_type": manifest.get("target_type"),
        "primary_source": manifest.get("primary_source"),
        "decision_question": manifest.get("decision_question"),
        "external_output": manifest.get("external_output", {}).get("path") if isinstance(manifest.get("external_output"), dict) else None,
        "internal_outputs": manifest.get("internal_outputs", []),
    }
    if is_v2_manifest(manifest):
        inputs["output_layout"] = OUTPUT_LAYOUT_V2
    return inputs


def _lane_identity(lane: dict[str, Any]) -> dict[str, Any]:
    return {key: lane.get(key) for key in ("order", "id", "name", "type")}


def _lane_input(campaign_inputs: dict[str, Any], lane: dict[str, Any]) -> dict[str, Any]:
    return {"campaign": campaign_inputs, "lane": _lane_identity(lane)}


def _default_metrics() -> dict[str, Any]:
    return {
        "active_time_seconds": 0,
        "idle_time_seconds": 0,
        "active_events": 0,
        "idle_events": 0,
        "retries": 0,
        "fallback_attempts": 0,
        "workers_total": 0,
        "reused_lanes": 0,
        "redone_lanes": 0,
        "blocked_lanes": 0,
        "stale_lanes": 0,
        "invalidated_lanes": 0,
        "completion_events": 0,
        "duplicate_events": 0,
        "status_poll_loop_api_calls": 0,
    }


def _export_closeout(requested: bool) -> dict[str, Any]:
    proofs = ["local_copy", "local_commit", "remote_push", "remote_readback"] if requested else []
    return {
        "requested": requested,
        "local_copy": False,
        "local_commit": False,
        "remote_push": False,
        "remote_readback": False,
        "complete": not requested,
        "terminal_reason": "not_requested" if not requested else "missing_proofs:" + ",".join(proofs),
    }


def _default_phases(order: list[str] | None = None) -> dict[str, Any]:
    return {
        phase: {"status": "pending", "started_at": None, "completed_at": None, "terminal_reason": None}
        for phase in (order or PHASE_ORDER)
    }


def initialize_campaign_state(root: Path | str, manifest: dict[str, Any]) -> dict[str, Any]:
    """Create or update controller state from a scaffold manifest.

    Existing lane runtime state is preserved by lane id. Fingerprints are stable
    across repeated scaffolds with the same inputs.
    """
    root = Path(root)
    with _file_lock(root):
        existing_by_id: dict[str, Any] = {}
        if _state_path(root).is_file() and not _state_path(root).is_symlink():
            try:
                existing = load_state(root)
                existing_by_id = {lane["id"]: lane for lane in existing.get("lanes", []) if isinstance(lane, dict) and lane.get("id")}
            except Exception:
                existing = {}
        else:
            existing = {}
        created_at = existing.get("created_at") or now_iso()
        campaign_inputs = _campaign_inputs(manifest)
        lanes = []
        for lane in manifest.get("lanes", []):
            identity = _lane_identity(lane)
            prior = existing_by_id.get(lane.get("id"), {})
            input_snapshot = _lane_input(campaign_inputs, lane)
            input_fingerprint = stable_hash(input_snapshot)
            input_changed = bool(prior) and prior.get("input_fingerprint") != input_fingerprint
            timestamps = dict(prior.get("timestamps") or {})
            timestamps.setdefault("created_at", created_at)
            timestamps.setdefault("updated_at", created_at)
            lanes.append({
                "id": lane.get("id"),
                "name": lane.get("name"),
                "type": lane.get("type"),
                "order": lane.get("order"),
                "lane_fingerprint": stable_hash(identity),
                "input_fingerprint": input_fingerprint,
                "input_snapshot": input_snapshot,
                "status": "stale" if input_changed else prior.get("status", "pending"),
                "attempts": prior.get("attempts", []),
                "output_path": prior.get("output_path"),
                "output_hash": prior.get("output_hash"),
                "dependencies": prior.get("dependencies", []),
                "terminal_reason": "input_fingerprint_changed" if input_changed else prior.get("terminal_reason"),
                "invalidated_by": ["input_fingerprint"] if input_changed else prior.get("invalidated_by"),
                "timestamps": timestamps,
            })
        order = phase_order_for(manifest)
        phases = existing.get("phases") or _default_phases(order)
        phases.setdefault("intake", {"status": "pending", "started_at": None, "completed_at": None, "terminal_reason": None})
        if phases["intake"].get("status") != "complete":
            phases["intake"].update({"status": "complete", "started_at": phases["intake"].get("started_at") or created_at, "completed_at": now_iso(), "terminal_reason": None})
        for phase, default in _default_phases(order).items():
            phases.setdefault(phase, default)
        metrics = _default_metrics()
        metrics.update(existing.get("metrics") or {})
        requested = bool((manifest.get("deliverable_exports") or {}).get("enabled")) if isinstance(manifest.get("deliverable_exports"), dict) else False
        state = {
            "schema_version": SCHEMA_VERSION,
            "created_at": created_at,
            "updated_at": now_iso(),
            "campaign_root": str(root),
            "phase_order": order,
            "phases": phases,
            "campaign_fingerprint": stable_hash(campaign_inputs),
            "lanes": lanes,
            "metrics": metrics,
            "events": existing.get("events", []),
            "timing_events": existing.get("timing_events", []),
            "repair_cycles": existing.get("repair_cycles", {}),
            "no_poll_loop_api": True,
            "export_closeout": existing.get("export_closeout") or _export_closeout(requested),
        }
        save_state(root, state)
        return state


def _lane(state: dict[str, Any], lane_id: str) -> dict[str, Any]:
    for lane in state.get("lanes", []):
        if lane.get("id") == lane_id:
            return lane
    raise KeyError(f"unknown lane: {lane_id}")


def _running_attempt(lane: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in reversed(lane.get("attempts", [])):
        if attempt.get("status") == "running":
            return attempt
    return None


def _attempt_counts(lane: dict[str, Any]) -> tuple[int, int, int]:
    attempts = lane.get("attempts", [])
    same_route_failures = sum(1 for a in attempts if not a.get("fallback") and a.get("status") == "failed")
    fallback_attempts = sum(1 for a in attempts if a.get("fallback"))
    failures = sum(1 for a in attempts if a.get("status") == "failed")
    return same_route_failures, fallback_attempts, failures


def _mark_terminal_if_retry_ceiling(lane: dict[str, Any]) -> bool:
    _, fallback_attempts, failures = _attempt_counts(lane)
    if fallback_attempts >= 1 and failures >= 3:
        lane["status"] = "terminal"
        lane["terminal_reason"] = "retry_ceiling_exhausted"
        lane["timestamps"]["updated_at"] = now_iso()
        return True
    return False


def claim_lane(root: Path | str, lane_id: str, *, route: str, worker_id: str, duplicate_policy: str = "reject", fallback: bool = False, lease_seconds: int | None = None) -> dict[str, Any]:
    with _file_lock(root):
        state = load_state(root)
        lane = _lane(state, lane_id)
        _mark_terminal_if_retry_ceiling(lane)
        running = _running_attempt(lane)
        if running:
            if duplicate_policy == "join":
                return {"status": "joined", "lane_id": lane_id, "attempt_id": running["attempt_id"], "worker_id": running.get("worker_id")}
            return {"status": "rejected", "lane_id": lane_id, "attempt_id": running["attempt_id"], "reason": "lane_already_claimed"}
        if lane.get("status") == "completed" and _lane_output_valid(Path(root), lane):
            return {"status": "rejected", "lane_id": lane_id, "reason": "lane_already_completed"}
        if lane.get("status") == "terminal":
            _refresh_lane_phase(state)
            save_state(root, state)
            return {"status": "rejected", "lane_id": lane_id, "reason": lane.get("terminal_reason") or "lane_terminal"}
        same_route_failures, fallback_attempts, _ = _attempt_counts(lane)
        is_fallback = fallback or route == "fallback"
        if is_fallback:
            if fallback_attempts >= 1:
                lane["status"] = "terminal"
                lane["terminal_reason"] = "retry_ceiling_exhausted"
                lane["timestamps"]["updated_at"] = now_iso()
                _refresh_lane_phase(state)
                save_state(root, state)
                return {"status": "rejected", "lane_id": lane_id, "reason": "retry_ceiling_exhausted"}
            attempt_kind = "fallback"
            state["metrics"]["fallback_attempts"] = state["metrics"].get("fallback_attempts", 0) + 1
        else:
            if same_route_failures >= 2:
                return {"status": "rejected", "lane_id": lane_id, "reason": "same_route_retry_exhausted; use fallback"}
            attempt_kind = "initial" if not lane.get("attempts") else "same_route_retry"
            if attempt_kind == "same_route_retry":
                state["metrics"]["retries"] = state["metrics"].get("retries", 0) + 1
        attempt_id = f"{lane_id}-attempt-{len(lane.get('attempts', [])) + 1}"
        timestamp = now_iso()
        attempt = {
            "attempt_id": attempt_id,
            "route": route,
            "worker_id": worker_id,
            "attempt_kind": attempt_kind,
            "fallback": is_fallback,
            "status": "running",
            "claimed_at": timestamp,
            "lease_seconds": lease_seconds,
            "lease_expires_at": datetime.fromtimestamp(time.time() + lease_seconds, timezone.utc).isoformat() if lease_seconds else None,
            "completed_at": None,
            "terminal_reason": None,
        }
        lane.setdefault("attempts", []).append(attempt)
        lane["status"] = "running"
        lane["terminal_reason"] = None
        lane["timestamps"]["updated_at"] = timestamp
        state["metrics"]["workers_total"] = state["metrics"].get("workers_total", 0) + 1
        state["phases"]["lanes"]["status"] = "running"
        state["phases"]["lanes"]["started_at"] = state["phases"]["lanes"].get("started_at") or timestamp
        save_state(root, state)
        return {"status": "claimed", "lane_id": lane_id, "attempt_id": attempt_id, "attempt_kind": attempt_kind}


def _safe_relative_path(root: Path, rel: str, *, label: str = "path") -> Path:
    candidate = Path(rel)
    if candidate.is_absolute():
        raise ValueError(f"unsafe {label}: absolute paths are not allowed")
    if any(part == ".." for part in candidate.parts):
        raise ValueError(f"unsafe {label}: traversal is not allowed")
    path = root / candidate
    resolved_root = root.resolve()
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"unsafe {label}: escapes campaign root") from exc
    if path.is_symlink():
        raise ValueError(f"unsafe {label}: symlink not allowed")
    return path


def _lane_output_valid(root: Path, lane: dict[str, Any]) -> bool:
    rel = lane.get("output_path")
    digest = lane.get("output_hash")
    if not rel or not digest:
        return False
    try:
        path = _safe_relative_path(root, str(rel), label="output_path")
    except ValueError:
        return False
    return sha256_path(path) == digest


def _event_seen(state: dict[str, Any], event_id: str | None) -> bool:
    if not event_id:
        return False
    return any(item.get("event_id") == event_id for item in state.get("events", []))


def ingest_completion_event(root: Path | str, event: dict[str, Any]) -> dict[str, Any]:
    with _file_lock(root):
        state = load_state(root)
        event_id = event.get("event_id")
        if _event_seen(state, str(event_id) if event_id is not None else None):
            state["metrics"]["duplicate_events"] = state["metrics"].get("duplicate_events", 0) + 1
            save_state(root, state)
            return {"status": "duplicate_ignored", "lane_id": str(event.get("lane_id")), "event_id": event_id}
        lane = _lane(state, str(event["lane_id"]))
        attempt_id = str(event["attempt_id"])
        attempts = [a for a in lane.get("attempts", []) if a.get("attempt_id") == attempt_id]
        if not attempts:
            raise ValueError(f"unknown attempt for lane {lane['id']}: {attempt_id}")
        attempt = attempts[-1]
        status = str(event.get("status") or "").lower()
        if status not in {"completed", "failed", "invalid"}:
            raise ValueError(f"unsupported completion status: {status}")
        prior_status = attempt.get("status")
        if status not in VALID_ATTEMPT_TRANSITIONS.get(str(prior_status), set()):
            raise ValueError(f"invalid event transition: {prior_status} -> {status}")
        timestamp = now_iso()
        if status == "completed":
            output_path = str(event.get("output_path") or lane.get("output_path") or f"lanes/{lane['id']}.md")
            path = _safe_relative_path(Path(root), output_path, label="output_path")
            digest = sha256_path(path)
            if not digest:
                attempt["status"] = "invalid"
                attempt["completed_at"] = timestamp
                attempt["terminal_reason"] = "output_missing_or_unhashable"
                lane["status"] = "invalid"
                lane["terminal_reason"] = "output_missing_or_unhashable"
            else:
                attempt["status"] = status
                attempt["completed_at"] = timestamp
                attempt["terminal_reason"] = event.get("terminal_reason")
                lane["status"] = "completed"
                lane["output_path"] = output_path
                lane["output_hash"] = digest
                lane["terminal_reason"] = None
                lane["timestamps"]["completed_at"] = timestamp
        else:
            attempt["status"] = status
            attempt["completed_at"] = timestamp
            attempt["terminal_reason"] = event.get("terminal_reason")
            lane["status"] = status
            lane["terminal_reason"] = event.get("terminal_reason") or status
            _mark_terminal_if_retry_ceiling(lane)
        lane["timestamps"]["updated_at"] = timestamp
        state["metrics"]["completion_events"] = state["metrics"].get("completion_events", 0) + 1
        state.setdefault("events", []).append({"type": "completion", "event_id": event_id, "at": timestamp, "event": event})
        _refresh_lane_phase(state)
        save_state(root, state)
        return {"status": lane["status"], "lane_id": lane["id"], "output_hash": lane.get("output_hash")}


def _refresh_lane_phase(state: dict[str, Any]) -> None:
    lanes = state.get("lanes", [])
    if lanes and all(lane.get("status") == "completed" for lane in lanes):
        state["phases"]["lanes"].update({"status": "complete", "completed_at": now_iso(), "terminal_reason": None})
        if "external_freeze" in state["phases"] and "external_freeze" in _state_phase_order(state):
            state["phases"]["external_freeze"]["status"] = "pending"
    elif any(lane.get("status") == "running" for lane in lanes):
        state["phases"]["lanes"]["status"] = "running"
    elif any(lane.get("status") == "terminal" for lane in lanes):
        state["phases"]["lanes"].update({"status": "blocked", "terminal_reason": "lane_terminal"})
    else:
        state["phases"]["lanes"]["status"] = "pending"


def _mark_stale_running_lanes(state: dict[str, Any], *, stale_after_seconds: int | None = None) -> list[str]:
    stale: list[str] = []
    now = datetime.now(timezone.utc)
    for lane in state.get("lanes", []):
        running = _running_attempt(lane)
        if not running:
            continue
        expires = _parse_iso(running.get("lease_expires_at"))
        claimed = _parse_iso(running.get("claimed_at"))
        expired = expires is not None and expires <= now
        over_stale_after = stale_after_seconds is not None and claimed is not None and (now - claimed).total_seconds() >= stale_after_seconds
        if expired or over_stale_after:
            running["status"] = "stale"
            running["completed_at"] = now_iso()
            running["terminal_reason"] = "lease_expired"
            lane["status"] = "stale"
            lane["terminal_reason"] = "lease_expired"
            lane["timestamps"]["updated_at"] = running["completed_at"]
            stale.append(lane["id"])
    return stale


def resume_selection(root: Path | str, *, stale_after_seconds: int | None = None) -> dict[str, Any]:
    with _file_lock(root):
        state = load_state(root)
        newly_stale = _mark_stale_running_lanes(state, stale_after_seconds=stale_after_seconds)
        selected: list[str] = []
        reused: list[str] = []
        blocked: list[str] = []
        for lane in state.get("lanes", []):
            status = lane.get("status")
            if status == "completed" and _lane_output_valid(Path(root), lane):
                reused.append(lane["id"])
            elif status in SELECTABLE_STATUSES or status == "completed":
                selected.append(lane["id"])
            elif status == "terminal":
                blocked.append(lane["id"])
        state["metrics"]["reused_lanes"] = len(reused)
        state["metrics"]["redone_lanes"] = len(selected)
        state["metrics"]["blocked_lanes"] = len(blocked)
        state["metrics"]["stale_lanes"] = state["metrics"].get("stale_lanes", 0) + len(newly_stale)
        _refresh_lane_phase(state)
        save_state(root, state)
        status = "HOLD" if blocked else "READY"
        result = {"status": status, "selected_lanes": selected, "reused_lanes": reused, "blocked_lanes": blocked, "next_phase": _next_phase(state)}
        if blocked:
            result["hold_reason"] = "terminal_lanes_block_resume"
        return result


def _next_phase(state: dict[str, Any]) -> str | None:
    for phase in _state_phase_order(state):
        if state.get("phases", {}).get(phase, {}).get("status") != "complete":
            return phase
    return None


def set_lane_dependencies(root: Path | str, lane_id: str, dependencies: list[str]) -> dict[str, Any]:
    with _file_lock(root):
        state = load_state(root)
        lane = _lane(state, lane_id)
        lane["dependencies"] = list(dict.fromkeys(dependencies))
        lane["timestamps"]["updated_at"] = now_iso()
        save_state(root, state)
        return lane


def invalidate_artifact(root: Path | str, artifact_path: str, *, reason: str) -> dict[str, Any]:
    with _file_lock(root):
        state = load_state(root)
        changed_lane_ids = [lane["id"] for lane in state.get("lanes", []) if lane.get("output_path") == artifact_path]
        dependents: list[str] = []
        frontier = list(changed_lane_ids)
        while frontier:
            current = frontier.pop(0)
            for lane in state.get("lanes", []):
                if current in (lane.get("dependencies") or []) and lane["id"] not in dependents:
                    dependents.append(lane["id"])
                    frontier.append(lane["id"])
        timestamp = now_iso()
        for lane_id in dependents:
            lane = _lane(state, lane_id)
            lane["status"] = "stale"
            lane["terminal_reason"] = reason
            lane["invalidated_by"] = changed_lane_ids
            lane["timestamps"]["updated_at"] = timestamp
        state["metrics"]["invalidated_lanes"] = state["metrics"].get("invalidated_lanes", 0) + len(dependents)
        state.setdefault("events", []).append({"type": "invalidation", "at": timestamp, "artifact_path": artifact_path, "reason": reason, "invalidated_lanes": dependents})
        save_state(root, state)
        return {"changed_lanes": changed_lane_ids, "invalidated_lanes": dependents}


def restart_packet(root: Path | str, *, interruption_reason: str) -> dict[str, Any]:
    selection = resume_selection(root)
    state = load_state(root)
    packet = {
        "schema_version": "deep-research-restart-packet-v1",
        "campaign_root": str(root),
        "created_at": now_iso(),
        "interruption_reason": interruption_reason,
        "next_phase": selection["next_phase"],
        "resume": selection,
        "metrics": state.get("metrics", {}),
        "export_closeout": state.get("export_closeout", {}),
        "no_poll_loop_api": True,
    }
    write_json(_restart_path(root), packet)
    return packet


def _valid_hexish(value: str, *, min_len: int = 7) -> bool:
    return isinstance(value, str) and len(value) >= min_len and all(c in "0123456789abcdefABCDEF" for c in value)


def _validate_local_copy(root: Path, proof: Any) -> bool:
    if not isinstance(proof, dict):
        return False
    rel = proof.get("staged_path")
    digest = proof.get("sha256")
    if not rel or not digest:
        return False
    try:
        path = _safe_relative_path(root, str(rel), label="local_copy staged_path")
    except ValueError:
        return False
    return sha256_path(path) == digest


def _validate_local_commit(proof: Any) -> bool:
    return isinstance(proof, dict) and _valid_hexish(str(proof.get("commit", ""))) and bool(proof.get("ref"))


def _validate_remote_push(proof: Any, commit: str | None) -> bool:
    if not isinstance(proof, dict):
        return False
    if not proof.get("remote") or not proof.get("ref") or not _valid_hexish(str(proof.get("commit", ""))):
        return False
    return commit is None or proof.get("commit") == commit


def _validate_remote_readback(root: Path, proof: Any, local_copy: Any) -> bool:
    if not isinstance(proof, dict):
        return False
    path = proof.get("path")
    digest = proof.get("sha256") or proof.get("readback_sha256")
    if not path or not digest:
        return False
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        return False
    try:
        p.resolve(strict=True).relative_to(root.resolve(strict=True))
        return False
    except ValueError:
        pass
    local_digest = local_copy.get("sha256") if isinstance(local_copy, dict) else None
    return bool(local_digest) and digest == local_digest and sha256_path(p) == digest


def record_export_proof(root: Path | str, *, local_copy: Any | None = None, local_commit: Any | None = None, remote_push: Any | None = None, remote_readback: Any | None = None) -> dict[str, Any]:
    with _file_lock(root):
        state = load_state(root)
        root_path = Path(root)
        vc = state.setdefault("export_closeout", _export_closeout(True))
        vc["requested"] = True
        if local_copy is not None:
            vc["local_copy"] = local_copy
        if local_commit is not None:
            vc["local_commit"] = local_commit
        if remote_push is not None:
            vc["remote_push"] = remote_push
        if remote_readback is not None:
            vc["remote_readback"] = remote_readback
        commit_value = vc.get("local_commit", {}).get("commit") if isinstance(vc.get("local_commit"), dict) else None
        validators = {
            "local_copy": _validate_local_copy(root_path, vc.get("local_copy")),
            "local_commit": _validate_local_commit(vc.get("local_commit")),
            "remote_push": _validate_remote_push(vc.get("remote_push"), commit_value),
            "remote_readback": _validate_remote_readback(root_path, vc.get("remote_readback"), vc.get("local_copy")),
        }
        vc["validated"] = validators
        missing = [name for name, ok in validators.items() if not ok]
        vc["complete"] = not missing
        vc["terminal_reason"] = "complete" if not missing else "missing_proofs:" + ",".join(missing)
        save_state(root, state)
        return vc


def record_plain_brief(root: Path | str, manifest: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mark the plain_brief phase complete only when the shared plain-brief gate is clean.

    The gate (``plain_brief_rewrite.verify_campaign_plain_brief``) checks the reader lint, the
    independent editor review and that the review is bound to the current brief and full-pack hashes.
    """
    import importlib.util

    root_path = Path(root)
    if manifest is None:
        manifest = load_json(root_path / "manifest.json")
    spec = importlib.util.spec_from_file_location("plain_brief_rewrite", Path(__file__).resolve().parent / "plain_brief_rewrite.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gate = module.verify_campaign_plain_brief(root_path, manifest)
    problems = gate["failures"] + gate["holds"]
    if not gate["declared"]:
        problems = ["plain_brief_not_declared"]
    with _file_lock(root):
        state = load_state(root)
        phase = state.setdefault("phases", _default_phases()).setdefault("plain_brief", {"status": "pending", "started_at": None, "completed_at": None, "terminal_reason": None})
        timestamp = now_iso()
        phase["started_at"] = phase.get("started_at") or timestamp
        if problems:
            phase.update({"status": "blocked", "completed_at": None, "terminal_reason": "; ".join(problems)})
        else:
            phase.update({"status": "complete", "completed_at": timestamp, "terminal_reason": None})
        save_state(root, state)
        return {"status": phase["status"], "problems": problems, "metrics": gate["metrics"]}


def record_context_release(root: Path | str) -> dict[str, Any]:
    """Layout v2: record when the controller may first read the Context Brief.

    The Context Brief is read only after all research lanes finish. The first recorded time is kept.
    Legacy campaigns release the Context Brief after the external freeze, so this helper refuses them.
    """
    root_path = Path(root)
    try:
        manifest = load_json(root_path / "manifest.json")
    except (OSError, ValueError):
        manifest = {}
    if not is_v2_manifest(manifest):
        return {"status": "refused", "reason": "not_in_brief_and_reference_layout"}
    with _file_lock(root):
        state = load_state(root)
        lanes = state.get("lanes", [])
        if not lanes or not all(lane.get("status") == "completed" for lane in lanes):
            return {"status": "refused", "reason": "research_lanes_not_finished"}
        prior = state.get("context_release")
        if isinstance(prior, dict) and prior.get("released_at"):
            return {"status": "released", "released_at": prior["released_at"], "already_recorded": True}
        timestamp = now_iso()
        state["context_release"] = {"released_at": timestamp, "rule": "read_only_after_all_research_lanes_finish"}
        state.setdefault("events", []).append({"type": "context_release", "at": timestamp, "lanes_completed": [lane.get("id") for lane in lanes]})
        save_state(root, state)
        return {"status": "released", "released_at": timestamp}


def record_idle(root: Path | str, *, seconds: int | float, reason: str) -> dict[str, Any]:
    return _record_timing(root, kind="idle", seconds=seconds, reason=reason)


def record_active(root: Path | str, *, seconds: int | float, reason: str) -> dict[str, Any]:
    return _record_timing(root, kind="active", seconds=seconds, reason=reason)


def _record_timing(root: Path | str, *, kind: str, seconds: int | float, reason: str) -> dict[str, Any]:
    if seconds < 0:
        raise ValueError("timing seconds must be non-negative")
    with _file_lock(root):
        state = load_state(root)
        metrics = state.setdefault("metrics", _default_metrics())
        key = f"{kind}_time_seconds"
        event_key = f"{kind}_events"
        metrics[key] = metrics.get(key, 0) + seconds
        metrics[event_key] = metrics.get(event_key, 0) + 1
        event = {"type": f"{kind}_time", "seconds": seconds, "reason": reason, "at": now_iso()}
        state.setdefault("timing_events", []).append(event)
        save_state(root, state)
        return event


def timing_metrics(root: Path | str) -> dict[str, Any]:
    metrics = load_state(root).get("metrics", {})
    return {
        "active_time_seconds": metrics.get("active_time_seconds", 0),
        "idle_time_seconds": metrics.get("idle_time_seconds", 0),
        "active_events": metrics.get("active_events", 0),
        "idle_events": metrics.get("idle_events", 0),
    }


def record_repair_cycle(root: Path | str, *, failure_class: str, artifact_path: str, max_cycles: int = 1) -> dict[str, Any]:
    if not failure_class or failure_class.strip().upper() in {"TBD", "TODO", "N/A"}:
        raise ValueError("failure_class must be a concrete material failure class")
    if max_cycles < 1:
        raise ValueError("max_cycles must be positive")
    with _file_lock(root):
        state = load_state(root)
        cycles = state.setdefault("repair_cycles", {})
        entry = cycles.setdefault(failure_class, {"count": 0, "max_cycles": max_cycles, "artifacts": [], "blocked": False})
        entry["max_cycles"] = max_cycles
        if entry.get("count", 0) >= max_cycles:
            entry["blocked"] = True
            save_state(root, state)
            return {"status": "repair_blocked", "failure_class": failure_class, "reason": f"repair_cycle_cap_exceeded:{failure_class}", "count": entry.get("count", 0), "max_cycles": max_cycles}
        entry["count"] = entry.get("count", 0) + 1
        entry.setdefault("artifacts", []).append({"path": artifact_path, "at": now_iso()})
        entry["blocked"] = entry["count"] >= max_cycles
        save_state(root, state)
        return {"status": "repair_allowed", "failure_class": failure_class, "count": entry["count"], "max_cycles": max_cycles}
