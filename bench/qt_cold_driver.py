#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Run each QtWebEngine fallback observation in a fresh process."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmark import aggregate

HERE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("iterations", nargs="?", type=int, default=5)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--cases",
        default="math,nomath",
        help="comma-separated subset of: math, nomath, math-lazy, nomath-lazy",
    )
    args = parser.parse_args()
    runs: dict[str, list[dict[str, Any]]] = {
        case.strip(): [] for case in args.cases.split(",") if case.strip()
    }
    metadata: dict[str, Any] | None = None
    for index in range(args.iterations):
        for case, case_runs in runs.items():
            print(
                f"cold iteration {index + 1}/{args.iterations}: {case}", file=sys.stderr
            )
            command = [
                str(HERE / "run-qt-fallback.sh"),
                str(args.artifact_dir),
                "1",
                "--case",
                case,
            ]
            if args.config:
                command.extend(("--config", str(args.config)))
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60,
                env=os.environ,
                check=False,
            )
            if completed.returncode:
                raise RuntimeError(
                    f"QtWebEngine worker exited {completed.returncode}:\n{completed.stderr[-4000:]}"
                )
            worker = json.loads(completed.stdout)
            metadata = worker
            case_runs.append(worker["runs"][case][0])
    assert metadata
    report = {
        "driver": "QtWebEngine fallback, one fresh process/page per observation "
        "(custom URL scheme; no HTTP transfer timing)",
        "artifact_js_dir": metadata["artifact_js_dir"],
        "config_override": metadata["config_override"],
        "iterations_per_case": args.iterations,
        "machine": metadata["machine"],
        "summary": {case: aggregate(items) for case, items in runs.items()},
        "runs": runs,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + "\n")


if __name__ == "__main__":
    main()
