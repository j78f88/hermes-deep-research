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


@pytest.fixture()
def campaigns(tmp_path: Path, monkeypatch) -> Path:
    """Fake Hermes storage: plugins.plugin_storage.plugin_data_dir -> tmp_path/data/<name>."""
    data = tmp_path / "data"

    def plugin_data_dir(name: str) -> Path:
        path = data / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    pkg = types.ModuleType("plugins")
    pkg.__path__ = []  # type: ignore[attr-defined]
    storage = types.ModuleType("plugins.plugin_storage")
    storage.plugin_data_dir = plugin_data_dir  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "plugins", pkg)
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", storage)
    root = data / "hermes-deep-research" / "campaigns"
    root.mkdir(parents=True)
    return root.resolve()


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
    assert env_names == {"OPENROUTER_API_KEY"}
    optional_names = {entry["name"] for entry in manifest.get("optional_env", [])}
    assert optional_names == {"APIFY_TOKEN"}
    assert manifest["requires_hermes"] == ">=0.22.6"
    assert set(manifest["disclosures"]) == {"storage", "subprocesses", "network", "credentials"}

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


def test_init_and_check_completion_tool_handlers(tmp_path: Path, campaigns: Path) -> None:
    plugin = load_plugin_module()

    missing_target = json.loads(plugin.deep_research_init_campaign({}))
    assert missing_target["ok"] is False

    campaign_root = campaigns / "scaffolded-campaign"
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
        plugin.deep_research_check_completion({"campaign_root": str(campaigns / "does-not-exist")})
    )
    assert missing_root["ok"] is False
    assert missing_root["tool"] == "deep_research_check_completion"
    assert missing_root["error_type"] == "FileNotFoundError"


def test_slash_command_subcommands(tmp_path: Path, monkeypatch) -> None:
    plugin = load_plugin_module()
    # Standalone path: no Hermes storage module, so RESEARCH_ARCHIVE_ROOT is the confinement root.
    monkeypatch.setitem(sys.modules, "plugins.plugin_storage", None)
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


def test_secret_scope_miss_is_final_inside_hermes(monkeypatch) -> None:
    """Under the multiplexing gateway a scope miss must not leak another profile's os.environ key."""
    plugin = load_plugin_module()
    mc = load_script_module("model_client")
    ri = load_script_module("receipt_integrity")
    apify = load_script_module("acquire_youtube_apify")
    for name in ("OPENROUTER_API_KEY", "APIFY_TOKEN"):
        monkeypatch.setenv(name, "other-profile-value")

    fake_agent = types.ModuleType("agent")
    fake_scope = types.ModuleType("agent.secret_scope")
    fake_scope.get_secret = lambda name, default=None: None  # type: ignore[attr-defined]
    fake_agent.secret_scope = fake_scope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent", fake_agent)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_scope)

    assert plugin._get_secret("OPENROUTER_API_KEY") is None
    with pytest.raises(RuntimeError):
        mc._api_key()
    with pytest.raises(RuntimeError):
        ri._api_key()
    assert apify._apify_token() is None

    # Without Hermes importable, standalone use still reads the process environment.
    monkeypatch.setitem(sys.modules, "agent.secret_scope", None)
    assert plugin._get_secret("OPENROUTER_API_KEY") == "other-profile-value"
    assert mc._api_key() == "other-profile-value"
    assert ri._api_key() == "other-profile-value"
    assert apify._apify_token() == "other-profile-value"


def test_campaign_root_is_confined_to_plugin_storage(tmp_path: Path, campaigns: Path) -> None:
    plugin = load_plugin_module()
    outside = tmp_path / "outside-campaign"
    link = campaigns / "link-out"
    link.symlink_to(tmp_path, target_is_directory=True)

    for bad in (str(outside), "../escape", str(campaigns), ".", str(link / "x")):
        for tool, args in (
            (plugin.deep_research_init_campaign, {"target": "Confinement probe", "campaign_root": bad}),
            (plugin.deep_research_check_completion, {"campaign_root": bad}),
        ):
            payload = json.loads(tool(args))
            assert payload["ok"] is False, (bad, payload)
            assert "inside the plugin campaigns folder" in payload["error"], (bad, payload)
    assert not outside.exists()
    assert not (tmp_path / "x").exists()
    assert not (tmp_path.parent / "escape").exists()


