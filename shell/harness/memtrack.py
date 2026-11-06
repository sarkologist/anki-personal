# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Sample a running shell's memory over a day of real use, for the phase 1
exit check.

usage: python3 shell/harness/memtrack.py [--interval-min N] [--out FILE]

Every N minutes (default 5) it appends a CSV row: the shell's footprint, the
total of the WebKit processes started after it (its WebContent, networking and
GPU processes; a Safari tab opened meanwhile would be counted too), and their
count. Start it after the shell; it stops when the shell exits. Compare the
totals with the bench numbers in the plan's Phase 1 findings.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import time
from pathlib import Path


def footprint_mb(pid: int) -> float:
    out = subprocess.run(["footprint", str(pid)], capture_output=True, text=True).stdout
    m = re.search(r"Footprint: ([\d.]+) (KB|MB|GB)", out)
    if not m:
        return 0.0
    value = float(m.group(1))
    return {"KB": value / 1024, "MB": value, "GB": value * 1024}[m.group(2)]


def processes() -> list[tuple[int, float, str]]:
    """(pid, start time as unix seconds, command) for every process."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,lstart=,comm="], capture_output=True, text=True
    ).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid, start, comm = parts[0], " ".join(parts[1:6]), parts[6]
        started = time.mktime(time.strptime(start, "%a %b %d %H:%M:%S %Y"))
        rows.append((int(pid), started, comm))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--interval-min", type=float, default=5)
    parser.add_argument("--out", type=Path, default=Path("shell-mem.csv"))
    args = parser.parse_args()

    shells = [(pid, started) for pid, started, comm in processes() if comm.endswith("anki-shell")]
    if not shells:
        sys.exit("no running anki-shell")
    shell_pid, shell_started = shells[0]
    new = not args.out.exists()
    with args.out.open("a") as out:
        if new:
            out.write("time,shell_mb,webkit_mb,webkit_processes,total_mb\n")
        while True:
            procs = processes()
            if not any(pid == shell_pid for pid, _, _ in procs):
                break
            webkit = [
                pid
                for pid, started, comm in procs
                if "com.apple.WebKit" in comm and started >= shell_started - 1
            ]
            shell_mb = footprint_mb(shell_pid)
            webkit_mb = sum(footprint_mb(pid) for pid in webkit)
            now = datetime.datetime.now().isoformat(timespec="seconds")
            row = f"{now},{shell_mb:.0f},{webkit_mb:.0f},{len(webkit)},{shell_mb + webkit_mb:.0f}"
            out.write(row + "\n")
            out.flush()
            print(row, flush=True)
            time.sleep(args.interval_min * 60)


if __name__ == "__main__":
    main()
