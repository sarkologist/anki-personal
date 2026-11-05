# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Memory check for the native shell: drive the stock reviewer page through
cards and record the footprint of the shell and its WebKit processes.

usage: python3 shell/harness/bench.py [--shell PATH] [--collection PATH]
                                      [--max-total-mb N] [--max-growth-mb N]
                                      [--compare-stock] [--json]

By default it runs on a fresh profile with synthetic notes, so it needs no
user data and can run in CI (macOS, with a window server). It fails (exit 1)
if the total footprint at any checkpoint exceeds --max-total-mb, or if it
grows by more than --max-growth-mb from idle to the last checkpoint: a leak
check, and a guard that the shell stays much lighter than Qt (893-1506 MB on
the same workload in the spike).

--collection uses a copy of the given collection.anki2 instead (its media
folder is copied next to it if present); never pass a live profile's path
without copying it first. --compare-stock also runs the same workload in a
second, isolated stock Anki (needs ANKI_STOCK_PYTHON, a Python with aqt
installed; defaults to ~/.anki-stable/bin/python) for a side-by-side.

Windows open in the background and don't take focus.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HARNESS = Path(__file__).resolve().parent
REPO = HARNESS.parents[1]
GEOMETRY = (100, 100, 1600, 1000)
INTERVAL_MS = 250
SEED_NOTES = 300
# (label, offset, count): cards cycled before measuring
CHECKPOINTS = [("idle", 0, 0), ("after 40 cards", 0, 40), ("after 240 cards", 40, 200)]
SETTLE_S = 5


def default_shell() -> Path:
    for path in (
        REPO / "out/shell/AnkiShell.app/Contents/MacOS/anki-shell",
        REPO / "out/rust/debug/anki-shell",
        REPO / "target/debug/anki-shell",
    ):
        if path.exists():
            return path
    sys.exit("no shell binary found; run ./ninja shell")


def footprint_mb(pid: int) -> float:
    out = subprocess.run(
        ["footprint", str(pid)], capture_output=True, text=True
    ).stdout
    m = re.search(r"Footprint: ([\d.]+) (KB|MB|GB)", out)
    if not m:
        return 0.0
    value = float(m.group(1))
    return {"KB": value / 1024, "MB": value, "GB": value * 1024}[m.group(2)]


def pids_matching(pattern: str) -> set[int]:
    out = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True
    ).stdout
    return {int(p) for p in out.split()}


