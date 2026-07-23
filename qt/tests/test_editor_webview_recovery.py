# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Tests for the editor webview blank/crash recovery helpers.

These exercise ``Editor.recover_webview_after_crash`` and
``Editor.refresh_web_view_surface`` as unbound methods against lightweight fake
``self`` objects, so no real Qt widgets or ``Editor`` instance are constructed.
"""

from __future__ import annotations

from typing import Any, Callable

from aqt import editor as editor_module
from aqt.editor import Editor


class _FakeProgress:
    def __init__(self) -> None:
        self.scheduled: list[tuple[int, Callable[[], None]]] = []

    def timer(
        self, ms: int, func: Callable[[], None], repeat: bool, parent: Any = None
    ) -> None:
        self.scheduled.append((ms, func))


class _FakeMw:
    def __init__(self) -> None:
        self.progress = _FakeProgress()


def _run_scheduled(mw: _FakeMw) -> None:
    for _ms, func in list(mw.progress.scheduled):
        func()


# recover_webview_after_crash
######################################################################


class _RecoverEditor:
    def __init__(self) -> None:
        self.web: Any = object()
        self.note: Any = object()
        self.currentField: int | None = 1
        self._recovering_webview = False
        self._recent_recovery_times: list[float] = []
        self._recovery_retry_scheduled = False
        self.mw = _FakeMw()
        self.setup_web_calls = 0
        self.load_note_focus: list[int | None] = []

    def setupWeb(self) -> None:
        self.setup_web_calls += 1

    def loadNote(self, focusTo: int | None = None) -> None:
        self.load_note_focus.append(focusTo)

    def recover_webview_after_crash(self) -> None:
        # the scheduled retry calls this on self; delegate to the real method
        Editor.recover_webview_after_crash(self)


def test_recover_reloads_and_preserves_focus_field() -> None:
    editor = _RecoverEditor()

    Editor.recover_webview_after_crash(editor)

    assert editor.setup_web_calls == 1
    assert editor.load_note_focus == [1]


def test_recover_noops_without_web_or_note() -> None:
    editor = _RecoverEditor()
    editor.web = None
    Editor.recover_webview_after_crash(editor)

    editor2 = _RecoverEditor()
    editor2.note = None
    Editor.recover_webview_after_crash(editor2)

    assert editor.setup_web_calls == 0
    assert editor2.setup_web_calls == 0


def test_recover_is_not_reentrant() -> None:
    editor = _RecoverEditor()
    editor._recovering_webview = True

    Editor.recover_webview_after_crash(editor)

    assert editor.setup_web_calls == 0


def test_recover_rate_limits_runaway_crash_loop(monkeypatch) -> None:
    editor = _RecoverEditor()
    now = [1000.0]
    monkeypatch.setattr(editor_module.time, "monotonic", lambda: now[0])

    # three reloads inside the window are allowed, the fourth is suppressed
    for _ in range(4):
        Editor.recover_webview_after_crash(editor)
    assert editor.setup_web_calls == 3

    # once the 30s window has passed, recovery is allowed again
    now[0] += 31
    Editor.recover_webview_after_crash(editor)
    assert editor.setup_web_calls == 4


def test_recover_schedules_single_retry_after_backoff(monkeypatch) -> None:
    editor = _RecoverEditor()
    now = [1000.0]
    monkeypatch.setattr(editor_module.time, "monotonic", lambda: now[0])

    # exhaust the budget; the 4th (suppressed) crash must schedule a retry so a
    # dead renderer — which emits no further signal — is not left blank forever
    for _ in range(4):
        Editor.recover_webview_after_crash(editor)
    assert editor.setup_web_calls == 3
    assert editor._recovery_retry_scheduled is True
    assert len(editor.mw.progress.scheduled) == 1

    # further suppressed crashes coalesce onto the one pending retry
    Editor.recover_webview_after_crash(editor)
    assert len(editor.mw.progress.scheduled) == 1

    # when the retry fires after the window clears, recovery proceeds again
    now[0] += 31
    _ms, retry = editor.mw.progress.scheduled[0]
    retry()
    assert editor.setup_web_calls == 4
    assert editor._recovery_retry_scheduled is False


# refresh_web_view_surface
######################################################################


class _FakeWeb:
    def __init__(self, visible: bool = True) -> None:
        self._visible = visible
        self.hidden = 0
        self.shown = 0
        self.updated = 0
        self.repainted = 0

    def isVisible(self) -> bool:
        return self._visible

    def hide(self) -> None:
        self.hidden += 1

    def show(self) -> None:
        self.shown += 1

    def update(self) -> None:
        self.updated += 1

    def repaint(self) -> None:
        self.repainted += 1


class _FakeWindow:
    def window(self) -> "_FakeWindow":
        return self


class _RefreshEditor:
    def __init__(self, web: Any) -> None:
        self.web = web
        self.mw = _FakeMw()
        self.parentWindow = _FakeWindow()


def test_refresh_noops_when_web_missing() -> None:
    editor = _RefreshEditor(web=None)
    Editor.refresh_web_view_surface(editor)
    assert editor.mw.progress.scheduled == []


def test_refresh_repaints_without_reset_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        editor_module.QApplication, "focusWidget", staticmethod(lambda: None)
    )
    web = _FakeWeb()
    editor = _RefreshEditor(web=web)

    Editor.refresh_web_view_surface(editor)
    _run_scheduled(editor.mw)

    assert (web.updated, web.repainted) == (1, 1)
    assert (web.hidden, web.shown) == (0, 0)


def test_refresh_reset_surface_hides_and_shows(monkeypatch) -> None:
    monkeypatch.setattr(
        editor_module.QApplication, "focusWidget", staticmethod(lambda: None)
    )
    web = _FakeWeb()
    editor = _RefreshEditor(web=web)

    Editor.refresh_web_view_surface(editor, reset_surface=True)
    _run_scheduled(editor.mw)

    assert (web.hidden, web.shown) == (1, 1)
    assert (web.updated, web.repainted) == (1, 1)


def test_refresh_bails_if_web_replaced_before_timer(monkeypatch) -> None:
    monkeypatch.setattr(
        editor_module.QApplication, "focusWidget", staticmethod(lambda: None)
    )
    web = _FakeWeb()
    editor = _RefreshEditor(web=web)

    Editor.refresh_web_view_surface(editor, reset_surface=True)
    # the editor swapped in a new webview (or was torn down) before the repaint
    editor.web = _FakeWeb()
    _run_scheduled(editor.mw)

    assert (web.updated, web.repainted) == (0, 0)
    assert (web.hidden, web.shown) == (0, 0)


def test_refresh_bails_if_web_not_visible(monkeypatch) -> None:
    monkeypatch.setattr(
        editor_module.QApplication, "focusWidget", staticmethod(lambda: None)
    )
    web = _FakeWeb(visible=False)
    editor = _RefreshEditor(web=web)

    Editor.refresh_web_view_surface(editor)
    _run_scheduled(editor.mw)

    assert (web.updated, web.repainted) == (0, 0)