def test_default_campaign_lands_in_plugin_storage_and_leaves_process_state_alone(tmp_path: Path, campaigns: Path) -> None:
    plugin = load_plugin_module()
    argv_before, cwd_before = list(sys.argv), os.getcwd()
    payload = json.loads(plugin.deep_research_init_campaign({"target": "Default folder check"}))
    assert payload["ok"] is True, payload
    assert Path(payload["campaign_root"]).parent == campaigns
    checked = json.loads(plugin.deep_research_check_completion({"campaign_root": payload["campaign_root"]}))
    assert checked["status"] in {"HOLD", "FAIL"}
    assert sys.argv == argv_before
    assert os.getcwd() == cwd_before


@pytest.mark.parametrize(
    "tool, args, key",
    [
        ("deep_research_init_campaign", {"target": "x", "deliverable_export": "false"}, "deliverable_export"),
        ("deep_research_check_completion", {"campaign_root": "c", "probe": "false"}, "probe"),
        ("deep_research_check_completion", {"campaign_root": "c", "offline": 0}, "offline"),
        ("deep_research_check_completion", {"campaign_root": "c", "write_receipt": "no"}, "write_receipt"),
        ("deep_research_preflight", {"mode": 5}, "mode"),
        ("deep_research_preflight", {"question": "q" * 2001}, "question"),
        ("deep_research_init_campaign", {"target": ["not", "text"]}, "target"),
    ],
)
def test_arguments_are_type_checked_not_coerced(campaigns: Path, tool: str, args: dict, key: str) -> None:
    plugin = load_plugin_module()
    payload = json.loads(getattr(plugin, tool)(args))
    assert payload["ok"] is False
    assert payload["tool"] == tool
    assert payload["error_type"] == "ValueError"
    assert key in payload["error"]
    assert not list(campaigns.iterdir())


def test_handlers_never_raise_and_truncate_errors(campaigns: Path, monkeypatch) -> None:
    plugin = load_plugin_module()
    for tool in (plugin.deep_research_preflight, plugin.deep_research_init_campaign, plugin.deep_research_check_completion):
        for bad in ("a string", 7, ["x"]):
            payload = json.loads(tool(bad, task_id="t", future_kwarg=True))
            assert payload["ok"] is False and payload["error_type"] == "ValueError"
    long_message = "x" * 5000
    monkeypatch.setattr(plugin, "_get_secret", lambda name: (_ for _ in ()).throw(RuntimeError(long_message)))
    payload = json.loads(plugin.deep_research_preflight({"mode": "fast"}))
    assert payload["ok"] is False and payload["error_type"] == "RuntimeError"
    assert len(payload["error"]) <= plugin._MAX_ERROR_CHARS + 3
    assert plugin.handle_deep_research_command("preflight").startswith("deep-research preflight error:")


def test_schemas_are_strict_and_register_is_inert(tmp_path: Path, monkeypatch) -> None:
    plugin = load_plugin_module()

    def forbidden(*args, **kwargs):
        raise AssertionError("registration attempted I/O")

    monkeypatch.setattr(plugin, "_default_campaigns_dir", forbidden)
    monkeypatch.setattr(plugin, "_get_secret", forbidden)
    monkeypatch.setattr("subprocess.run", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    ctx = FakePluginContext()
    plugin.register(ctx)
    assert len(ctx.tools) == 3
    for tool in ctx.tools:
        params = tool["schema"]["parameters"]
        assert params["additionalProperties"] is False
        assert set(params.get("required", [])) <= set(params["properties"])
        assert tool["schema"]["name"] == tool["name"]


def test_plugin_entry_has_no_dotenv_or_process_global_mutation() -> None:
    tree = ast.parse((REPO_ROOT / "__init__.py").read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    forbidden_attrs = {("sys", "argv"), ("sys", "path"), ("os", "chdir"), ("contextlib", "chdir")}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert (node.value.id, node.attr) not in forbidden_attrs, f"line {node.lineno}"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            assert ".env" not in node.value.replace("get_secret (os.environ only when Hermes is not importable; no direct .env reads)", ""), f"line {node.lineno}"
