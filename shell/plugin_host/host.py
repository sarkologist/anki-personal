# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The native shell's plugin host: runs add-ons against the shell's open
collection (pylib over a socket, `anki.remote`) with a small `aqt` shim
(this folder). The shell starts it after opening a profile, with
`python -I -u host.py --socket ... --path <pylib> ...`, and restarts it if
it dies. Its output is the shell's plugin log."""

from __future__ import annotations

import argparse
import importlib.machinery
import os
import signal
import socket
import sys
import traceback


class HostPackages:
    """`anki`, `aqt` and `_aqt` come only from the shell's paths (the shim's
    folder first), even if the Python has them installed (an editable
    install's finder would otherwise win over sys.path). There's no
    _rsbridge: the shell's backend does the work."""

    TOP_LEVEL = ("anki", "aqt", "_aqt")

    def __init__(self, paths: list[str]) -> None:
        self.paths = paths

    def find_spec(
        self, name: str, path: object, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if name == "anki._rsbridge":
            raise ImportError("the plugin host uses the shell's backend")
        if name in self.TOP_LEVEL:
            return importlib.machinery.PathFinder.find_spec(name, self.paths)
        return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--base", required=True, help="the Anki base folder")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument("--addon", action="append", default=[])
    parser.add_argument(
        "--path", action="append", default=[], help="where anki and _aqt are"
    )
    parser.add_argument("--lang", default="en")
    return parser.parse_args(argv)


def main(argv: list[str]) -> None:
    args = parse_args(argv)
    sys.meta_path.insert(
        0, HostPackages([os.path.dirname(os.path.abspath(__file__)), *args.path])
    )
    import aqt
    from anki import remote
    from aqt.main import MainWindow, ProfileManager, report_exception
    from aqt.native import Native

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(args.socket)
    native = Native(None)
    addons_folder = os.path.join(args.base, "addons21")
    mw = MainWindow(native, addons_folder)
    native.mw = mw
    aqt.mw = mw
    # the shell going away ends the host
    native.connection = remote.Connection(sock, native.on_request, mw.loop.stop)
    mw.addonManager.on_web_exports = lambda module, pattern: native.notify(
        "web_exports", module=module, pattern=pattern
    )

    # as AddonManager.loadAddons, before the profile opens
    sys.path.insert(0, addons_folder)
    loaded = []
    for module in args.addon:
        if not mw.addonManager.isEnabled(module):
            print(f"plugin host: {module} is disabled in Anki; skipped", flush=True)
            continue
        try:
            __import__(module)
            loaded.append(module)
            print(f"plugin host: loaded {module}", flush=True)
        except Exception:
            report_exception(f"loading {module}")
            native.notify(
                "addon_failed", module=module, error=traceback.format_exc()[-2000:]
            )

    def open_profile() -> None:
        from anki.collection import Collection

        backend = remote.RemoteBackend(native.connection, args.collection)
        backend.use_for_translations(args.lang)
        mw.col = Collection(args.collection, backend=backend)
        mw.pm = ProfileManager(
            args.profile, os.path.dirname(args.collection), args.base
        )
        aqt.gui_hooks.collection_did_load(mw.col)
        aqt.gui_hooks.profile_did_open()
        native.notify("ready", addons=loaded)

    mw.loop.post(open_profile)
    try:
        mw.loop.run()
    finally:
        print("plugin host: exiting", flush=True)
        mw.taskman.shutdown()
        if os.getpgrp() == os.getpid():
            # The shell started it in a group of its own, and kills that
            # group too; but if the shell died, what the add-ons started (a
            # model CLI, say) must still end.
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.killpg(0, signal.SIGTERM)
        # background threads mustn't keep the host alive
        os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
