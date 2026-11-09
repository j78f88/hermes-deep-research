"""Unit and regression tests for the Hermes plugin wrapper and catalog compliance."""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import sys
import types
from pathlib import Path

import pytest
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]


def load_plugin_module():
    spec = importlib.util.spec_from_file_location("hermes_deep_research_plugin", REPO_ROOT / "__init__.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_script_module(name: str):
    spec = importlib.util.spec_from_file_location(f"test_target_{name}", SCRIPT_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakePluginContext:
    def __init__(self) -> None:
        self.skills: list[dict] = []
        self.tools: list[dict] = []
        self.commands: list[dict] = []
        self.hooks: list[dict] = []

    def register_skill(self, name: str, path: Path, description: str = "", frontmatter=None) -> None:
        self.skills.append({
            "name": name,
            "path": Path(path),
            "description": description,
            "frontmatter": frontmatter,
        })

    def register_tool(self, name: str, toolset: str, schema: dict, handler, **kwargs) -> None:
        self.tools.append({
            "name": name,
            "toolset": toolset,
            "schema": schema,
            "handler": handler,
            "kwargs": kwargs,
        })

    def register_command(self, name: str, handler, description: str = "", args_hint: str = "", **kwargs) -> None:
        self.commands.append({
            "name": name,
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
            "kwargs": kwargs,
        })

    def register_hook(self, hook_name: str, callback, **kwargs) -> None:
        self.hooks.append({"hook_name": hook_name, "callback": callback, "kwargs": kwargs})


def test_manifest_and_pyproject_compliance() -> None:
    manifest_path = REPO_ROOT / "plugin.yaml"
    pyproject_path = REPO_ROOT / "pyproject.toml"
    assert manifest_path.is_file(), "plugin.yaml must exist at repository root"
    assert pyproject_path.is_file(), "pyproject.toml must exist at repository root"

    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "hermes-deep-research"
    assert manifest["license"] == "MIT"
    assert set(manifest["provides_tools"]) == {
        "deep_research_preflight",
        "deep_research_init_campaign",
        "deep_research_check_completion",
    }

    env_names = {
        entry["name"] if isinstance(entry, dict) else entry
        for entry in manifest.get("requires_env", [])
    }
    assert {"OPENROUTER_API_KEY", "APIFY_TOKEN"} <= env_names

    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore[no-redef]

    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    deps = set(pyproject["project"]["dependencies"])
    dev_deps = set(pyproject["project"]["optional-dependencies"]["dev"])
    assert "PyYAML>=6,<7" in deps
    assert "jsonschema>=4,<5" in deps
    assert "pytest>=8,<10" in dev_deps


def test_register_wires_skills_tools_and_command() -> None:
    plugin = load_plugin_module()
    ctx = FakePluginContext()
    plugin.register(ctx)

    skill_names = [s["name"] for s in ctx.skills]
    assert skill_names == ["deep-research", "deep-research-fast", "deepest-research"]
    for skill in ctx.skills:
        assert skill["path"].is_file(), f"Missing SKILL.md for {skill['name']}"

    tool_names = [t["name"] for t in ctx.tools]
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    assert tool_names == manifest["provides_tools"]

    command_names = [c["name"] for c in ctx.commands]
    assert command_names == ["deep-research"]


@pytest.mark.parametrize("mode", ["fast", "deepest", "full"])
def test_preflight_handler_returns_cost_and_credential_status(monkeypatch, mode: str) -> None:
    plugin = load_plugin_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-secret")
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    raw = plugin.deep_research_preflight({"mode": mode, "question": "How does X work?"})
    payload = json.loads(raw)
    assert payload["ok"] is True
    assert payload["mode"] == mode
    assert payload["credentials"]["openrouter_key_present"] is True
    assert payload["credentials"]["apify_token_present"] is False
    assert "test-openrouter-secret" not in raw
    cost = payload["estimated_cost_usd"]
    assert cost["currency"] == "USD"
    assert 0 < cost["min_usd"] <= cost["typical_usd"] <= cost["max_usd"]
    assert payload["roles"]


def test_preflight_handler_rejects_invalid_inputs_without_raising() -> None:
    plugin = load_plugin_module()
    bad_mode = json.loads(plugin.deep_research_preflight({"mode": "invalid"}))
    assert bad_mode["ok"] is False
    assert "mode" in bad_mode["error"]

    bad_purpose = json.loads(plugin.deep_research_preflight({"mode": "full", "report_purpose": "invalid"}))
    assert bad_purpose["ok"] is False
    assert "report_purpose" in bad_purpose["error"]


def test_init_and_check_completion_tool_handlers(tmp_path: Path) -> None:
    plugin = load_plugin_module()

    missing_target = json.loads(plugin.deep_research_init_campaign({}))
    assert missing_target["ok"] is False

    campaign_root = tmp_path / "scaffolded-campaign"
    init_res = json.loads(
        plugin.deep_research_init_campaign({
            "target": "Evaluate local verification gates",
            "report_purpose": "factual",
            "target_type": "workflow",
            "campaign_root": str(campaign_root),
            "deliverable_export": False,
        })
    )
    assert init_res["ok"] is True, init_res
    assert Path(init_res["manifest_path"]).is_file()
    assert init_res["report_purpose"] == "factual"
    assert init_res["lane_count"] >= 5

    check_res = json.loads(
        plugin.deep_research_check_completion({
            "campaign_root": str(campaign_root),
            "offline": True,
            "write_receipt": True,
        })
    )
    assert check_res["status"] in {"HOLD", "FAIL"}
    assert (campaign_root / "receipts" / "completion.json").is_file()

    missing_root = json.loads(
        plugin.deep_research_check_completion({"campaign_root": str(tmp_path / "does-not-exist")})
    )
    assert missing_root["ok"] is False


def test_slash_command_subcommands(tmp_path: Path, monkeypatch) -> None:
    plugin = load_plugin_module()
    monkeypatch.setenv("RESEARCH_ARCHIVE_ROOT", str(tmp_path / "archive"))

    help_out = plugin.handle_deep_research_command("help")
    assert "/deep-research" in help_out

    preflight_out = plugin.handle_deep_research_command("preflight deepest Example question")
    assert "Deep Research Preflight (deepest mode)" in preflight_out
    assert "Estimated cost (USD):" in preflight_out

    init_out = plugin.handle_deep_research_command("init Slash command test campaign")
    assert "Scaffolded deep-research campaign at" in init_out

    created_dirs = list((tmp_path / "archive").iterdir())
    assert len(created_dirs) == 1
    check_out = plugin.handle_deep_research_command(f"check {created_dirs[0]}")
    assert "Campaign completion check:" in check_out

    unknown_out = plugin.handle_deep_research_command("nonexistent")
    assert "Unknown subcommand: nonexistent" in unknown_out


def test_no_script_opens_dotenv_and_secrets_use_secret_scope(tmp_path: Path, monkeypatch) -> None:
    py_files = [
        p for p in REPO_ROOT.rglob("*.py")
        if ".git" not in p.parts
        and "__pycache__" not in p.parts
        and not p.name.startswith("test_")
        and p.name != "conftest.py"
    ]
    assert py_files, "Expected runtime Python scripts in repository"
    for path in py_files:
        source = path.read_text(encoding="utf-8")
        assert ".hermes/.env" not in source, f"{path} still references .hermes/.env"
        assert "read_secrets_file" not in source

    # Trap ~/.hermes/.env in a fake HOME so any direct file read would return 'trap-dotenv-value'.
    fake_home = tmp_path / "home"
    dotenv_dir = fake_home / ".hermes"
    dotenv_dir.mkdir(parents=True)
    (dotenv_dir / ".env").write_text(
        "OPENROUTER_API_KEY=trap-dotenv-value\nAPIFY_TOKEN=trap-dotenv-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("APIFY_TOKEN", raising=False)

    mc = load_script_module("model_client")
    ri = load_script_module("receipt_integrity")
    apify = load_script_module("acquire_youtube_apify")

    # 1. With secret_scope returning None and env unset, must NOT read ~/.hermes/.env
    fake_agent = types.ModuleType("agent")
    fake_scope = types.ModuleType("agent.secret_scope")
    fake_scope.get_secret = lambda name, default=None: None  # type: ignore[attr-defined]
    fake_agent.secret_scope = fake_scope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_scope)

    with pytest.raises(RuntimeError):
        mc._api_key()
    with pytest.raises(RuntimeError):
        ri._api_key()
    assert apify._apify_token() is None

    # 2. With secret_scope returning a scoped credential, it is used first
    fake_scope.get_secret = lambda name, default=None: f"scoped-{name.lower()}"  # type: ignore[attr-defined]
    assert mc._api_key() == "scoped-openrouter_api_key"
    assert ri._api_key() == "scoped-openrouter_api_key"
    assert apify._apify_token() == "scoped-apify_token"


def test_complete_campaign_has_no_sys_path_mutation_and_docs_drop_private_checkout() -> None:
    cc_source = (SCRIPT_DIR / "complete_campaign.py").read_text(encoding="utf-8")
    tree = ast.parse(cc_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in {"insert", "append", "extend"}:
                val = node.func.value
                assert not (
                    isinstance(val, ast.Attribute)
                    and val.attr == "path"
                    and isinstance(val.value, ast.Name)
                    and val.value.id == "sys"
                ), "complete_campaign.py must not mutate sys.path"

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    skill_md = (REPO_ROOT / "skills" / "deep-research" / "SKILL.md").read_text(encoding="utf-8")
    assert "~/source-intelligence" not in readme
    assert "~/source-intelligence" not in skill_md
    assert "$HOME/source-intelligence" not in skill_md
