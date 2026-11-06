"""Completion pins executable repository dependencies, not the external runtime."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

HERE = Path(__file__).resolve().parent


def load(path: Path):
    spec = importlib.util.spec_from_file_location("completion_dependency_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(root: Path, *args: str):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(root: Path):
    git(root, "add", "--all")
    git(root, "-c", "user.name=Checker Test", "-c", "user.email=checker@test.invalid", "commit", "-qm", "fixture")


@pytest.fixture
def checker_repo(tmp_path):
    root = tmp_path / "repo"
    scripts = root / "research/deep-research/scripts"
    scripts.mkdir(parents=True)
    ignore = HERE.parent / '.gitignore'
    if ignore.exists():
        (scripts.parent / '.gitignore').write_bytes(ignore.read_bytes())
    (scripts / "complete_campaign.py").write_bytes((HERE / "complete_campaign.py").read_bytes())
    cc = load(scripts / "complete_campaign.py")
    for name in cc.CHECKER_FILES[1:]:
        (scripts / name).write_text("# Side-effect-free fixture checker.\n")
    git(root, "init", "-q")
    commit(root)
    return root, scripts, cc


def dynamic_helper(scripts: Path):
    helper = scripts.parents[2] / "productivity/plain-prose/scripts/helper.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("VALUE = 42\n")
    (scripts / "consumer.py").write_text(
        "import importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('detached_helper', {str(helper)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "del module\n"
    )
    return helper


def test_clean_dependency_inventory_includes_detached_cross_skill_helper(checker_repo):
    root, scripts, cc = checker_repo
    helper = dynamic_helper(scripts)
    commit(root)
    cc._load("consumer")
    assert "detached_helper" not in sys.modules
    result = cc.check_checker_code()
    assert result["verdict"] == "PASS", result
    assert result["dependencies_sha256"][helper.relative_to(root).as_posix()] == hashlib.sha256(helper.read_bytes()).hexdigest()
    assert result["dependencies_sha256"]["research/deep-research/scripts/consumer.py"]


@pytest.mark.parametrize("change", ["dirty", "deleted", "untracked", "assume_unchanged", "staged"])
def test_loaded_cross_skill_dependency_must_match_head(checker_repo, change):
    root, scripts, cc = checker_repo
    helper = dynamic_helper(scripts)
    if change != "untracked":
        commit(root)
    cc._load("consumer")
    if change == "deleted":
        helper.unlink()
    else:
        if change == "assume_unchanged":
            git(root, "update-index", "--assume-unchanged", str(helper.relative_to(root)))
        helper.write_text("VALUE = 'changed'\n")
        if change == "staged":
            git(root, "add", str(helper.relative_to(root)))
    result = cc.check_checker_code()
    assert result["verdict"] == "HOLD", result
    assert result["class"] in {"checker_modified", "checker_unpinned"}
    assert helper.name in " ".join(result["reasons"])


def test_sys_modules_repository_dependency_is_pinned(checker_repo, monkeypatch):
    root, scripts, cc = checker_repo
    helper = root / "other-skill/library.py"
    helper.parent.mkdir()
    helper.write_text("VALUE = 1\n")
    commit(root)
    module = ModuleType("loaded_repo_helper")
    module.__file__ = str(helper)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    helper.write_text("VALUE = 2\n")
    result = cc.check_checker_code()
    assert (result["verdict"], result["class"]) == ("HOLD", "checker_modified"), result


@pytest.mark.parametrize('inventory', ['sys_modules', 'verifier_child'])
def test_loaded_repository_bytecode_is_not_hidden_by_source_pinning(checker_repo, monkeypatch, inventory):
    root, scripts, cc = checker_repo
    helper = root / 'other-skill/library.py'
    helper.parent.mkdir()
    helper.write_text('VALUE = 1\n')
    cached = helper.parent / '__pycache__/library.cpython-313.pyc'
    cached.parent.mkdir()
    cached.write_bytes(b'committed bytecode fixture')
    commit(root)
    if inventory == 'sys_modules':
        module = ModuleType('loaded_cached_helper')
        module.__file__ = str(helper)
        vars(module)['__cached__'] = str(cached)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    else:
        envelope = {'verifier_stdout': json.dumps({'status': 'PASS'}), 'dependencies': [str(cached)]}
        with monkeypatch.context() as scoped:
            scoped.setattr(cc.subprocess, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps(envelope), ''))
            assert cc.check_verifier(root)['verdict'] == 'PASS'
    cached.write_bytes(b'altered bytecode fixture')
    result = cc.check_checker_code()
    assert (result['verdict'], result['class']) == ('HOLD', 'checker_modified'), result
    assert cached.relative_to(root).as_posix() in result['dependencies_sha256']


def test_deleted_seed_checker_is_not_silently_omitted(checker_repo):
    _, scripts, cc = checker_repo
    (scripts / "quote_check.py").unlink()
    result = cc.check_checker_code()
    assert result["verdict"] == "HOLD", result
    assert "quote_check.py" in " ".join(result["reasons"])


def test_dependency_symlink_cannot_escape_repository(checker_repo, tmp_path):
    root, scripts, cc = checker_repo
    helper = dynamic_helper(scripts)
    commit(root)
    cc._load("consumer")
    outside = tmp_path / "outside.py"
    outside.write_bytes(helper.read_bytes())
    helper.unlink()
    helper.symlink_to(outside)
    result = cc.check_checker_code()
    assert result["verdict"] == "HOLD", result
    assert helper.name in " ".join(result["reasons"])


def test_external_runtime_is_recorded_not_verified(checker_repo, tmp_path, monkeypatch):
    _, _, cc = checker_repo
    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "run_agent.py").write_text("# Never import this runtime.\nraise AssertionError('runtime executed')\n")
    git(runtime, "init", "-q")
    commit(runtime)
    revision = git(runtime, "rev-parse", "HEAD")
    (runtime / "run_agent.py").write_text("# Dirty external code is outside checker verification.\n")
    monkeypatch.setenv("HERMES_AGENT_DIR", str(runtime))
    result = cc.check_checker_code()
    assert result["verdict"] == "PASS", result
    assert result["external_runtime"]["commit"] == revision
    assert result["external_runtime"]["verified"] is False
    assert result["external_runtime"]["status"] == "recorded_only"
    assert not any("run_agent.py" in rel for rel in result["dependencies_sha256"])


def test_verifier_subprocess_reports_unregistered_dependencies(checker_repo):
    root, scripts, cc = checker_repo
    helper = dynamic_helper(scripts)
    (scripts / "verify_campaign_depth.py").write_text(
        "import importlib.util, json\n"
        f"spec = importlib.util.spec_from_file_location('child_helper', {str(helper)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(json.dumps({'status': 'PASS', 'classification': 'ok'}))\n"
    )
    commit(root)
    assert cc.check_verifier(root)["verdict"] == "PASS"
    helper.write_text("VALUE = 'modified after child load'\n")
    result = cc.check_checker_code()
    assert (result["verdict"], result["class"]) == ("HOLD", "checker_modified"), result
    assert helper.name in " ".join(result["reasons"])


def test_verifier_subprocess_does_not_write_bytecode(checker_repo):
    root, scripts, cc = checker_repo
    helper = dynamic_helper(scripts)
    (scripts / "verify_campaign_depth.py").write_text(
        "import importlib.util, json\n"
        f"spec = importlib.util.spec_from_file_location('child_helper', {str(helper)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "print(json.dumps({'status': 'PASS', 'classification': 'ok'}))\n"
    )
    commit(root)
    assert cc.check_verifier(root)["verdict"] == "PASS"
    assert not list(root.rglob("*.pyc")), "read-only completion child wrote repository bytecode"


def test_dependency_collection_restores_callers_profiler(checker_repo):
    _, scripts, cc = checker_repo
    dynamic_helper(scripts)
    previous = sys.getprofile()
    def observer(frame, event, arg):
        pass
    try:
        sys.setprofile(observer)
        cc._load("consumer")
        assert sys.getprofile() is observer
    finally:
        sys.setprofile(previous)


def isolate_completion(cc, monkeypatch):
    for name in ("check_verifier", "check_receipts", "check_web_search", "check_quotes"):
        monkeypatch.setattr(cc, name, lambda *a, _name=name, **kw: cc.step(_name, "PASS", "ok", []))


@pytest.mark.parametrize("allowed,status", [(True, "PASS"), (False, "BLOCKED_FACTUAL_FINDINGS"), (True, "BLOCKED_STALE_DECISIONS"), (False, "PASS")])
def test_completion_uses_shared_readiness_gate_for_optional_legacy_brief(tmp_path, monkeypatch, allowed, status):
    cc = load(HERE / "complete_campaign.py")
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "manifest.json").write_text(json.dumps({"plain_brief": {"required": False}}))
    isolate_completion(cc, monkeypatch)
    calls = []
    shared = ModuleType("research_qa_repair")
    def readiness(path, *, require_brief=True):
        calls.append((path, require_brief))
        return {"status": status, "publication_qa_allowed": allowed, "findings": [] if allowed and status == "PASS" else ["unresolved pack finding"]}
    shared.campaign_readiness = readiness
    original = cc._load
    monkeypatch.setattr(cc, "_load", lambda name: shared if name == "research_qa_repair" else original(name))
    result = cc.evaluate(root, online=False, homes=[], pin_checker=False)
    assert calls == [(root.resolve(), True)]
    assert result["status"] == ("PASS" if allowed and status == "PASS" else "HOLD"), result
    gate = next(s for s in result["steps"] if s["step"] == "readiness")
    assert gate["readiness_status"] == status
    if not allowed or status != "PASS":
        assert "unresolved pack finding" in gate["reasons"]


def test_completion_cannot_pass_when_readiness_api_is_missing(tmp_path, monkeypatch):
    cc = load(HERE / "complete_campaign.py")
    (tmp_path / "manifest.json").write_text("{}")
    isolate_completion(cc, monkeypatch)
    original = cc._load
    monkeypatch.setattr(cc, "_load", lambda name: ModuleType(name) if name == "research_qa_repair" else original(name))
    result = cc.evaluate(tmp_path, online=False, homes=[], pin_checker=False)
    assert result["status"] != "PASS", result
    assert "readiness_unavailable" in result["classes"]


def test_checker_pinning_runs_after_dependencies_are_loaded(tmp_path, monkeypatch):
    cc = load(HERE / "complete_campaign.py")
    (tmp_path / "manifest.json").write_text("{}")
    isolate_completion(cc, monkeypatch)
    loaded = []
    shared = ModuleType("research_qa_repair")
    shared.campaign_readiness = lambda *a, **kw: {"status": "PASS", "publication_qa_allowed": True, "findings": []}
    def load_shared(name):
        loaded.append(name)
        return shared
    monkeypatch.setattr(cc, "_load", load_shared)
    def pin():
        assert "research_qa_repair" in loaded, "pinning ran before readiness dependencies loaded"
        return cc.step("checker_code", "PASS", "ok", [])
    monkeypatch.setattr(cc, "check_checker_code", pin)
    assert cc.evaluate(tmp_path, online=False, homes=[])["status"] == "PASS"


def test_external_runtime_unset_is_recorded_as_not_configured(checker_repo, monkeypatch):
    _, _, cc = checker_repo
    monkeypatch.delenv("HERMES_AGENT_DIR", raising=False)
    result = cc.check_checker_code()
    assert result["verdict"] == "PASS", result
    assert result["external_runtime"]["status"] == "not_configured"
    assert result["external_runtime"]["verified"] is False
