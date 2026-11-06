#!/usr/bin/env python3
"""Compatibility entry point for the explicit historical saved-draft replay."""
from __future__ import annotations

# Retain the actual executed module, including imports before completion starts.
__checker_module_code__ = __import__("sys")._getframe().f_code

import argparse
import json
from pathlib import Path

from research_qa_repair import replay_saved


def replay(root: Path) -> dict:
    return replay_saved(root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-root", required=True)
    args = parser.parse_args()
    result = replay(Path(args.campaign_root))
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
