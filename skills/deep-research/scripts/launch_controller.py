#!/usr/bin/env python3
"""Launch a deep-research controller with a high step limit and one automatic resume (DR-876).

Start command:
  hermes chat --oneshot -Q --ignore-rules --max-turns 500 --query-file <packet> --in <root> [passthrough]

How the launcher knows what happened (Hermes source, read-only):
- Session id: with -Q, the run prints "session_id: <id>" on stderr as its last line
  (hermes_cli/cli_single_query.py:322). The launcher merges stderr into the run log and reads it there.
- Step-limit stop: the run exits 1 and its reply is model-written text, so the exit code and reply cannot
  tell a step stop from another failure. The agent logs one line per turn end
  "Turn ended: reason=max_iterations_reached(N/M) ... session=<id>"
  (agent/turn_finalizer.py:138 sets the reason, :347-386 logs it) to <HERMES_HOME>/logs/agent.log.
  The launcher remembers the log's inode and size at launch and reads only the bytes added since, even
  when the log rotates to agent.log.N (hermes_logging.py:387-406). Background forks share the session id and
  tag their turn ends with " origin=<name>" (turn_finalizer.py:376-391). The launcher ignores those lines and
  resumes only when the controller's last foreground turn end for the session is max_iterations_reached.

Resume (pitfall 30a): `--resume` refuses changed policy flags (hermes_cli/gateway_chat.py:22,
:249), so the resume carries only --resume, --query-file and --in plus the output-mode flags
--oneshot and -Q, which are not policy flags. It runs once. A second step stop is only recorded.
Any other failure is recorded and not resumed. The launcher never passes --source.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "controller-launch-v1"
DEFAULT_HERMES = "~/.local/bin/hermes"
SESSION_RE = re.compile(r"^session_id: (\S+)\s*$", re.M)
# Turn-end line: reason, session, and a trailing origin=<name> only on background forks.
TURN_RE = re.compile(r"Turn ended: reason=(\S+)\s.*?\bsession=(\S+)(?:\s+origin=(\S+))?\s*$")
STEP_REASON_RE = re.compile(r"max_iterations_reached\(\d+/\d+\)$")
# Flags the launcher owns. Passing them through would break the start or the resume.
OWNED_FLAGS = {"--source", "--resume", "-r", "--max-turns", "--query-file", "--in", "-q", "--query",
               "--continue", "-c", "--oneshot", "-Q", "--quiet", "--ignore-rules"}
RESUME_TEXT = ("continue. Pick up where the last run stopped and carry the campaign through to the end "
               "of the original packet ({packet}). Do not redo finished work.\n")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.replace(tmp, path)


def find_session_id(log_path: Path) -> str | None:
    matches = SESSION_RE.findall(log_path.read_text(errors="replace")) if log_path.is_file() else []
    return matches[-1] if matches else None


def log_mark(agent_log: Path) -> tuple[int | None, int]:
    """Inode and size of agent.log at launch; (None, 0) when it does not exist yet."""
    try:
        st = agent_log.stat()
    except OSError:
        return None, 0
    return st.st_ino, st.st_size


def new_log_text(agent_log: Path, mark: tuple[int | None, int]) -> list[str]:
    """Text added to the log since mark, oldest file first.

    Rotation renames agent.log to agent.log.1 and shifts older backups up, so the launch file is found
    by inode among agent.log and agent.log.N. Its bytes after the launch size come first, then every
    newer file whole. When the launch file is gone or never existed, every remaining file is newer.
    """
    inode, offset = mark
    backups = [p for p in agent_log.parent.glob(agent_log.name + ".*") if p.name.rsplit(".", 1)[1].isdigit()]
    backups.sort(key=lambda p: -int(p.name.rsplit(".", 1)[1]))  # agent.log.3, .2, .1: oldest first
    files = [(p, 0) for p in backups + [agent_log]]
    for i, (path, _) in enumerate(files):
        try:
            st = path.stat()
        except OSError:
            continue
        if inode is not None and st.st_ino == inode:
            files = [(path, offset if offset <= st.st_size else 0)] + files[i + 1:]
            break
    chunks = []
    for path, start in files:
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                chunks.append(fh.read().decode("utf-8", "replace"))
        except OSError:
            continue
    return chunks


def stopped_for_steps(agent_log: Path, mark: tuple[int | None, int], session_id: str | None) -> bool:
    """True when the controller's last foreground turn end for this session since launch hit the step limit.

    Background forks share the session id and log their own turn ends with origin=<name>. They are ignored.
    """
    if not session_id:
        return False
    reason = None
    for text in new_log_text(agent_log, mark):
        for line in text.splitlines():
            m = TURN_RE.search(line)
            if m and m.group(2) == session_id and not m.group(3):
                reason = m.group(1)
    return bool(reason and STEP_REASON_RE.match(reason))


def run_once(kind: str, cmd: list[str], log_path: Path, agent_log: Path, record: dict, record_path: Path) -> dict:
    """Run one hermes process, log stdout+stderr, update the record before and after."""
    mark = log_mark(agent_log)
    run = {"kind": kind, "command": cmd, "started_at": now(), "log": str(log_path), "pid": None,
           "session_id": None, "exit_code": None, "stopped_for_steps": False}
    record["runs"].append(run)
    record["status"] = "running"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with log_path.open("wb") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
            run["pid"] = proc.pid
            write_json(record_path, record)
            run["exit_code"] = proc.wait()
    except OSError as exc:
        run["exit_code"] = 127
        run["error"] = str(exc)
    run["ended_at"] = now()
    run["session_id"] = find_session_id(log_path)
    run["stopped_for_steps"] = run["exit_code"] != 0 and stopped_for_steps(agent_log, mark, run["session_id"])
    record["session_id"] = run["session_id"] or record["session_id"]
    record["exit_code"] = run["exit_code"]
    write_json(record_path, record)
    return run


def default_agent_log() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "logs" / "agent.log"


def run_with_one_resume(*, hermes: str, cmd: list[str], packet: Path, root: Path, log_dir: Path, log_stem: str,
                        record_path: Path, agent_log: Path, resume_packet: Path | None = None,
                        schema: str = SCHEMA, extra: dict | None = None) -> dict:
    """Run cmd once; on a step-limit stop resume exactly once with only --resume, --query-file and --in.

    Shared by this launcher and the phased run driver (DR-897). Logs go to <log_dir>/<log_stem>-run-N.log.
    Returns the final record, also written to record_path.
    """
    first_log = log_dir / f"{log_stem}-run-1.log"
    record = {"schema": schema, **(extra or {}), "command": cmd, "started_at": now(), "log": str(first_log),
              "session_id": None, "exit_code": None, "resume_count": 0, "status": "starting", "runs": []}
    first = run_once("start", cmd, first_log, agent_log, record, record_path)
    if first["stopped_for_steps"]:
        if resume_packet is None:
            resume_packet = log_dir / f"{log_stem}-resume-packet.md"
            resume_packet.write_text(RESUME_TEXT.format(packet=packet))
        record["resume_count"] = 1
        resume_cmd = [hermes, "chat", "--oneshot", "-Q", "--resume", first["session_id"],
                      "--query-file", str(resume_packet), "--in", str(root)]
        second = run_once("resume", resume_cmd, log_dir / f"{log_stem}-run-2.log", agent_log, record, record_path)
        record["log"] = second["log"]
        record["status"] = ("step_limit_after_resume" if second["stopped_for_steps"]
                            else "completed" if second["exit_code"] == 0 else "failed_after_resume")
    else:
        record["status"] = "completed" if first["exit_code"] == 0 else "failed"
    record["ended_at"] = now()
    write_json(record_path, record)
    return record


def launch(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    packet = Path(args.packet).expanduser().resolve()
    hermes = str(Path(args.hermes_bin).expanduser())
    receipts = root / "receipts"
    record_path = Path(args.record).expanduser().resolve() if args.record else receipts / "controller-launch.json"
    agent_log = Path(args.agent_log).expanduser() if args.agent_log else default_agent_log()
    cmd = [hermes, "chat", "--oneshot", "-Q", "--ignore-rules", "--max-turns", str(args.max_turns),
           "--query-file", str(packet), "--in", str(root), *args.passthrough]
    # Without --resume-packet the default receipts/controller-resume-packet.md is written only on a step stop.
    resume_packet = Path(args.resume_packet).expanduser().resolve() if args.resume_packet else None
    record = run_with_one_resume(hermes=hermes, cmd=cmd, packet=packet, root=root, log_dir=receipts,
                                 log_stem="controller", record_path=record_path, agent_log=agent_log,
                                 resume_packet=resume_packet)
    print(json.dumps({"status": record["status"], "session_id": record["session_id"],
                      "exit_code": record["exit_code"], "resume_count": record["resume_count"],
                      "record": str(record_path)}))
    return record["exit_code"] or 0


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0],
                                     epilog="Flags after -- go to hermes chat on the first run only, "
                                            "for example: -- -m <model> -t web,terminal,file --provider openrouter")
    parser.add_argument("--packet", required=True, help="controller packet (query file)")
    parser.add_argument("--root", required=True, help="campaign root, passed as --in")
    parser.add_argument("--resume-packet", help="query file for the one resume; a short default is written if omitted")
    parser.add_argument("--max-turns", type=int, default=500)
    parser.add_argument("--hermes-bin", default=DEFAULT_HERMES, help="hermes binary (override for tests)")
    parser.add_argument("--record", help="run record path (default <root>/receipts/controller-launch.json)")
    parser.add_argument("--agent-log", help="agent.log path (default $HERMES_HOME/logs/agent.log)")
    parser.add_argument("passthrough", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    bad = [a for a in args.passthrough if a.split("=", 1)[0] in OWNED_FLAGS]
    if bad:
        parser.error(f"the launcher owns these flags, do not pass them: {', '.join(bad)}")
    return args


def main(argv: list[str] | None = None) -> int:
    return launch(parse(argv))


if __name__ == "__main__":
    sys.exit(main())
