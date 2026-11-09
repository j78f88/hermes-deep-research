"""Hermes Deep Research plugin — skills, preflight/cost estimator, campaign scaffold, and completion checker."""

from __future__ import annotations

import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

PLUGIN_ID = "hermes-deep-research"
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
                "description": "Optional research question or target to include in the preflight summary.",
            },
            "report_purpose": {
                "type": "string",
                "enum": ["factual", "decision_assessment"],
                "description": "Report purpose for full campaigns ('factual' or 'decision_assessment').",
            },
        },
        "required": [],
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
                "description": "Research question or target topic to scaffold.",
            },
            "report_purpose": {
                "type": "string",
                "enum": ["factual", "decision_assessment"],
                "description": "Campaign report purpose ('factual' by default, or 'decision_assessment').",
            },
            "target_type": {
                "type": "string",
                "description": "Target category (e.g., 'idea', 'product', 'repo', 'market', 'workflow').",
            },
            "campaign_root": {
                "type": "string",
                "description": "Optional directory path for the campaign root. Defaults to the plugin data directory.",
            },
            "deliverable_export": {
                "type": "boolean",
                "description": "Whether deliverables export is enabled for this campaign (default false).",
            },
        },
        "required": ["target"],
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
                "description": "Path to the campaign root directory containing manifest.json.",
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
    },
}


def _get_secret(name: str) -> str | None:
    """Resolve a credential through Hermes secret_scope with os.environ fallback; never read .env files."""
    try:
        from agent.secret_scope import get_secret
        value = get_secret(name)
    except ImportError:
        value = None
    if not value:
        value = os.environ.get(name)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _default_campaigns_dir() -> Path:
    """Return the profile-scoped plugin data directory for campaigns, or ~/.hermes/research-archive fallback."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        base = Path(plugin_data_dir(PLUGIN_ID)) / "campaigns"
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        fallback = Path(os.environ.get("RESEARCH_ARCHIVE_ROOT", "~/.hermes/research-archive")).expanduser()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


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


def deep_research_preflight(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Tool handler: preflight check and cost estimator for fast, deepest, or full research."""
    _ = kwargs
    args = dict(args or {})
    mode = str(args.get("mode") or "fast").strip().lower()
    question = str(args.get("question") or "").strip()
    report_purpose = str(args.get("report_purpose") or "factual").strip().lower()

    if mode not in {"fast", "deepest", "full"}:
        return json.dumps({
            "ok": False,
            "error": f"Unsupported mode {mode!r}; expected one of: fast, deepest, full.",
        })
    if report_purpose not in {"factual", "decision_assessment"}:
        return json.dumps({
            "ok": False,
            "error": f"Unsupported report_purpose {report_purpose!r}; expected factual or decision_assessment.",
        })

    try:
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

        return json.dumps({
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
                ],
                "secret_resolution": "agent.secret_scope.get_secret (with os.environ fallback; no direct .env reads)",
            },
        })
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Preflight failed: {type(exc).__name__}: {exc}"})


