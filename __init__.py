"""Hermes Deep Research plugin — skills, preflight/cost estimator, campaign scaffold, and completion checker."""

from __future__ import annotations

import contextlib
import datetime
import functools
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

PLUGIN_ID = "hermes-deep-research"
_MAX_ERROR_CHARS = 500
_MAX_TEXT_CHARS = 2000
PLUGIN_DIR = Path(__file__).resolve().parent
SKILLS_DIR = PLUGIN_DIR / "skills"
DEEP_RESEARCH_SCRIPTS = SKILLS_DIR / "deep-research" / "scripts"
FAST_RESEARCH_CONFIG = SKILLS_DIR / "deep-research-fast" / "config" / "fast-mode.json"

SKILL_DEFINITIONS: tuple[tuple[str, str, str], ...] = (
    (
        "deep-research",
        "skills/deep-research/SKILL.md",
        "Multi-lane research campaign with saved sources, quote verification, and independent review.",
    ),
    (
        "deep-research-fast",
        "skills/deep-research-fast/SKILL.md",
        "Fast parallel-search research mode with verbatim quote and meaning checks.",
    ),
    (
        "deepest-research",
        "skills/deepest-research/SKILL.md",
        "Extended multi-branch fast research with follow-up gap runs and review round.",
    ),
)

PREFLIGHT_SCHEMA: dict[str, Any] = {
    "name": "deep_research_preflight",
    "description": (
        "Inspect deep-research role models, check credential availability via Hermes secret scope, "
        "and estimate token and USD cost bounds before running fast, deepest, or full campaign research."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["fast", "deepest", "full"],
                "description": "Research mode to inspect ('fast', 'deepest', or 'full'). Defaults to 'fast'.",
            },
            "question": {
                "type": "string",
                "maxLength": 2000,
                "description": "Optional research question or target to include in the preflight summary.",
            },
            "report_purpose": {
                "type": "string",
                "enum": ["factual", "decision_assessment"],
                "description": "Report purpose for full campaigns ('factual' or 'decision_assessment').",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

INIT_CAMPAIGN_SCHEMA: dict[str, Any] = {
    "name": "deep_research_init_campaign",
    "description": (
        "Scaffold a local deep-research campaign directory (manifest, lane plan, worker packets, "
        "reference pack template, and QA receipt stubs) without making network or paid model calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2000,
                "description": "Research question or target topic to scaffold.",
            },
            "report_purpose": {
                "type": "string",
                "enum": ["factual", "decision_assessment"],
                "description": "Campaign report purpose ('factual' by default, or 'decision_assessment').",
            },
            "target_type": {
                "type": "string",
                "maxLength": 64,
                "description": "Target category (e.g., 'idea', 'product', 'repo', 'market', 'workflow').",
            },
            "campaign_root": {
                "type": "string",
                "description": (
                    "Optional campaign folder. Relative paths resolve inside the plugin campaigns folder; "
                    "absolute paths must already be inside it. Defaults to a dated folder there."
                ),
            },
            "deliverable_export": {
                "type": "boolean",
                "description": "Whether deliverables export is enabled for this campaign (default false).",
            },
        },
        "required": ["target"],
        "additionalProperties": False,
    },
}

CHECK_COMPLETION_SCHEMA: dict[str, Any] = {
    "name": "deep_research_check_completion",
    "description": (
        "Run the deterministic deep-research completion, receipt-integrity, and saved-quote check "
        "against a campaign root directory and return the structured PASS/HOLD/FAIL verdict."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "campaign_root": {
                "type": "string",
                "description": (
                    "Campaign folder containing manifest.json. Relative paths resolve inside the plugin "
                    "campaigns folder; absolute paths must already be inside it."
                ),
            },
            "offline": {
                "type": "boolean",
                "description": "Skip live OpenRouter generation verification (default true; offline max verdict is HOLD).",
            },
            "probe": {
                "type": "boolean",
                "description": "Whether to run a live web_search probe during completion check (default false).",
            },
            "write_receipt": {
                "type": "boolean",
                "description": "Whether to write receipts/completion.json inside the campaign root (default false).",
            },
        },
        "required": ["campaign_root"],
        "additionalProperties": False,
    },
}


