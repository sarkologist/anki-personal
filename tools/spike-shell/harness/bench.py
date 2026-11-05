"""Run the same reviewer workload in the spike shell and in stock Anki, and
record memory footprint across each app's processes at checkpoints."""
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

H = Path(__file__).resolve().parent
REPO = H.parents[2]
# scratch space for collection copies; never the real profile
S = Path(__import__("os").environ.get("BENCH_DIR", "/tmp/anki-spike-bench"))
REAL = Path.home() / "Library/Application Support/Anki2/User 1"
X, Y, W, HGT = -850, -1340, 1600, 1000
INTERVAL = 250
CHECKPOINTS = [("idle", 0, 0), ("after 40 cards", 0, 40), ("after 240 cards", 40, 200)]


def footprint_mb(pid: int) -> float:
    out = subprocess.run(["footprint", str(pid)], capture_output=True, text=True).stdout
    m = re.search(r"Footprint: ([\d.]+) (KB|MB|GB)", out)
    if not m:
        return 0.0
    v = float(m.group(1))
    return {"KB": v / 1024, "MB": v, "GB": v * 1024}[m.group(2)]


def pids_matching(pattern: str) -> set[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True).stdout
    return {int(p) for p in out.split()}


def measure(pids: dict[str, int]) -> dict:
    per = {name: round(footprint_mb(pid)) for name, pid in pids.items()}
    return {"total": sum(per.values()), "processes": per}


def fresh_collection(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(REAL / "collection.anki2", dest / "collection.anki2")
    media = dest / "collection.media"
    if not media.exists():
        shutil.copytree(REAL / "collection.media", media)


def run_spike() -> dict:
    base = S / "bench-spike"
    shutil.rmtree(base, ignore_errors=True)
    fresh_collection(base)
    before = pids_matching("com.apple.WebKit")
    t0 = time.time()
    proc = subprocess.Popen(
        [str(REPO / "target/release/spike-shell"), "--collection", str(base / "collection.anki2"),
         "--media", str(base / "collection.media"), "--web", str(REPO / "out/qt/_aqt/data/web"),
         "--mode", "stdin", "--x", str(X), "--y", str(Y), "--width", str(W),
         "--height", str(HGT), "--interval-ms", str(INTERVAL)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    assert proc.stdout.readline().strip() == "ready"
    startup = time.time() - t0
    webkit = pids_matching("com.apple.WebKit") - before
    names = {"shell": proc.pid}
    for p in webkit:
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(p)], capture_output=True, text=True).stdout.strip()
        names[comm.rsplit(".", 1)[-1] + f" {p}"] = p
    results = {"startup_s": round(startup, 2), "checkpoints": {}}
    for label, offset, count in CHECKPOINTS:
        if count:
            proc.stdin.write(f"cycle {offset} {count}\n")
            proc.stdin.flush()
            assert proc.stdout.readline().strip() == "cycled"
        time.sleep(5)
        results["checkpoints"][label] = measure(names)
    proc.kill()
    return results


def ctl(code: str) -> dict:
    req = urllib.request.Request("http://127.0.0.1:8790/", data=code.encode(), method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=70).read())


def run_stock() -> dict:
    base = S / "bench-stock"
    shutil.rmtree(base, ignore_errors=True)
    fresh_collection(base / "User 1")
    shutil.copytree(H / "addon/benchctl", base / "addons21/benchctl")
    t0 = time.time()
    proc = subprocess.Popen(
        [str(Path.home() / ".anki-stable/bin/python"), str(H / "stock_anki.py"), str(base)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={**__import__("os").environ, "BENCH_GEOM": f"{X},{Y},{W},{HGT}"},
    )
    while True:
        try:
            if "deckBrowser" in ctl("result = mw.state").get("result", ""):
                break
        except Exception:
            pass
        time.sleep(0.1)
    startup = time.time() - t0
    ctl("from aqt.sound import av_player\nav_player.play_tags = lambda tags: None\n"
        f"mw.setGeometry({X}, {Y}, {W}, {HGT})\nresult = 1")
    children = subprocess.run(["pgrep", "-P", str(proc.pid)], capture_output=True, text=True).stdout.split()
    names = {"anki": proc.pid}
    for p in map(int, children):
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(p)], capture_output=True, text=True).stdout.strip()
        names[Path(comm).name + f" {p}"] = p
    cycle = (H / "cycle.py").read_text()
    results = {"startup_s": round(startup, 2), "checkpoints": {}}
    for label, offset, count in CHECKPOINTS:
        if count:
            code = cycle.replace("OFFSET", str(offset)).replace("CARDS", str(count)).replace("INTERVAL", str(INTERVAL))
            ctl(code)
            while "None" in ctl('result = mw._bench["done"]')["result"]:
                time.sleep(0.5)
        time.sleep(5)
        results["checkpoints"][label] = measure(names)
    subprocess.run(["kill", "-9", str(proc.pid)])
    return results


if __name__ == "__main__":
    which = sys.argv[1:] or ["spike", "stock", "spike", "stock"]
    for name in which:
        res = run_spike() if name == "spike" else run_stock()
        summary = {k: v["total"] for k, v in res["checkpoints"].items()}
        print(json.dumps({"app": name, "startup_s": res["startup_s"], "totals_mb": summary,
                          "detail": res["checkpoints"]}), flush=True)
