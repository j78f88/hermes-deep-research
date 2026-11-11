#!/usr/bin/env python3
"""Run one model phase as a fresh one-shot Hermes session, with one resume on a step stop (DR-897).

The run driver launches this file inside a systemd-run --user unit (or a detached process in tests).
It builds:
  hermes chat --oneshot -Q --ignore-rules --max-turns <budget> -t <toolsets> --query-file <abs packet>
              --in <root> --provider <p> -m <model> [--reasoning <r>] [--run-budget <s>]
It never passes --source. On a step-limit stop it resumes once with only --resume <id>, --query-file and
--in, using launch_controller.run_with_one_resume. A second step stop is recorded, not resumed.

The record goes to --record and a marker file (--marker) is always written last, holding the exit
code, so the driver can wait on every phase with one blocking wait_for_jobs call.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

SCHEMA = "deep-research-phase-run-v1"


def build_command(*, hermes: str, packet: Path, root: Path, budget: int, toolsets: str, provider: str,
                  model: str, reasoning: str | None = None, run_budget: int | None = None) -> list[str]:
    cmd = [hermes, "chat", "--oneshot", "-Q", "--ignore-rules", "--max-turns", str(budget), "-t", toolsets,
           "--query-file", str(packet), "--in", str(root), "--provider", provider, "-m", model]
    if reasoning:
        cmd += ["--reasoning", reasoning]
    if run_budget:
        cmd += ["--run-budget", str(run_budget)]
    return cmd


def run(args: argparse.Namespace) -> int:
    import launch_controller as lc

    root = Path(args.root).resolve()
    packet = Path(args.packet).resolve()
    record_path = Path(args.record).resolve()
    log_dir = Path(args.log_dir).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)
    hermes = str(Path(args.hermes_bin).expanduser())
    agent_log = Path(args.agent_log).expanduser() if args.agent_log else lc.default_agent_log()
    cmd = build_command(hermes=hermes, packet=packet, root=root, budget=args.budget, toolsets=args.toolsets,
                        provider=args.provider, model=args.model, reasoning=args.reasoning,
                        run_budget=args.run_budget)
    log1 = log_dir / f"{args.name}-run-1.log"
    record = {
        "schema": SCHEMA,
        "phase": args.name,
        "budget": args.budget,
        "model": args.model,
        "toolsets": args.toolsets,
        "command": cmd,
        "packet": str(packet),
        "root": str(root),
        "started_at": lc.now(),
        "log": str(log1),
        "session_id": None,
        "exit_code": None,
        "resume_count": 0,
        "status": "starting",
        "runs": [],
    }
    first = lc.run_once("start", cmd, log1, agent_log, record, record_path)
    if first["stopped_for_steps"]:
        record["status"] = "step_limit_reached"
    else:
        record["status"] = "completed" if first["exit_code"] == 0 else "failed"
    record["ended_at"] = lc.now()
    lc.write_json(record_path, record)
    return 0 if record["status"] == "completed" else (record["exit_code"] or 1)


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--marker", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--root", required=True)
    p.add_argument("--packet", required=True)
    p.add_argument("--record", required=True)
    p.add_argument("--log-dir", required=True)
    p.add_argument("--budget", type=int, required=True)
    p.add_argument("--toolsets", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--provider", default="openrouter")
    p.add_argument("--reasoning")
    p.add_argument("--run-budget", type=int)
    p.add_argument("--hermes-bin", default="~/.local/bin/hermes")
    p.add_argument("--agent-log")
    return p.parse_args(argv)


def _marker_from_argv(argv: list[str]) -> Path | None:
    for i, item in enumerate(argv):
        if item == "--marker" and i + 1 < len(argv):
            return Path(argv[i + 1])
        if item.startswith("--marker="):
            return Path(item.split("=", 1)[1])
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    marker = _marker_from_argv(argv)
    code = 1
    error = None
    try:
        code = run(parse(argv))
    except SystemExit as exc:  # argparse errors still leave a marker
        code = exc.code if isinstance(exc.code, int) else 2
        error = "bad arguments"
    except Exception as exc:  # noqa: BLE001 - the marker must always be written
        code = 1
        error = f"{type(exc).__name__}: {exc}"
    finally:
        if marker is not None:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(json.dumps({"exit_code": code, "error": error}) + "\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
