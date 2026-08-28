#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Cold-load MathJax benchmark using Chrome's headless mode and stdlib only."""

from __future__ import annotations

import argparse
import html
import json
import os
import platform
import plistlib
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEFAULT_CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
RESULT_START = '<pre id="result">'
RESULT_END = "</pre>"


def find_chrome() -> Path:
    if configured := os.environ.get("CHROME"):
        path = Path(configured).expanduser()
        if path.is_file():
            return path.resolve()
        raise SystemExit(f"CHROME does not point to a file: {path}")
    for name in ("google-chrome", "chromium", "chromium-browser"):
        if found := shutil.which(name):
            return Path(found).resolve()
    candidates = (
        DEFAULT_CHROME,
        Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        Path("/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
    )
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit("No Chromium-family browser found; set CHROME=/path/to/browser")


def chrome_version(chrome: Path) -> str:
    plist = chrome.parents[2] / "Info.plist"
    try:
        with plist.open("rb") as handle:
            info = plistlib.load(handle)
        return str(info.get("CFBundleShortVersionString", "unknown"))
    except (OSError, plistlib.InvalidFileException):
        return "unknown"


def resolve_js_dir(argument: Path) -> Path:
    root = argument.expanduser().resolve()
    candidates = (root / "js", root)
    for candidate in candidates:
        if (candidate / "mathjax.js").is_file() and (
            candidate / "vendor/mathjax/tex-chtml-full.js"
        ).is_file():
            return candidate
    raise SystemExit(
        f"{root} is neither a built web directory nor a js directory containing "
        "mathjax.js and vendor/mathjax/tex-chtml-full.js"
    )


def benchmark_html(case: str) -> bytes:
    # Eager cases: script tags intentionally match AqtWebView.stdHtml(): config
    # first, then the combined component, both classic and parser-blocking.
    # Lazy cases ("*-lazy"): no MathJax script tags; the measurement script
    # replicates ts/reviewer/mathjax.ts lazyLoadMathJax() (detection regex,
    # then config script onload -> component script onload).
    lazy = case.endswith("-lazy")
    base_case = case.removesuffix("-lazy")
    card = (
        r"Inline \(e^{i\pi}+1=0\), then \(\sqrt{x^2+y^2}\). "
        r"\[\mathtoolsset{showonlyrefs}\begin{aligned}"
        r"A&amp;=\begin{dcases}x^2&amp;x&gt;0\\0&amp;x\leq0\end{dcases}\\"
        r"B&amp;=\coloneqq\frac{1}{1+x}\end{aligned}\]"
        if base_case == "math"
        else "A representative review card with text, punctuation, and no mathematics."
    )
    eager_scripts = """<script src="/_anki/js/mathjax.js"></script>
<script>__bench.configEnd = performance.now(); __bench.mainStart = performance.now();</script>
<script id="MathJax-script" src="/_anki/js/vendor/mathjax/tex-chtml-full.js"></script>
<script>__bench.mainEnd = performance.now();</script>"""
    document = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>running</title></head><body>
<script>
window.__bench = {{errors: [], configStart: performance.now()}};
addEventListener("error", event => __bench.errors.push(String(event.message || event.error)));
addEventListener("unhandledrejection", event => __bench.errors.push(String(event.reason)));
</script>
{"" if lazy else eager_scripts}
<div id="qa" dir="auto">{card}</div><pre id="result">pending</pre>
<script>
(async () => {{
  const b = window.__bench;
  const resource = path => performance.getEntriesByType("resource")
    .find(entry => new URL(entry.name).pathname === path);
  const snap = entry => entry ? {{
    name: new URL(entry.name).pathname,
    initiatorType: entry.initiatorType,
    duration_ms: entry.duration,
    transfer_bytes: entry.transferSize,
    encoded_body_bytes: entry.encodedBodySize,
    decoded_body_bytes: entry.decodedBodySize,
  }} : null;
  const lazy = {json.dumps(lazy)};
  const output = {{case: {json.dumps(case)}, status: "ok"}};
  const loadScript = src => new Promise((resolve, reject) => {{
    const script = document.createElement("script");
    script.src = src;
    script.onload = resolve;
    script.onerror = () => reject(new Error("failed to load " + src));
    document.head.appendChild(script);
  }});
  try {{
    let doTypeset = true;
    if (lazy) {{
      // Mirrors ts/reviewer/mathjax.ts containsMathjax()/lazyLoadMathJax().
      const qaEl = document.getElementById("qa");
      const mathjaxRegex = /<anki-mathjax(?:\\s|>)|\\\\\\[(.*?)\\\\\\]|\\\\\\((.*?)\\\\\\)/isu;
      doTypeset = mathjaxRegex.test(qaEl.innerHTML);
      output.contains_math = doTypeset;
      if (doTypeset) {{
        b.configStart = performance.now();
        await loadScript("/_anki/js/mathjax.js");
        b.configEnd = performance.now();
        b.mainStart = performance.now();
        await loadScript("/_anki/js/vendor/mathjax/tex-chtml-full.js");
        b.mainEnd = performance.now();
      }}
    }}
    if (doTypeset) {{
      if (!window.MathJax || !MathJax.startup || !MathJax.startup.promise) {{
        throw new Error("MathJax startup.promise is unavailable after scripts loaded");
      }}
      await MathJax.startup.promise;
      b.startupEnd = performance.now();
      MathJax.typesetClear();
      b.typesetStart = performance.now();
      await MathJax.typesetPromise([document.getElementById("qa")]);
      b.typesetEnd = performance.now();
      // Make layout/font requests observable before taking the resource snapshot.
      document.getElementById("qa").getBoundingClientRect();
      await document.fonts.ready;
      await new Promise(resolve => setTimeout(resolve, 0));
    }}
    b.doneEnd = performance.now();
  }} catch (error) {{
    output.status = "error";
    output.error = String(error && (error.stack || error));
  }}
  const configResource = resource("/_anki/js/mathjax.js");
  const mainResource = resource("/_anki/js/vendor/mathjax/tex-chtml-full.js");
  const entries = performance.getEntriesByType("resource");
  const navigation = performance.getEntriesByType("navigation")[0];
  const configTotal = b.configEnd - b.configStart;
  const mainTotal = b.mainEnd - b.mainStart;
  Object.assign(output, {{
    config_fetch_parse_eval_ms: configTotal,
    config_fetch_ms: configResource ? configResource.duration : null,
    config_parse_eval_residual_ms: configResource ? Math.max(0, configTotal - configResource.duration) : null,
    main_fetch_parse_eval_ms: mainTotal,
    main_fetch_ms: mainResource ? mainResource.duration : null,
    main_parse_eval_residual_ms: mainResource ? Math.max(0, mainTotal - mainResource.duration) : null,
    startup_ms: b.startupEnd ?? null,
    startup_after_main_ms: b.startupEnd == null ? null : b.startupEnd - b.mainEnd,
    typeset_ms: b.typesetEnd == null ? null : b.typesetEnd - b.typesetStart,
    first_card_ms: b.typesetEnd ?? b.doneEnd ?? null,
    transfer_bytes: (navigation ? navigation.transferSize : 0) + entries.reduce((n, entry) => n + entry.transferSize, 0),
    encoded_body_bytes: (navigation ? navigation.encodedBodySize : 0) + entries.reduce((n, entry) => n + entry.encodedBodySize, 0),
    errors: b.errors,
    config_resource: snap(configResource),
    main_resource: snap(mainResource),
    resources: entries.map(snap),
  }});
  if (b.errors.length) {{
    output.status = "error";
    output.error ??= "page reported script/runtime errors";
  }}
  const result = document.getElementById("result");
  result.textContent = JSON.stringify(output);
  document.title = output.status;
  // Report to the harness server; dynamic script loads deadlock Chrome's
  // virtual-time --dump-dom mode, so the driver waits for this POST instead.
  try {{
    await fetch("/result", {{method: "POST", body: JSON.stringify(output)}});
  }} catch (error) {{}}
}})();
</script></body></html>"""
    return document.encode()


class BenchHandler(BaseHTTPRequestHandler):
    js_dir: Path
    config_path: Path | None = None
    result_event: threading.Event = threading.Event()
    last_result: bytes | None = None

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path != "/result":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        type(self).last_result = self.rfile.read(length)
        self.send_response(204)
        self.end_headers()
        type(self).result_event.set()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/bench.html":
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            case = query.get("case", ["math"])[0]
            if case not in ("math", "nomath", "math-lazy", "nomath-lazy"):
                self.send_error(400, "case must be math, nomath, math-lazy, or nomath-lazy")
                return
            self.send_bytes(benchmark_html(case), "text/html; charset=utf-8")
            return
        prefix = "/_anki/js/"
        if path.startswith(prefix):
            relative = Path(path[len(prefix) :])
            target = (
                self.config_path
                if relative == Path("mathjax.js") and self.config_path
                else (self.js_dir / relative).resolve()
            )
            try:
                if target != self.config_path:
                    target.relative_to(self.js_dir)
            except ValueError:
                self.send_error(403)
                return
            if target.is_file():
                content_type = (
                    "text/javascript; charset=utf-8"
                    if target.suffix == ".js"
                    else "font/woff2"
                    if target.suffix == ".woff"
                    else "application/octet-stream"
                )
                self.send_bytes(target.read_bytes(), content_type)
                return
        self.send_error(404)

    def send_bytes(self, payload: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        pass


def parse_dump(document: str) -> dict[str, Any]:
    start = document.find(RESULT_START)
    end = document.find(RESULT_END, start)
    if start < 0 or end < 0:
        raise RuntimeError(
            f"result element absent from Chrome output: {document[-1000:]}"
        )
    payload = html.unescape(document[start + len(RESULT_START) : end])
    return json.loads(payload)


def one_run(chrome: Path, url: str) -> dict[str, Any]:
    BenchHandler.result_event = threading.Event()
    BenchHandler.last_result = None
    with tempfile.TemporaryDirectory(prefix="anki-mathjax-bench-") as profile:
        command = [
            str(chrome),
            "--headless=new",
            "--disable-gpu",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-extensions",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--disk-cache-size=1",
            "--disable-application-cache",
            f"--user-data-dir={profile}",
            url,
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        try:
            if not BenchHandler.result_event.wait(60):
                raise RuntimeError(f"page never reported a result for {url}")
        finally:
            process.kill()
            process.wait()
    assert BenchHandler.last_result is not None
    return json.loads(BenchHandler.last_result)


def spread(values: list[float | int]) -> dict[str, float]:
    ordered = sorted(float(value) for value in values)
    return {
        "median": statistics.median(ordered),
        "min": ordered[0],
        "max": ordered[-1],
    }


def aggregate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    fields = (
        "config_fetch_parse_eval_ms",
        "config_fetch_ms",
        "config_parse_eval_residual_ms",
        "main_fetch_parse_eval_ms",
        "main_fetch_ms",
        "main_parse_eval_residual_ms",
        "startup_ms",
        "startup_after_main_ms",
        "typeset_ms",
        "first_card_ms",
        "transfer_bytes",
        "encoded_body_bytes",
    )
    summary: dict[str, Any] = {}
    for field in fields:
        values = [run[field] for run in runs if run.get(field) is not None]
        summary[field] = spread(values) if values else None
    return summary


def machine_context(chrome: Path) -> dict[str, str]:
    macos = platform.mac_ver()[0] or "unknown"
    context = {
        "machine": platform.machine(),
        "macOS": macos,
        "chrome": chrome_version(chrome),
        "chrome_path": str(chrome),
    }
    context.update(mac_hardware_context())
    return context


def mac_hardware_context() -> dict[str, str]:
    if platform.system() != "Darwin":
        return {}
    try:
        result = subprocess.run(
            ["system_profiler", "SPHardwareDataType", "-detailLevel", "mini"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    wanted = {"Model Identifier", "Chip", "Memory"}
    context: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.strip().partition(":")
        if separator and key in wanted:
            context[key.lower().replace(" ", "_")] = value.strip()
    if chip := os.environ.get("BENCH_CHIP"):
        context["chip"] = chip
    return context


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("iterations", nargs="?", type=int, default=5)
    parser.add_argument(
        "--config",
        type=Path,
        help="benchmark-only replacement for js/mathjax.js",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--cases",
        default="math,nomath",
        help="comma-separated subset of: math, nomath, math-lazy, nomath-lazy",
    )
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")

    chrome = find_chrome()
    js_dir = resolve_js_dir(args.artifact_dir)
    BenchHandler.js_dir = js_dir
    BenchHandler.config_path = (
        args.config.expanduser().resolve() if args.config else None
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), BenchHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/bench.html"
    valid_cases = ("math", "nomath", "math-lazy", "nomath-lazy")
    cases = [case.strip() for case in args.cases.split(",") if case.strip()]
    if invalid := [case for case in cases if case not in valid_cases]:
        parser.error(f"unknown cases: {', '.join(invalid)}")
    all_runs: dict[str, list[dict[str, Any]]] = {case: [] for case in cases}
    try:
        for index in range(args.iterations):
            for case, case_runs in all_runs.items():
                print(
                    f"iteration {index + 1}/{args.iterations}: {case}", file=sys.stderr
                )
                case_runs.append(one_run(chrome, f"{base_url}?case={case}&run={index}"))
    finally:
        server.shutdown()
        server.server_close()

    report = {
        "artifact_js_dir": str(js_dir),
        "config_override": str(BenchHandler.config_path)
        if BenchHandler.config_path
        else None,
        "iterations_per_case": args.iterations,
        "machine": machine_context(chrome),
        "summary": {case: aggregate(runs) for case, runs in all_runs.items()},
        "runs": all_runs,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + "\n")


if __name__ == "__main__":
    main()
