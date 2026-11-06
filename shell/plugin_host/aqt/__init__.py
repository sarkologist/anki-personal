# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""A small `aqt` for add-ons in the native shell's plugin host (see
`native-shell-port-plan.md`, "Plugin host"). Collection code works as in
Anki: `mw.col` is pylib on the shell's open collection. There are no Qt
widgets; aqt.native has the shell's web panels instead."""

from __future__ import annotations

from typing import Any

# before the hooks, whose imports only work once it's loaded (as in aqt)
import anki.collection  # noqa: F401
from anki.lang import tr_legacyglobal as tr  # noqa: F401

from . import gui_hooks  # noqa: F401
from .main import MainWindow

mw: MainWindow | None = None


class DialogManager:
    def open(self, name: str, *_args: Any, **kwargs: Any) -> None:
        assert mw
        if name == "Browser":
            search = kwargs.get("search") or ()
            mw.native.notify("browser_search", search=" ".join(str(s) for s in search))
        else:
            mw.native.notify("show_info", text=f"{name} isn't in the native shell yet.")


dialogs = DialogManager()
