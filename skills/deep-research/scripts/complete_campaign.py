#!/usr/bin/env python3
"""Completion check for a deep-research campaign, run outside the session that wrote it.

A campaign is done only when this script prints ``PASS``. Nothing a writer puts in the campaign folder
can make it pass on its own, because every step recomputes from evidence the writer does not control:

* ``verify_campaign_depth.py`` runs in a fresh interpreter (``-E -s``: no PYTHON* variables, no user site) with a clean environment.
* Review records: forged shapes FAIL ``receipt_fabricated``; the model calls a plain-brief review names
  are confirmed at the provider (they must exist, be served by the declared model, and the editor call
  must have read the brief and pack).
* Web search: the Hermes session records for the run decide whether search worked; if it did not, the
  brief must say so (FAIL ``web_search_missing_undeclared``).
* Saved quotes: every cited lane finding must quote a saved source whose hash matches its
  index (FAIL ``quote_check_failed``).
* Checker code: the scripts used must match the committed skill (HOLD ``checker_modified``), so a writer
  cannot edit the check to pass.

When it is started from inside a Hermes session, it relaunches itself with ``systemd-run --user`` so the
check runs as its own process, outside the agent's process tree and environment. The record it writes
(``receipts/completion.json``) is a report, not proof: anyone who needs the answer runs this again.

Usage:
  python3 complete_campaign.py --campaign-root ROOT [--offline] [--probe] [--session-export F.jsonl]
Exit codes: 0 PASS, 2 HOLD, 3 FAIL.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import marshal
import os
import runpy
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# A read-only checker must not manufacture uncommitted executable caches on normal
# invocation. Existing caches still load and remain pinned by _remember_cached.
sys.dont_write_bytecode = True

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA = "deep-research-completion-v1"
# Mandatory entry points, not an allowlist: observed imports extend this inventory.
CHECKER_FILES = ["complete_campaign.py", "verify_campaign_depth.py", "plain_brief_rewrite.py", "receipt_integrity.py",
                 "web_search_status.py", "quote_check.py", "export_deliverables.py"]
CLEAN_ENV_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "TZ", "OPENROUTER_API_KEY")
STATUS_RANK = {"PASS": 0, "HOLD": 1, "FAIL": 2}
# Keep lexical paths: resolving a dependency symlink would hide its repository entry.
_DEPENDENCY_PATHS: set[str] = set()
_LOADED_CODE: dict[str, set[str]] = {}


def _code_sha(code) -> str:
    # marshal's reference/intern flags depend on object identity and refcounts.
    # Fingerprint every executable/debug field and typed constant instead, so a
    # fresh compile and the same code recovered from a pyc compare identically.
    fields = ('co_argcount', 'co_posonlyargcount', 'co_kwonlyargcount', 'co_nlocals',
              'co_stacksize', 'co_flags', 'co_code', 'co_consts', 'co_names', 'co_varnames',
              'co_filename', 'co_name', 'co_qualname', 'co_firstlineno', 'co_linetable',
              'co_exceptiontable', 'co_freevars', 'co_cellvars')
    def freeze(value):
        if isinstance(value, type(__checker_module_code__)):
            return ['code', [freeze(getattr(value, name, None)) for name in fields]]
        if isinstance(value, (tuple, frozenset)):
            items = [freeze(item) for item in value]
            return [type(value).__name__, sorted(items, key=repr) if isinstance(value, frozenset) else items]
        return [type(value).__name__, repr(value)]
    return hashlib.sha256(json.dumps(freeze(code), ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def _remember_code(code) -> None:
    if isinstance(code, type(__checker_module_code__)) and not code.co_filename.startswith('<'):
        path = os.path.abspath(code.co_filename)
        _remember_dependency(path)
        _LOADED_CODE.setdefault(path, set()).add(_code_sha(code))


def _remember_dependency(filename) -> None:
    if isinstance(filename, str) and filename and not filename.startswith("<"):
        if filename.endswith((".pyc", ".pyo")):
            _DEPENDENCY_PATHS.add(os.path.abspath(filename))
            try:
                filename = importlib.util.source_from_cache(filename)
            except ValueError:
                pass
        _DEPENDENCY_PATHS.add(os.path.abspath(filename))


def _remember_cached(filename) -> None:
    # -B prevents writes, not reads. A tracked/tampered pyc can execute while its
    # source still matches HEAD, so retain the bytecode's lexical path as well.
    if isinstance(filename, str) and filename and Path(filename).is_file():
        _remember_dependency(filename)


def _remember_modules() -> None:
    for module in list(sys.modules.values()):
        if module is not None:
            _remember_dependency(vars(module).get("__file__"))
            _remember_cached(vars(module).get("__cached__"))
            _remember_code(vars(module).get("__checker_module_code__"))


@contextlib.contextmanager
def _collect_dependencies():
    """Observe executed code as well as registered imports; never import code to inventory it.

    importlib.exec_module callers need not register their modules in sys.modules, and
    may discard them immediately. A scoped profile sees those module code objects too.
    Restore the caller's profiler rather than leaving a process-wide hook installed.
    """
    previous = sys.getprofile()
    def observe(frame, event, arg):
        if event == "call":
            _remember_dependency(frame.f_code.co_filename)
            # Settings/style files are executable policy inputs too. Observe the
            # actual Path reads, including detached dynamic imports and children;
            # external campaign/runtime files are excluded by the repository boundary.
            if frame.f_code.co_name in {"read_text", "read_bytes"}:
                source = frame.f_locals.get("self")
                if isinstance(source, Path):
                    _remember_dependency(os.fspath(source))
            if frame.f_code.co_name == "<module>":
                _remember_code(frame.f_code)
                _remember_cached(frame.f_globals.get("__cached__"))
    _remember_modules()
    sys.setprofile(observe)
    try:
        yield
    finally:
        sys.setprofile(previous)
        _remember_modules()


def _load(name: str):
    path = SCRIPT_DIR / f"{name}.py"
    _remember_dependency(str(path))
    with _collect_dependencies():
        if __package__:
            return importlib.import_module(f".{name}", __package__)
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else None


def clean_env() -> dict:
    return {k: os.environ[k] for k in CLEAN_ENV_KEYS if k in os.environ}


def pack_path(manifest: dict) -> str:
    """The pack the plain brief is bound to: the reference pack in layout v2 (first internal output), else the full pack."""
    if isinstance(manifest, dict) and manifest.get("output_layout") == "brief-and-reference-v2":
        internals = manifest.get("internal_outputs")
        first = internals[0] if isinstance(internals, list) and internals else None
        rel = str(first.get("path") if isinstance(first, dict) else first or "").strip().replace("\\", "/")
        return rel or "internal/reference-pack.md"
    return "internal/full-research-pack.md"


def step(name: str, verdict: str, klass: str, reasons: list[str], **detail) -> dict:
    return {"step": name, "verdict": verdict, "class": klass, "reasons": reasons, **detail}


def _verifier_subprocess(root: str) -> None:
    """Run the unchanged verifier CLI and return its output plus observed dependencies."""
    campaign_root = str(Path(root).resolve())
    sys.argv = [str(SCRIPT_DIR / "verify_campaign_depth.py"), "--campaign-root", campaign_root,
                "--mode", "true_deep_research", "--json"]
    output = io.StringIO()
    code = 0
    with contextlib.chdir(SCRIPT_DIR), _collect_dependencies(), contextlib.redirect_stdout(output):
        try:
            runpy.run_path(sys.argv[0], run_name="__main__")
        except SystemExit as exc:
            code = exc.code
    print(json.dumps({"verifier_stdout": output.getvalue(), "dependencies": sorted(_DEPENDENCY_PATHS),
                      "loaded_code_sha256": {path: sorted(hashes) for path, hashes in _LOADED_CODE.items()}}))
    raise SystemExit(code)


def check_verifier(root: Path) -> dict:
    launcher = ("import runpy, sys; ns = runpy.run_path(sys.argv[1]); "
                "ns['_verifier_subprocess'](sys.argv[2])")
    proc = subprocess.run([sys.executable, "-B", "-E", "-s", "-c", launcher, str(SCRIPT_DIR / "complete_campaign.py"), str(Path(root).resolve())],
                          cwd=str(SCRIPT_DIR), capture_output=True, text=True, env=clean_env(), timeout=600)
    try:
        envelope = json.loads(proc.stdout)
        dependencies = envelope["dependencies"]
        if not isinstance(dependencies, list) or not all(isinstance(p, str) for p in dependencies):
            raise ValueError("invalid verifier dependency inventory")
        for path in dependencies:
            _remember_dependency(path)
        loaded = envelope.get('loaded_code_sha256', {})
        if not isinstance(loaded, dict) or any(not isinstance(path, str) or not isinstance(hashes, list)
                or not all(isinstance(value, str) and len(value) == 64 for value in hashes)
                for path, hashes in loaded.items()):
            raise ValueError('invalid verifier loaded-code inventory')
        for path, hashes in loaded.items():
            _remember_dependency(path)
            _LOADED_CODE.setdefault(os.path.abspath(path), set()).update(hashes)
        result = json.loads(envelope["verifier_stdout"])
    except (ValueError, KeyError, TypeError):
        return step("verifier", "FAIL", "verifier_crashed", [proc.stderr[-400:] or proc.stdout[-400:]])
    verdict = result.get("status", "FAIL")
    reasons = list(result.get("failures") or []) + list(result.get("holds") or [])
    return step("verifier", verdict if verdict in STATUS_RANK else "FAIL", result.get("classification", "unknown"), reasons[:40],
                holds=len(result.get("holds") or []), failures=len(result.get("failures") or []))


def check_readiness(root: Path) -> dict:
    """Require the shared pack/brief acceptance gate even for optional legacy briefs."""
    try:
        result = _load("research_qa_repair").campaign_readiness(root, require_brief=True)
        if (not isinstance(result, dict) or not isinstance(result.get("status"), str)
                or not isinstance(result.get("publication_qa_allowed"), bool)
                or not isinstance(result.get("findings"), list)):
            raise ValueError("invalid campaign_readiness result")
    except Exception as exc:  # noqa: BLE001 - a missing/broken gate cannot grant acceptance
        return step("readiness", "HOLD", "readiness_unavailable", [f"shared readiness check unavailable: {type(exc).__name__}: {exc}"])
    allowed = result["status"] == "PASS" and result["publication_qa_allowed"] is True
    reasons = [str(f) for f in result["findings"]]
    if not allowed and not reasons:
        reasons = [f"shared readiness blocked: {result['status']}"]
    return step("readiness", "PASS" if allowed else "HOLD", "ok" if allowed else "campaign_not_ready", reasons,
                readiness_status=result["status"], publication_qa_allowed=result["publication_qa_allowed"])


def check_receipts(root: Path, manifest: dict, *, online: bool, fetch=None) -> dict:
    ri = _load("receipt_integrity")
    swept = ri.sweep_qa_dir(root)
    if swept:
        return step("receipts", "FAIL", "receipt_fabricated", swept)
    result = ri.verify_campaign_provenance(root, manifest, online=online, fetch=fetch)
    return step("receipts", result["verdict"], result["class"], result["reasons"], provider_calls=result.get("calls", []))


def check_web_search(root: Path, *, probe: bool, exports: list[Path], homes: list[Path] | None = None) -> dict:
    result = _load("web_search_status").evaluate(root, run_probe=probe, session_exports=exports, homes=homes)
    return step("web_search", result["verdict"], result["class"], result["reasons"],
                status=result["status"], declared_in_brief=result["declared_in_brief"], sessions_checked=result["sessions_checked"],
                successes=result["successes"], failures=result["failures"], failure_examples=result["failure_examples"][:3])


def check_quotes(root: Path) -> dict:
    if not (SCRIPT_DIR / "quote_check.py").is_file():
        return step("quotes", "HOLD", "quote_check_unavailable", ["quote_check.py is missing from the skill"])
    result = _load("quote_check").evaluate(root)
    return step("quotes", result["verdict"], result["class"], result["reasons"][:40], **{k: result[k] for k in ("findings", "quoted", "lanes") if k in result})


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, env=clean_env(), timeout=30)


def _external_runtime_revision() -> dict:
    """Record only the separate runtime HEAD; neither import nor attest its code."""
    configured = os.environ.get("HERMES_AGENT_DIR")
    if not configured:
        return {"path": None, "commit": None, "status": "not_configured", "verified": False,
                "note": "Set HERMES_AGENT_DIR to record the external Hermes runtime revision."}
    runtime = Path(configured).expanduser()
    revision = _git(runtime, "rev-parse", "HEAD")
    return {"path": str(runtime), "commit": revision.stdout.decode().strip() if revision.returncode == 0 else None,
            "status": "recorded_only", "verified": False,
            "note": "External Hermes runtime revision only; its working tree and code are not verified by this checker."}


def _derived_cache_matches(path: Path, current: bytes, top: Path, head: str) -> bool:
    """Attest an untracked CPython cache, never import or execute it to check it.

    Source is still independently pinned byte-for-byte below. A valid header is
    insufficient: both the cache body and the module code actually executed must
    equal a fresh compilation of the exact committed source in this interpreter.
    Tracked caches do not use this exception: their exact HEAD bytes remain bound.
    """
    try:
        source = Path(importlib.util.source_from_cache(str(path)))
        rel = source.relative_to(top).as_posix()
        if (str(path) != importlib.util.cache_from_source(str(source)) or not source.is_file()
                or any(part.is_symlink() for part in (source, *source.parents)
                       if part != top and top in part.parents)):
            return False
        committed = _git(top, 'show', f'{head}:{rel}')
        if committed.returncode != 0 or source.read_bytes() != committed.stdout:
            return False
        if len(current) < 16 or current[:4] != importlib.util.MAGIC_NUMBER:
            return False
        flags = int.from_bytes(current[4:8], 'little')
        if flags == 0:
            stat = source.stat()
            if current[8:16] != ((int(stat.st_mtime) & 0xffffffff).to_bytes(4, 'little')
                                + (stat.st_size & 0xffffffff).to_bytes(4, 'little')):
                return False
        elif flags in (1, 3):
            if current[8:16] != importlib.util.source_hash(committed.stdout):
                return False
        else:
            return False
        stream = io.BytesIO(current[16:])
        code = marshal.load(stream)
        if not isinstance(code, type(__checker_module_code__)) or stream.read():
            return False
        expected = _code_sha(compile(committed.stdout, str(source), 'exec', dont_inherit=True))
        observed = _LOADED_CODE.get(str(source), set())
        return bool(observed) and observed == {expected} and _code_sha(code) == expected
    except (OSError, ValueError, TypeError, EOFError, SyntaxError):
        return False


def check_checker_code() -> dict:
    """Pin every observed repository dependency to HEAD bytes, not just git status.

    This includes cross-skill imports and the child verifier inventory. Mandatory
    entry points remain in scope even if deleted. Never execute extra modules just
    to discover dependencies, or count external runtime code as verified.
    """
    try:
        runtime = _external_runtime_revision()
        repo = _git(SCRIPT_DIR, "rev-parse", "--show-toplevel")
        if repo.returncode != 0:
            return step("checker_code", "HOLD", "checker_unpinned", ["the skill is not a git checkout; cannot pin the checker code"],
                        external_runtime=runtime)
        top = Path(os.fsdecode(repo.stdout).strip()).resolve()
        revision = _git(top, "rev-parse", "HEAD")
        if revision.returncode != 0:
            return step("checker_code", "HOLD", "checker_unpinned", ["the skill has no committed checker revision"], external_runtime=runtime)
        head = revision.stdout.decode().strip()
        _remember_code(__checker_module_code__)
        _remember_modules()
        paths = {SCRIPT_DIR / filename for filename in CHECKER_FILES}
        paths.update(Path(filename) for filename in _DEPENDENCY_PATHS)
        paths.add(Path(__file__).absolute())
        dependencies, problems, loaded = {}, [], {}
        for path in sorted(paths):
            try:
                rel = path.relative_to(top).as_posix()
            except ValueError:
                continue  # stdlib, site packages and the external runtime are not this repository
            if any(part.is_symlink() for part in (path, *path.parents) if part != top and top in part.parents):
                problems.append(f"{rel}: symlinked checker dependency")
                continue
            if not path.is_file():
                problems.append(f"{rel}: checker dependency missing")
                continue
            current = path.read_bytes()
            dependencies[rel] = hashlib.sha256(current).hexdigest()
            committed = _git(top, "show", f"{head}:{rel}")
            if committed.returncode != 0:
                if not (path.suffix == '.pyc' and _derived_cache_matches(path, current, top, head)):
                    problems.append(f"{rel}: checker dependency is not committed at HEAD or attested derived bytecode")
            elif current != committed.stdout:
                problems.append(f"{rel}: checker dependency differs from HEAD")
            observed = _LOADED_CODE.get(str(path))
            if observed:
                loaded[rel] = sorted(observed)
                if committed.returncode == 0 and path.suffix == '.py':
                    try:
                        expected = _code_sha(compile(committed.stdout, str(path), 'exec', dont_inherit=True))
                    except (ValueError, TypeError, SyntaxError):
                        expected = None
                    if observed != {expected}:
                        problems.append(f"{rel}: loaded executable differs from HEAD")
        return step("checker_code", "HOLD" if problems else "PASS", "checker_modified" if problems else "ok", problems,
                    commit=head, dependencies_sha256=dependencies, loaded_code_sha256=loaded, external_runtime=runtime)
    except (OSError, subprocess.SubprocessError) as exc:
        return step("checker_code", "HOLD", "checker_unpinned", [f"cannot pin checker dependencies: {type(exc).__name__}: {exc}"])


def evaluate(root: Path, *, online: bool = True, probe: bool = False, exports: list[Path] | None = None,
             fetch=None, homes: list[Path] | None = None, pin_checker: bool = True) -> dict:
    """Run every step. ``fetch``, ``homes`` and ``pin_checker`` exist for tests; the CLI never sets them."""
    root = Path(root).expanduser().resolve()
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    with _collect_dependencies():
        steps = [check_readiness(root), check_verifier(root), check_receipts(root, manifest, online=online, fetch=fetch),
                 check_web_search(root, probe=probe, exports=exports or [], homes=homes), check_quotes(root)]
    # Pin after all checks have loaded their dependencies, including the child inventory.
    steps.insert(0, check_checker_code() if pin_checker else step("checker_code", "PASS", "not_checked", []))
    worst = max(steps, key=lambda s: STATUS_RANK.get(s["verdict"], 2))["verdict"]
    pb = manifest.get("plain_brief") if isinstance(manifest.get("plain_brief"), dict) else {}
    files = {rel: _sha(root / rel) for rel in sorted({pb.get("path") or "internal/plain-brief.md",
                                                      pb.get("review") or "qa/plain-brief-review.json",
                                                      pack_path(manifest), "manifest.json"})}
    return {
        "schema_version": SCHEMA,
        "status": worst,
        "classes": sorted({s["class"] for s in steps if s["verdict"] != "PASS"}),
        "steps": steps,
        "checked_files_sha256": files,
        "campaign_root": str(root),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "launched_via": os.environ.get("DEEP_RESEARCH_COMPLETION_LAUNCH", "direct"),
        "requested_by_session": os.environ.get("DEEP_RESEARCH_COMPLETION_REQUESTER") or os.environ.get("HERMES_SESSION_ID"),
        "online": online,
        "note": "Report only. Consumers re-run complete_campaign.py instead of trusting this file.",
    }


def relaunch_outside_session(argv: list[str]) -> int | None:
    """From inside a Hermes session, rerun as a transient systemd user unit with a clean environment."""
    if not os.environ.get("HERMES_SESSION_ID") or os.environ.get("DEEP_RESEARCH_COMPLETION_LAUNCH"):
        return None
    runner = shutil.which("systemd-run")
    if not runner:
        return None
    env_args = [f"--setenv={k}={os.environ[k]}" for k in CLEAN_ENV_KEYS if k in os.environ and k != "OPENROUTER_API_KEY"]
    env_args += ["--setenv=DEEP_RESEARCH_COMPLETION_LAUNCH=systemd-run",
                 f"--setenv=DEEP_RESEARCH_COMPLETION_REQUESTER={os.environ['HERMES_SESSION_ID']}"]
    cmd = [runner, "--user", "--wait", "--pipe", "--collect", "--quiet", f"--working-directory={Path.home()}", *env_args,
           sys.executable, "-E", "-s", str(Path(__file__).resolve()), *argv]
    return subprocess.run(cmd).returncode


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(description="Deep-research completion check")
    ap.add_argument("--campaign-root", required=True)
    ap.add_argument("--offline", action="store_true", help="skip the provider check (the result can then only HOLD)")
    ap.add_argument("--probe", action="store_true", help="also make one live web_search call")
    ap.add_argument("--session-export", action="append", default=[], help="session .jsonl export to include (replays)")
    ap.add_argument("--no-relaunch", action="store_true", help="run in this process even inside a Hermes session")
    ap.add_argument("--no-write", action="store_true", help="print only; do not write receipts/completion.json")
    args = ap.parse_args(argv)
    if not args.no_relaunch:
        code = relaunch_outside_session(argv)
        if code is not None:
            return code
    result = evaluate(Path(args.campaign_root), online=not args.offline, probe=args.probe,
                      exports=[Path(p) for p in args.session_export])
    if not args.no_write:
        out = Path(result["campaign_root"]) / "receipts" / "completion.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return {"PASS": 0, "HOLD": 2}.get(result["status"], 3)


if __name__ == "__main__":
    raise SystemExit(main())