def _get_secret(name: str) -> str | None:
    """Resolve a credential through Hermes ``agent.secret_scope``; never read ``.env`` files.

    Inside Hermes the scope owns the lookup, including the profile's ``.env`` overlay
    and the process environment where that is allowed. A miss is final: falling back
    to ``os.environ`` here would hand one profile another profile's key under the
    multiplexing gateway. ``os.environ`` is read only when Hermes is not importable.
    """
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        value = os.environ.get(name)
    else:
        value = get_secret(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _default_campaigns_dir() -> Path:
    """Return the profile-scoped campaigns folder, resolved on every call.

    Outside Hermes (``plugins.plugin_storage`` not importable) the CLI default
    ``RESEARCH_ARCHIVE_ROOT`` or ``~/.hermes/research-archive`` is used. Other
    failures inside Hermes propagate instead of silently switching folders.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir
    except ImportError:
        base = Path(os.environ.get("RESEARCH_ARCHIVE_ROOT", "~/.hermes/research-archive")).expanduser()
    else:
        base = Path(plugin_data_dir(PLUGIN_ID)) / "campaigns"
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def _confined_campaign(raw: str) -> Path:
    """Resolve a campaign path and require it to sit strictly inside the campaigns folder."""
    root = _default_campaigns_dir()
    candidate = Path(raw).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"campaign_root must be a folder inside the plugin campaigns folder ({root})")
    return resolved


# ---------------------------------------------------------------------------
# Argument checks and the shared tool envelope
# ---------------------------------------------------------------------------

def _error_payload(tool: str, exc: BaseException) -> dict[str, Any]:
    message = str(exc) or type(exc).__name__
    if len(message) > _MAX_ERROR_CHARS:
        message = message[:_MAX_ERROR_CHARS] + "..."
    return {"ok": False, "tool": tool, "error": message, "error_type": type(exc).__name__}


def _json_tool(tool: str):
    """Wrap a handler that takes a checked dict and returns a dict: always a JSON string, never raises."""

    def decorate(handler):
        @functools.wraps(handler)
        def wrapped(args: Any = None, **kwargs: Any) -> str:
            try:
                if args is None:
                    args = {}
                if not isinstance(args, dict):
                    raise ValueError("tool arguments must be a JSON object")
                return json.dumps(handler(args))
            except Exception as exc:  # noqa: BLE001 - tool contract: always return JSON
                return json.dumps(_error_payload(tool, exc))
            except SystemExit as exc:  # a bundled script called sys.exit / raise SystemExit
                return json.dumps(_error_payload(tool, RuntimeError(f"script exited: {exc.code}")))

        return wrapped

    return decorate


def _opt_text(args: dict[str, Any], key: str, default: str = "", *, limit: int = _MAX_TEXT_CHARS) -> str:
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{key} must be at most {limit} characters")
    return value or default


def _opt_choice(args: dict[str, Any], key: str, allowed: tuple[str, ...], default: str) -> str:
    value = _opt_text(args, key, default, limit=64).lower()
    if value not in allowed:
        raise ValueError(f"Unsupported {key} {value!r}; expected one of: {', '.join(allowed)}.")
    return value


def _opt_bool(args: dict[str, Any], key: str, default: bool) -> bool:
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _load_script_module(script_name: str):
    """Load a module from skills/deep-research/scripts without mutating sys.path."""
    path = DEEP_RESEARCH_SCRIPTS / f"{script_name}.py"
    spec = importlib.util.spec_from_file_location(f"_deep_research_{script_name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load script module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _estimate_fast_cost(cfg: dict[str, Any], *, deepest: bool = False) -> dict[str, Any]:
    """Compute a pre-run token and USD cost estimate from fast-mode.json list prices."""
    models = cfg.get("models", {})
    limits = cfg.get("limits", {})

    def _rate(role: str) -> tuple[float, float]:
        entry = models.get(role, {})
        prompt_rate = float(entry.get("prompt_usd_per_token", 0.0) or 0.0)
        completion_rate = float(entry.get("completion_usd_per_token", 0.0) or 0.0)
        return prompt_rate, completion_rate

    def _call_cost(role: str, prompt_tokens: int, completion_tokens: int) -> float:
        p_rate, c_rate = _rate(role)
        return prompt_tokens * p_rate + completion_tokens * c_rate

    min_search_fetch = max(4, int(limits.get("min_topics", 4)) * 2)
    max_search_fetch = int(limits.get("max_search_fetch_jobs", 38))
    typical_search_fetch = min(max_search_fetch, max(min_search_fetch, 16))

    base_fixed = (
        _call_cost("planner", 2500, 1200)
        + _call_cost("writer", 18000, 4500)
        + _call_cost("reviewer", 22000, 1800)
    )
    per_job = _call_cost("worker", 6000, 900) + _call_cost("checker", 5000, 600)

    min_usd = base_fixed + min_search_fetch * per_job
    typical_usd = base_fixed + typical_search_fetch * per_job
    max_usd = base_fixed + max_search_fetch * per_job

    if deepest:
        deepest_cfg = cfg.get("deepest", {})
        branches = int(deepest_cfg.get("parallel_branches", 4))
        branch_jobs = int(deepest_cfg.get("branch_limits", {}).get("max_search_fetch_jobs", 16))
        review_round = bool(deepest_cfg.get("review_round", {}).get("enabled", True))
        multiplier = 1 + max(1, branches // 2)
        max_multiplier = 1 + branches + (branches if review_round else 0)
        min_usd = min_usd * 1.5
        typical_usd = typical_usd * multiplier + (base_fixed if review_round else 0.0)
        max_usd = max_usd + (max_multiplier - 1) * (base_fixed + branch_jobs * per_job)

    return {
        "currency": "USD",
        "min_usd": round(min_usd, 4),
        "typical_usd": round(typical_usd, 4),
        "max_usd": round(max_usd, 4),
        "note": (
            "Estimated from captured OpenRouter list prices in fast-mode.json before execution; "
            "actual billing is recorded per call in session records."
        ),
    }


def _estimate_full_cost(settings: Any) -> dict[str, Any]:
    """Compute a pre-run USD cost estimate for a full multi-lane campaign from model-routing.yaml."""
    catalogue_models = settings._routing.get("catalogue", {}).get("models", {})

    def _role_call_cost(role: str, prompt_tokens: int, completion_tokens: int) -> float:
        resolved = settings.resolve(role)
        model_id = resolved["model"]
        pricing = catalogue_models.get(model_id, {}).get("pricing", {})
        prompt_rate = float(pricing.get("prompt", 0.0) or 0.0)
        completion_rate = float(pricing.get("completion", 0.0) or 0.0)
        return prompt_tokens * prompt_rate + completion_tokens * completion_rate

    lane_cost = _role_call_cost("evidence", 16000, 4000)
    synthesis_cost = _role_call_cost("synthesis", 40000, 10000)
    writer_cost = _role_call_cost("writer", 35000, 6000)
    editor_cost = _role_call_cost("brief_reviewer", 45000, 2500)
    qa_cost = _role_call_cost("reviewer", 30000, 3000)

    min_usd = 5 * lane_cost + synthesis_cost + writer_cost + editor_cost + 2 * qa_cost
    typical_usd = 6 * lane_cost + synthesis_cost + writer_cost + editor_cost + 4 * qa_cost
    max_usd = 9 * lane_cost + 2 * (synthesis_cost + writer_cost + editor_cost) + 6 * qa_cost
    return {
        "currency": "USD",
        "min_usd": round(min_usd, 4),
        "typical_usd": round(typical_usd, 4),
        "max_usd": round(max_usd, 4),
        "note": (
            "Estimated from model-routing.yaml catalogue pricing for 5-9 lanes plus synthesis, "
            "plain-brief rewrite, and independent review."
        ),
    }


MODES = ("fast", "deepest", "full")
PURPOSES = ("factual", "decision_assessment")


@_json_tool("deep_research_preflight")
def deep_research_preflight(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler: preflight check and cost estimator for fast, deepest, or full research."""
    mode = _opt_choice(args, "mode", MODES, "fast")
    report_purpose = _opt_choice(args, "report_purpose", PURPOSES, "factual")
    question = _opt_text(args, "question")

    openrouter_present = bool(_get_secret("OPENROUTER_API_KEY"))
    apify_present = bool(_get_secret("APIFY_TOKEN"))

    if mode in {"fast", "deepest"}:
        cfg = json.loads(FAST_RESEARCH_CONFIG.read_text(encoding="utf-8"))
        roles = {
            role: {
                "model": details.get("id"),
                "vendor": details.get("vendor"),
                "prompt_usd_per_token": details.get("prompt_usd_per_token"),
                "completion_usd_per_token": details.get("completion_usd_per_token"),
            }
            for role, details in (cfg.get("models") or {}).items()
        }
        limits = cfg.get("limits", {})
        cost_estimate = _estimate_fast_cost(cfg, deepest=(mode == "deepest"))
    else:
        rs = _load_script_module("research_settings")
        settings = rs.load_settings(environ={})
        roles = {}
        for role in sorted(rs.ROLES):
            resolved = settings.resolve(role)
            roles[role] = {
                "model": resolved["model"],
                "developer": settings.developer(resolved["model"]),
                "fallback_models": resolved["fallback_models"],
                "max_tokens": resolved["max_tokens"],
            }
        limits = dict(settings._execution)
        cost_estimate = _estimate_full_cost(settings)

    return {
        "ok": True,
        "mode": mode,
        "question": question or None,
        "report_purpose": report_purpose,
        "credentials": {
            "openrouter_key_present": openrouter_present,
            "apify_token_present": apify_present,
        },
        "ready_for_live_calls": openrouter_present,
        "roles": roles,
        "limits": limits,
        "estimated_cost_usd": cost_estimate,
        "disclosures": {
            "network_services": [
                "https://openrouter.ai/api/v1/chat/completions",
                "https://openrouter.ai/api/v1/generation",
                "https://api.apify.com/v2/acts",
                "https://r.jina.ai/ (full-mode page reader)",
                "https://web.archive.org/ (through the page reader)",
                "https://api.github.com/ (gh CLI, full-mode lanes)",
                "any cited source URL (full-mode lanes fetch it directly with curl)",
            ],
            "secret_resolution": "agent.secret_scope.get_secret (os.environ only when Hermes is not importable; no direct .env reads)",
        },
    }


@_json_tool("deep_research_init_campaign")
def deep_research_init_campaign(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler: scaffold a local deep-research campaign directory inside the campaigns folder."""
    target = _opt_text(args, "target")
    if not target:
        raise ValueError("Parameter 'target' is required and must be non-empty.")
    report_purpose = _opt_choice(args, "report_purpose", PURPOSES, "factual")
    target_type = _opt_text(args, "target_type", "idea", limit=64).lower()
    deliverable_export = _opt_bool(args, "deliverable_export", False)
    raw_root = _opt_text(args, "campaign_root", limit=4096)

    if raw_root:
        root_path = _confined_campaign(raw_root)
    else:
        slug = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:48] or "campaign"
        root_path = _confined_campaign(f"{datetime.date.today().isoformat()}-{slug}")

    init_mod = _load_script_module("init_campaign")
    argv = [
        "--target", target,
        "--report-purpose", report_purpose,
        "--target-type", target_type,
        "--root", str(root_path),
    ]
    if not deliverable_export:
        argv.append("--no-deliverable-export")

    stdout_buf = io.StringIO()
    with contextlib.redirect_stdout(stdout_buf):
        rc = init_mod.main(argv)

    manifest_path = root_path / "manifest.json"
    if rc != 0 or not manifest_path.is_file():
        raise RuntimeError(f"init_campaign exited with code {rc}: {stdout_buf.getvalue().strip()}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "ok": True,
        "campaign_root": str(root_path),
        "manifest_path": str(manifest_path),
        "report_purpose": manifest.get("report_purpose"),
        "output_layout": manifest.get("output_layout"),
        "lane_count": len(manifest.get("lanes") or []),
        "lanes": [lane.get("id") for lane in (manifest.get("lanes") or []) if isinstance(lane, dict)],
    }


def _run_completion_check(root_path: Path, *, offline: bool, probe: bool) -> dict[str, Any]:
    """Run scripts/complete_campaign.py in a Python child process and return its JSON result.

    The verifier imports its sibling scripts by name, so it runs as a script (its folder is then
    first on the child's import path) instead of being loaded into the Hermes process.
    """
    cmd = [sys.executable, str(DEEP_RESEARCH_SCRIPTS / "complete_campaign.py"),
           "--campaign-root", str(root_path), "--no-relaunch", "--no-write"]
    if offline:
        cmd.append("--offline")
    if probe:
        cmd.append("--probe")
    env = dict(os.environ)
    env.pop("OPENROUTER_API_KEY", None)
    if not offline or probe:
        key = _get_secret("OPENROUTER_API_KEY")
        if key:
            env["OPENROUTER_API_KEY"] = key
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, cwd=str(root_path), env=env)
    start = proc.stdout.find("{")
    try:
        result = json.loads(proc.stdout[start:]) if start >= 0 else None
    except json.JSONDecodeError:
        result = None
    if not isinstance(result, dict) or "status" not in result:
        detail = (proc.stderr or proc.stdout).strip()[-500:]
        raise RuntimeError(f"completion check exited with code {proc.returncode}: {detail}")
    return result


@_json_tool("deep_research_check_completion")
def deep_research_check_completion(args: dict[str, Any]) -> dict[str, Any]:
    """Tool handler: run completion, receipt-integrity, and saved-quote checks on a campaign root."""
    raw_root = _opt_text(args, "campaign_root", limit=4096)
    if not raw_root:
        raise ValueError("Parameter 'campaign_root' is required.")
    offline = _opt_bool(args, "offline", True)
    probe = _opt_bool(args, "probe", False)
    write_receipt = _opt_bool(args, "write_receipt", False)

    root_path = _confined_campaign(raw_root)
    manifest_path = root_path / "manifest.json"
    if not root_path.is_dir() or not manifest_path.is_file():
        raise FileNotFoundError(f"Campaign manifest not found at {manifest_path}")

    result = _run_completion_check(root_path, offline=offline, probe=probe)

    if write_receipt:
        out_file = root_path / "receipts" / "completion.json"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    return {
        "ok": result.get("status") == "PASS",
        "status": result.get("status"),
        "classes": result.get("classes", []),
        "steps": result.get("steps", []),
        "checked_files_sha256": result.get("checked_files_sha256", {}),
        "campaign_root": str(root_path),
        "online": not offline,
    }


HELP_TEXT = """\
/deep-research - research preflight, campaign scaffold and completion check

Usage:
  /deep-research preflight [mode] [question]   Show models, key state and cost estimate
  /deep-research estimate [mode] [question]    Same as preflight
  /deep-research init <target question>        Scaffold a local campaign folder
  /deep-research check <campaign_root>         Run the offline completion and quote check
  /deep-research help                          Show this message

mode is fast (default), deepest or full.

Examples:
  /deep-research preflight deepest How does X work?
  /deep-research init How does X work?
  /deep-research check <campaign_root printed by init>

Notes:
  These commands make no network calls and spend nothing. Key state is shown as
  present or missing, never as a value.
  Paid research runs through the bundled skills and needs OPENROUTER_API_KEY.
  Campaigns are saved under the plugin data folder.
  The same steps are available to the agent as deep_research_preflight,
  deep_research_init_campaign and deep_research_check_completion."""
_HELP_TEXT = HELP_TEXT  # name kept for callers that imported the old constant
HELP_WORDS = frozenset({"help", "-h", "--help", "?"})


def handle_deep_research_command(raw_args: str = "", **kwargs: Any) -> str:
    """Slash-command handler for /deep-research; reports problems as text and never raises."""
    _ = kwargs
    try:
        return _dispatch_command(raw_args)
    except Exception as exc:  # noqa: BLE001 - never crash the session
        return f"deep-research error: {_error_payload('deep-research', exc)['error']}"


def _dispatch_command(raw_args: str) -> str:
    text = (raw_args or "").strip()
    if not text or text.lower() in HELP_WORDS:
        return HELP_TEXT

    parts = text.split(maxsplit=1)
    subcmd = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    if subcmd in {"preflight", "estimate"}:
        mode = "fast"
        question = rest
        if rest:
            first, *tail = rest.split(maxsplit=1)
            if first.lower() in {"fast", "deepest", "full"}:
                mode = first.lower()
                question = tail[0] if tail else ""
        payload = json.loads(deep_research_preflight({"mode": mode, "question": question}))
        if not payload.get("ok"):
            return f"deep-research preflight error: {payload.get('error')}"
        est = payload["estimated_cost_usd"]
        creds = payload["credentials"]
        return (
            f"Deep Research Preflight ({payload['mode']} mode)\n"
            f"  OpenRouter key present: {creds['openrouter_key_present']}\n"
            f"  Apify token present:    {creds['apify_token_present']}\n"
            f"  Estimated cost (USD):   ${est['min_usd']:.2f} min / ${est['typical_usd']:.2f} typical / ${est['max_usd']:.2f} max\n"
            f"  Roles configured:       {', '.join(sorted(payload['roles'].keys()))}"
        )

    if subcmd == "init":
        if not rest:
            return "Usage: /deep-research init <target question>. Run /deep-research help for examples."
        payload = json.loads(deep_research_init_campaign({"target": rest}))
        if not payload.get("ok"):
            return f"deep-research init error: {payload.get('error')}"
        return (
            f"Scaffolded deep-research campaign at {payload['campaign_root']}\n"
            f"  Purpose: {payload['report_purpose']} ({payload['output_layout']})\n"
            f"  Lanes ({payload['lane_count']}): {', '.join(payload['lanes'])}"
        )

    if subcmd == "check":
        if not rest:
            return "Usage: /deep-research check <campaign_root>. Run /deep-research help for examples."
        payload = json.loads(deep_research_check_completion({"campaign_root": rest, "offline": True}))
        if "status" not in payload and not payload.get("ok"):
            return f"deep-research check error: {payload.get('error')}"
        return (
            f"Campaign completion check: {payload.get('status')}\n"
            f"  Root:    {payload.get('campaign_root')}\n"
            f"  Classes: {', '.join(payload.get('classes') or []) or 'none'}"
        )

    return f"Unknown subcommand: {subcmd}. Run /deep-research help for usage."


def register(ctx: Any) -> None:
    """Register the three research skills, tool wrappers, and /deep-research command."""
    for skill_name, rel_path, description in SKILL_DEFINITIONS:
        skill_md = PLUGIN_DIR / rel_path
        if skill_md.is_file():
            ctx.register_skill(skill_name, skill_md, description=description)

    ctx.register_tool(
        name="deep_research_preflight",
        toolset="deep_research",
        schema=PREFLIGHT_SCHEMA,
        handler=deep_research_preflight,
        description=PREFLIGHT_SCHEMA["description"],
    )
    ctx.register_tool(
        name="deep_research_init_campaign",
        toolset="deep_research",
        schema=INIT_CAMPAIGN_SCHEMA,
        handler=deep_research_init_campaign,
        description=INIT_CAMPAIGN_SCHEMA["description"],
    )
    ctx.register_tool(
        name="deep_research_check_completion",
        toolset="deep_research",
        schema=CHECK_COMPLETION_SCHEMA,
        handler=deep_research_check_completion,
        description=CHECK_COMPLETION_SCHEMA["description"],
    )
    ctx.register_command(
        "deep-research",
        handler=handle_deep_research_command,
        description="Preflight and cost estimate, scaffold or check a research campaign",
        args_hint="preflight|estimate|init|check [args] | help",
    )
