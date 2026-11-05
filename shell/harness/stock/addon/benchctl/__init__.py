# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Benchmark control: run Python on Anki's main thread over HTTP (localhost).

Requests must carry the X-Bench-Token header with the BENCH_TOKEN that
bench.py set, and no Origin header, so web pages in the user's browser can't
use it to run code."""
import ctypes
import ctypes.util
import json
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer

from aqt import gui_hooks, mw


def _accessory_policy() -> None:
    # keep this instance from taking focus / the Dock from the user
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p
    send = objc.objc_msgSend
    send.restype = ctypes.c_void_p
    send.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    app = send(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    send.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
    send(app, objc.sel_registerName(b"setActivationPolicy:"), 1)


def _run_on_main(code: str) -> dict:
    done = threading.Event()
    result: dict = {}

    def run() -> None:
        scope = {"mw": mw, "result": None}
        try:
            exec(code, scope)
            result["result"] = repr(scope.get("result"))
        except Exception:
            result["error"] = traceback.format_exc()
        done.set()

    mw.taskman.run_on_main(run)
    done.wait(60)
    return result


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        import os

        token = os.environ.get("BENCH_TOKEN")
        if (
            not token
            or self.headers.get("X-Bench-Token") != token
            or self.headers.get("Origin") is not None
        ):
            self.send_response(403)
            self.end_headers()
            return
        code = self.rfile.read(int(self.headers["Content-Length"])).decode()
        body = json.dumps(_run_on_main(code)).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def _start() -> None:
    _accessory_policy()
    server = HTTPServer(("127.0.0.1", 8790), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


gui_hooks.profile_did_open.append(_start)


def _initial_geometry() -> None:
    import os

    if geom := os.environ.get("BENCH_GEOM"):
        mw.setGeometry(*map(int, geom.split(",")))


gui_hooks.main_window_did_init.append(_initial_geometry)
gui_hooks.profile_did_open.append(_initial_geometry)