def deep_research_init_campaign(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Tool handler: scaffold a local deep-research campaign directory."""
    _ = kwargs
    args = dict(args or {})
    target = str(args.get("target") or "").strip()
    if not target:
        return json.dumps({"ok": False, "error": "Parameter 'target' is required and must be non-empty."})

    report_purpose = str(args.get("report_purpose") or "factual").strip().lower()
    target_type = str(args.get("target_type") or "idea").strip().lower()
    deliverable_export = bool(args.get("deliverable_export", False))
    raw_root = str(args.get("campaign_root") or "").strip()

    try:
        init_mod = _load_script_module("init_campaign")
        today = datetime.date.today().isoformat()
        slug = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:48] or "campaign"
        if raw_root:
            root_path = Path(raw_root).expanduser().resolve()
        else:
            root_path = (_default_campaigns_dir() / f"{today}-{slug}").resolve()

        argv = [
            str(DEEP_RESEARCH_SCRIPTS / "init_campaign.py"),
            "--target",
            target,
            "--report-purpose",
            report_purpose,
            "--target-type",
            target_type,
            "--root",
            str(root_path),
        ]
        if not deliverable_export:
            argv.append("--no-deliverable-export")

        stdout_buf = io.StringIO()
        old_argv = sys.argv[:]
        try:
            sys.argv = argv
            with contextlib.redirect_stdout(stdout_buf):
                rc = init_mod.main()
        finally:
            sys.argv = old_argv

        manifest_path = root_path / "manifest.json"
        if rc != 0 or not manifest_path.is_file():
            return json.dumps({
                "ok": False,
                "error": f"init_campaign exited with code {rc}",
                "stdout": stdout_buf.getvalue().strip(),
            })

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return json.dumps({
            "ok": True,
            "campaign_root": str(root_path),
            "manifest_path": str(manifest_path),
            "report_purpose": manifest.get("report_purpose"),
            "output_layout": manifest.get("output_layout"),
            "lane_count": len(manifest.get("lanes") or []),
            "lanes": [lane.get("id") for lane in (manifest.get("lanes") or []) if isinstance(lane, dict)],
        })
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Campaign scaffold failed: {type(exc).__name__}: {exc}"})


def deep_research_check_completion(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
    """Tool handler: run completion, receipt-integrity, and saved-quote checks on a campaign root."""
    _ = kwargs
    args = dict(args or {})
    raw_root = str(args.get("campaign_root") or "").strip()
    if not raw_root:
        return json.dumps({"ok": False, "error": "Parameter 'campaign_root' is required."})

    offline = bool(args.get("offline", True))
    probe = bool(args.get("probe", False))
    write_receipt = bool(args.get("write_receipt", False))

    try:
        root_path = Path(raw_root).expanduser().resolve()
        manifest_path = root_path / "manifest.json"
        if not root_path.is_dir() or not manifest_path.is_file():
            return json.dumps({
                "ok": False,
                "error": f"Campaign manifest not found at {manifest_path}",
            })

        cc = _load_script_module("complete_campaign")
        with contextlib.chdir(DEEP_RESEARCH_SCRIPTS):
            result = cc.evaluate(
                root_path,
                online=not offline,
                probe=probe,
                homes=[],
                pin_checker=False,
            )

        if write_receipt:
            out_file = root_path / "receipts" / "completion.json"
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

        return json.dumps({
            "ok": result.get("status") == "PASS",
            "status": result.get("status"),
            "classes": result.get("classes", []),
            "steps": result.get("steps", []),
            "checked_files_sha256": result.get("checked_files_sha256", {}),
            "campaign_root": str(root_path),
            "online": not offline,
        })
    except Exception as exc:
        return json.dumps({"ok": False, "error": f"Completion check failed: {type(exc).__name__}: {exc}"})


_HELP_TEXT = """\
/deep-research — research preflight, cost estimator, campaign scaffold, and completion checker

Subcommands:
  preflight [fast|deepest|full] [question]   Show role models, credential state, and estimated USD cost
  estimate [fast|deepest|full] [question]    Alias for preflight
  init <target question>                     Scaffold a new local factual campaign directory
  check <campaign_root>                      Run offline completion and quote checks on a campaign directory
  help                                       Show this usage message
"""


def handle_deep_research_command(raw_args: str = "", **kwargs: Any) -> str:
    """Slash-command handler for /deep-research."""
    _ = kwargs
    text = (raw_args or "").strip()
    if not text or text in {"help", "-h", "--help"}:
        return _HELP_TEXT

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
            return "Usage: /deep-research init <target question>"
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
            return "Usage: /deep-research check <campaign_root>"
        payload = json.loads(deep_research_check_completion({"campaign_root": rest, "offline": True}))
        if "status" not in payload and not payload.get("ok"):
            return f"deep-research check error: {payload.get('error')}"
        return (
            f"Campaign completion check: {payload.get('status')}\n"
            f"  Root:    {payload.get('campaign_root')}\n"
            f"  Classes: {', '.join(payload.get('classes') or []) or 'none'}"
        )

    return f"Unknown subcommand: {subcmd}\n\n{_HELP_TEXT}"


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
        description="Preflight, estimate cost, scaffold, or check a deep-research campaign",
        args_hint="[preflight|estimate|init|check] [args]",
    )
