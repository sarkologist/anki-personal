# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Messages go to the shell, which shows them as its own tooltips and
dialogs. Questions that block for an answer aren't supported."""

from __future__ import annotations

from typing import Any

import aqt
from anki.lang import tr_legacyglobal as tr  # noqa: F401


def tooltip(msg: str, period: int = 3000, *_args: Any, **_kwargs: Any) -> None:
    if aqt.mw:
        aqt.mw.native.notify("tooltip", text=str(msg), period=period)


def showInfo(text: str, *_args: Any, **_kwargs: Any) -> None:
    if aqt.mw:
        aqt.mw.native.notify("show_info", text=str(text))


showText = showInfo
showWarning = showInfo
showCritical = showInfo
