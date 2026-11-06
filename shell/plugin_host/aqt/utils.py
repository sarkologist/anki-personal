# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Messages go to the shell, which shows them as its own tooltips and
dialogs. Questions that block for an answer aren't supported."""

from __future__ import annotations

import subprocess
from typing import Any

import aqt
from anki.lang import tr_legacyglobal as tr  # noqa: F401


def _window(parent: Any) -> str | None:
    """The shell window an add-on's widget is in (an editor's side panel)."""
    from aqt.editor import EditorWindow

    widget = parent
    while widget is not None:
        if isinstance(widget, EditorWindow):
            return widget.editor.label
        widget = getattr(widget, "_parent", None)
    return None


def tooltip(
    msg: str, period: int = 3000, parent: Any = None, *_args: Any, **_kwargs: Any
) -> None:
    if aqt.mw:
        aqt.mw.native.notify(
            "tooltip", text=str(msg), period=period, window=_window(parent)
        )


def showInfo(text: str, parent: Any = None, *_args: Any, **_kwargs: Any) -> None:
    if aqt.mw:
        aqt.mw.native.notify("show_info", text=str(text))


showText = showInfo
showWarning = showInfo
showCritical = showInfo


def openFolder(path: str) -> None:
    subprocess.run(["open", path], check=False)
