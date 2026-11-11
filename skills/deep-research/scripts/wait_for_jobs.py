#!/usr/bin/env python3
"""Block until background jobs finish or a timeout passes, in one call (DR-876).

A job is a PID (--pid N) or a marker file (--marker PATH) that the job writes when it is done.
Use one call per background job instead of repeated sleep-and-check shell commands.
Exit 0 when every job is done, 124 on timeout, 2 on bad usage. A JSON summary is printed.
"""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable


def pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie has exited but is not reaped yet
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return True
    return state not in ("Z", "X")


def wait_for_jobs(pids=(), markers=(), timeout=3600.0, interval=2.0, sleep=time.sleep, clock=time.monotonic) -> dict:
    jobs: dict[str, Callable[[], bool]] = {}
    for p in pids:
        jobs[f"pid:{p}"] = lambda p=p: not pid_running(p)
    for m in markers:
        jobs[f"marker:{m}"] = lambda m=m: Path(m).exists()
    start = clock()
    done: list[str] = []
    while True:
        done = [name for name, is_done in jobs.items() if is_done()]
        pending = [name for name in jobs if name not in done]
        waited = round(clock() - start, 2)
        if not pending:
            return {"status": "done", "done": done, "pending": [], "waited_s": waited}
        if waited >= timeout:
            return {"status": "timeout", "done": done, "pending": pending, "waited_s": waited}
        sleep(min(interval, max(timeout - waited, 0)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--pid", type=int, action="append", default=[], help="process id to wait on (repeatable)")
    parser.add_argument("--marker", action="append", default=[], help="file that appears when the job is done (repeatable)")
    parser.add_argument("--timeout", type=float, default=3600, help="seconds before giving up (default 3600)")
    parser.add_argument("--interval", type=float, default=2, help="poll interval in seconds (default 2)")
    args = parser.parse_args(argv)
    if not (args.pid or args.marker):
        parser.error("give at least one --pid or --marker")
    result = wait_for_jobs(args.pid, args.marker, args.timeout, args.interval)
    print(json.dumps(result))
    return 0 if result["status"] == "done" else 124


if __name__ == "__main__":
    sys.exit(main())
