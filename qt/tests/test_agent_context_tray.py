# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The agent pane's ContextTray widget, in real (offscreen) Qt."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QToolButton

import aqt.webview  # noqa: F401 (QtWebEngine loads before any QApplication)

ADDONS = Path(__file__).resolve().parents[2] / "addons"
if str(ADDONS) not in sys.path:
    sys.path.insert(0, str(ADDONS))

from editor_agent_pane.attachments import Attachment  # noqa: E402

# imported with Qt stubs by test_editor_agent_pane; here with real Qt
QT_MODULES = ("runtime", "queue_runtime", "thread_panel", "queue_window", "queue_badge")


def _drop_qt_modules() -> None:
    package = sys.modules["editor_agent_pane"]
    for name in QT_MODULES:
        sys.modules.pop(f"editor_agent_pane.{name}", None)
        package.__dict__.pop(name, None)


@pytest.fixture(scope="module")
def app() -> QApplication:
    # No windows or interaction with the user's running Anki instance.
    return QApplication.instance() or QApplication(["test", "-platform", "offscreen"])


@pytest.fixture
def thread_panel(app: QApplication) -> Iterator[Any]:
    _drop_qt_modules()
    yield importlib.import_module("editor_agent_pane.thread_panel")
    _drop_qt_modules()


def test_rows_follow_a_removal_and_keep_their_comments(thread_panel: Any) -> None:
    tray = thread_panel.ContextTray()
    first, second = Attachment.transcript("first"), Attachment.transcript("second")
    back = Attachment.field("Back", 1, "back")
    for attachment in (first, second, back):
        tray.add(attachment)
    tray._comments[1].setText("on second")

    tray._rows[0].findChildren(QToolButton)[0].click()
    tray._comments[1].setText("on back")

    assert tray.items == (
        second.with_comment("on second"),
        back.with_comment("on back"),
    )
    assert [c.text() for c in tray._comments] == ["on second", "on back"]


def test_the_tray_hides_when_empty_and_enter_in_a_comment_sends(
    thread_panel: Any,
) -> None:
    sent: list[bool] = []
    tray = thread_panel.ContextTray(on_enter=lambda: sent.append(True))
    assert tray.isHidden()
    tray.add(Attachment.transcript("one"))
    assert not tray.isHidden()

    QTest.keyClick(tray._comments[0], "\r")
    assert sent == [True]

    assert tray.take() == (Attachment.transcript("one"),)
    assert tray.isHidden() and tray._comments == []
