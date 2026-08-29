#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""QtWebEngine fallback when the environment prohibits Chrome or TCP binds."""

from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any

from benchmark import aggregate, benchmark_html, mac_hardware_context, resolve_js_dir
from PyQt6.QtCore import QBuffer, QByteArray, QEventLoop, QIODevice, QTimer, QUrl
from PyQt6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineUrlRequestJob,
    QWebEngineUrlScheme,
    QWebEngineUrlSchemeHandler,
    qWebEngineChromiumVersion,
    qWebEngineVersion,
)
from PyQt6.QtWidgets import QApplication

SCHEME = b"anki-bench"


class AssetHandler(QWebEngineUrlSchemeHandler):
    def __init__(self, js_dir: Path, config: Path | None) -> None:
        super().__init__()
        self.js_dir = js_dir
        self.config = config
        self.buffers: list[QBuffer] = []
        self.served: list[dict[str, object]] = []

    def requestStarted(self, job: QWebEngineUrlRequestJob) -> None:  # noqa: N802
        path = job.requestUrl().path()
        prefix = "/_anki/js/"
        if not path.startswith(prefix):
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        relative = Path(path[len(prefix) :])
        target = (
            self.config
            if relative == Path("mathjax.js") and self.config
            else self.js_dir / relative
        ).resolve()
        try:
            if target != self.config:
                target.relative_to(self.js_dir)
            payload = target.read_bytes()
        except (ValueError, OSError):
            job.fail(QWebEngineUrlRequestJob.Error.UrlNotFound)
            return
        mime = b"text/javascript" if target.suffix == ".js" else b"font/woff"
        buffer = QBuffer(job)
        buffer.setData(QByteArray(payload))
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        self.buffers.append(buffer)
        self.served.append({"name": path, "encoded_body_bytes": len(payload)})
        job.reply(mime, buffer)


def run_page(
    profile: QWebEngineProfile, handler: AssetHandler, case: str
) -> dict[str, Any]:
    handler.served.clear()
    page = QWebEnginePage(profile)
    loop = QEventLoop()
    result: dict[str, Any] = {}
    deadline = time.monotonic() + 40

    def poll() -> None:
        if time.monotonic() > deadline:
            result.update(status="error", error="QtWebEngine result timeout", case=case)
            loop.quit()
            return

        def checked(value: object) -> None:
            if isinstance(value, str) and value != "pending":
                try:
                    result.update(json.loads(value))
                except json.JSONDecodeError as error:
                    result.update(status="error", error=str(error), case=case)
                loop.quit()
            else:
                QTimer.singleShot(25, poll)

        page.runJavaScript("document.getElementById('result')?.textContent", checked)

    page.setHtml(benchmark_html(case).decode(), QUrl("anki-bench://bench/bench.html"))
    QTimer.singleShot(0, poll)
    QTimer.singleShot(45_000, loop.quit)
    loop.exec()
    page.deleteLater()
    QApplication.processEvents()
    result["served_resources"] = list(handler.served)
    result["encoded_body_bytes"] = len(benchmark_html(case)) + sum(
        int(item["encoded_body_bytes"]) for item in handler.served
    )
    result["transfer_bytes"] = None
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact_dir", type=Path)
    parser.add_argument("iterations", nargs="?", type=int, default=5)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--case",
        choices=("both", "math", "nomath", "math-lazy", "nomath-lazy"),
        default="both",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    js_dir = resolve_js_dir(args.artifact_dir)
    config = args.config.expanduser().resolve() if args.config else None

    scheme = QWebEngineUrlScheme(SCHEME)
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.HostAndPort)
    scheme.setDefaultPort(80)
    scheme.setFlags(
        QWebEngineUrlScheme.Flag.SecureScheme
        | QWebEngineUrlScheme.Flag.LocalScheme
        | QWebEngineUrlScheme.Flag.LocalAccessAllowed
        | QWebEngineUrlScheme.Flag.ContentSecurityPolicyIgnored
    )
    QWebEngineUrlScheme.registerScheme(scheme)
    app = QApplication(sys.argv[:1])
    profile = QWebEngineProfile()
    profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.NoPersistentCookies
    )
    handler = AssetHandler(js_dir, config)
    profile.installUrlSchemeHandler(SCHEME, handler)
    cases = ("math", "nomath") if args.case == "both" else (args.case,)
    runs: dict[str, list[dict[str, Any]]] = {case: [] for case in cases}
    for index in range(args.iterations):
        for case, case_runs in runs.items():
            print(f"iteration {index + 1}/{args.iterations}: {case}", file=sys.stderr)
            profile.clearHttpCache()
            case_runs.append(run_page(profile, handler, case))
            app.processEvents()
    machine = {"machine": platform.machine(), "macOS": platform.mac_ver()[0]}
    machine.update(mac_hardware_context())
    machine["qt_webengine"] = qWebEngineVersion()
    machine["qt_chromium"] = qWebEngineChromiumVersion()
    report = {
        "driver": "QtWebEngine fallback (custom URL scheme; no HTTP transfer timing)",
        "artifact_js_dir": str(js_dir),
        "config_override": str(config) if config else None,
        "iterations_per_case": args.iterations,
        "machine": machine,
        "summary": {case: aggregate(items) for case, items in runs.items()},
        "runs": runs,
    }
    encoded = json.dumps(report, indent=2, sort_keys=True)
    print(encoded)
    if args.output:
        args.output.write_text(encoded + "\n")


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    main()