def process_name(pid: int) -> str:
    comm = subprocess.run(
        ["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return f"{comm.rsplit('/', 1)[-1].rsplit('.', 1)[-1]} {pid}"


def measure(pids: dict[str, int]) -> dict:
    per = {name: round(footprint_mb(pid)) for name, pid in pids.items()}
    return {"total": sum(per.values()), "processes": per}


def prepare_profile(base: Path, collection: Path | None) -> Path:
    """An Anki base folder with one profile; returns the shell settings."""
    profile = base / "bench"
    profile.mkdir(parents=True)
    if collection:
        shutil.copy(collection, profile / "collection.anki2")
        media = collection.with_suffix(".media")
        if media.is_dir():
            shutil.copytree(media, profile / "collection.media")
    settings = base / "shell-settings.json"
    settings.write_text(
        json.dumps(
            {
                "anki_base": str(base),
                "last_profile": "bench",
                "profiles": [{"name": "bench"}],
            }
        )
    )
    return settings


def run_shell(shell: Path, collection: Path | None) -> dict:
    with tempfile.TemporaryDirectory(prefix="anki-shell-bench-") as tmp:
        settings = prepare_profile(Path(tmp), collection)
        before = pids_matching("com.apple.WebKit")
        t0 = time.time()
        geometry = ",".join(map(str, GEOMETRY))
        proc = subprocess.Popen(
            [str(shell), "--settings", str(settings), "--bench",
             "--ankiconnect-port", "0", "--geometry", geometry],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )  # fmt: skip
        assert proc.stdin and proc.stdout

        def command(line: str, reply: str) -> None:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            got = proc.stdout.readline().strip()
            assert got == reply, f"{line!r}: expected {reply!r}, got {got!r}"

        try:
            assert proc.stdout.readline().strip() == "ready", "shell didn't start"
            startup = time.time() - t0
            if not collection:
                command(f"seed {SEED_NOTES}", "seeded")
            pids = {"shell": proc.pid}
            for pid in pids_matching("com.apple.WebKit") - before:
                pids[process_name(pid)] = pid
            results: dict = {"startup_s": round(startup, 2), "checkpoints": {}}
            for label, offset, count in CHECKPOINTS:
                if count:
                    command(f"cycle {offset} {count}", "cycled")
                time.sleep(SETTLE_S)
                results["checkpoints"][label] = measure(pids)
            command("quit", "")
        finally:
            proc.kill()
            proc.wait()
    return results


def run_stock(collection: Path | None) -> dict:
    python = os.environ.get(
        "ANKI_STOCK_PYTHON", str(Path.home() / ".anki-stable/bin/python")
    )
    with tempfile.TemporaryDirectory(prefix="anki-stock-bench-") as tmp:
        base = Path(tmp)
        profile = base / "User 1"
        profile.mkdir()
        if collection:
            shutil.copy(collection, profile / "collection.anki2")
            media = collection.with_suffix(".media")
            if media.is_dir():
                shutil.copytree(media, profile / "collection.media")
        shutil.copytree(HARNESS / "stock/addon/benchctl", base / "addons21/benchctl")
        x, y, w, h = GEOMETRY
        t0 = time.time()
        proc = subprocess.Popen(
            [python, str(HARNESS / "stock/stock_anki.py"), str(base)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "BENCH_GEOM": f"{x},{y},{w},{h}"},
        )  # fmt: skip

        def ctl(code: str) -> dict:
            req = urllib.request.Request(
                "http://127.0.0.1:8790/", data=code.encode(), method="POST"
            )
            return json.loads(urllib.request.urlopen(req, timeout=70).read())

        try:
            while True:
                try:
                    if "deckBrowser" in ctl("result = mw.state").get("result", ""):
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            startup = time.time() - t0
            if not collection:
                ctl(
                    "from anki.collection import Collection\n"
                    "m = mw.col.models.by_name('Basic')\n"
                    f"for i in range({SEED_NOTES}):\n"
                    "    n = mw.col.new_note(m)\n"
                    "    n['Front'] = f'Question {i}: what is \\\\(x^{{{i}}} \\\\cdot x\\\\)?'\n"
                    "    n['Back'] = f'\\\\(x^{{{i + 1}}}\\\\) <b>answer</b> {i}'\n"
                    "    mw.col.add_note(n, 1)\n"
                    "result = 1"
                )
            ctl(
                "from aqt.sound import av_player\n"
                "av_player.play_tags = lambda tags: None\nresult = 1"
            )
            children = subprocess.run(
                ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True
            ).stdout.split()
            pids = {"anki": proc.pid}
            for pid in map(int, children):
                pids[process_name(pid)] = pid
            cycle = (HARNESS / "stock/cycle.py").read_text()
            results: dict = {"startup_s": round(startup, 2), "checkpoints": {}}
            for label, offset, count in CHECKPOINTS:
                if count:
                    code = (
                        cycle.replace("OFFSET", str(offset))
                        .replace("CARDS", str(count))
                        .replace("INTERVAL", str(INTERVAL_MS))
                    )
                    ctl(code)
                    while "None" in ctl('result = mw._bench["done"]')["result"]:
                        time.sleep(0.5)
                time.sleep(SETTLE_S)
                results["checkpoints"][label] = measure(pids)
        finally:
            proc.kill()
            proc.wait()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shell", type=Path, default=None)
    parser.add_argument("--collection", type=Path, default=None)
    parser.add_argument("--max-total-mb", type=float, default=400)
    parser.add_argument("--max-growth-mb", type=float, default=150)
    parser.add_argument("--compare-stock", action="store_true")
    args = parser.parse_args()

    shell = run_shell(args.shell or default_shell(), args.collection)
    totals = {k: v["total"] for k, v in shell["checkpoints"].items()}
    report = {"app": "shell", "startup_s": shell["startup_s"], "totals_mb": totals,
              "detail": shell["checkpoints"]}  # fmt: skip
    print(json.dumps(report), flush=True)

    if args.compare_stock:
        stock = run_stock(args.collection)
        stock_totals = {k: v["total"] for k, v in stock["checkpoints"].items()}
        print(json.dumps({"app": "stock", "startup_s": stock["startup_s"],
                          "totals_mb": stock_totals, "detail": stock["checkpoints"]}),
              flush=True)  # fmt: skip

    failures = []
    peak = max(totals.values())
    if peak > args.max_total_mb:
        failures.append(f"peak {peak} MB > {args.max_total_mb} MB")
    values = list(totals.values())
    growth = values[-1] - values[0]
    if growth > args.max_growth_mb:
        failures.append(f"growth {growth} MB > {args.max_growth_mb} MB")
    if failures:
        print("FAIL: " + "; ".join(failures), file=sys.stderr)
        sys.exit(1)
    print(f"ok: peak {peak} MB, growth {growth} MB", file=sys.stderr)


if __name__ == "__main__":
    main()
