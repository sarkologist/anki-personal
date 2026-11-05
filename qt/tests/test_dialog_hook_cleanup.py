# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Dialogs that register global hooks must unregister them when closed, or the
hook lists keep every closed dialog alive."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from aqt import gui_hooks, mediasync
from aqt.qt import QApplication, QMainWindow, QObject


class _FakeMw(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.deleted: list[QObject] = []

    def deferred_delete_and_garbage_collect(self, obj: QObject) -> None:
        self.deleted.append(obj)


class _Syncer:
    last_progress = ""

    def __init__(self, syncing: bool = False) -> None:
        self.syncing = syncing

    def is_syncing(self) -> bool:
        return self.syncing

    def abort(self) -> None:
        pass


_app = None


def _media_sync_dialog(monkeypatch, syncer: _Syncer, **kwargs: Any):
    global _app
    _app = QApplication.instance() or QApplication(["test", "-platform", "offscreen"])
    monkeypatch.setattr(
        mediasync,
        "tr",
        SimpleNamespace(sync_media_log_title=lambda: "", sync_abort_button=lambda: ""),
    )
    monkeypatch.setattr(
        mediasync.aqt, "dialogs", SimpleNamespace(markClosed=lambda name: None)
    )
    mw = _FakeMw()
    return mw, mediasync.MediaSyncDialog(mw, syncer, **kwargs)  # type: ignore


def _hooked(dialog: mediasync.MediaSyncDialog) -> bool:
    return (
        dialog._on_log_entry in gui_hooks.media_sync_did_progress._hooks
        or dialog._on_start_stop in gui_hooks.media_sync_did_start_or_stop._hooks
    )


def test_media_sync_dialog_releases_hooks_on_close(monkeypatch) -> None:
    mw, dialog = _media_sync_dialog(monkeypatch, _Syncer())
    assert _hooked(dialog)

    dialog.reject()

    assert not _hooked(dialog)
    assert mw.deleted == [dialog]


def test_media_sync_dialog_closing_when_done_releases_hooks_once(
    monkeypatch,
) -> None:
    mw, dialog = _media_sync_dialog(monkeypatch, _Syncer(), close_when_done=True)

    dialog._on_start_stop(False)

    assert not _hooked(dialog)
    assert mw.deleted == [dialog]


def test_media_sync_dialog_keeps_hooks_while_abort_pending(monkeypatch) -> None:
    # closing during a close-when-done sync only starts an abort
    mw, dialog = _media_sync_dialog(
        monkeypatch, _Syncer(syncing=True), close_when_done=True
    )

    dialog.reject()

    assert _hooked(dialog)
    assert mw.deleted == []


def test_addon_config_editor_releases_help_webview_on_close(monkeypatch) -> None:
    # The help pane is an AnkiWebView, whose theme hooks pin it until cleanup().
    from aqt import addons

    cleaned: list[str] = []
    mw = SimpleNamespace(deleted=[])
    mw.deferred_delete_and_garbage_collect = mw.deleted.append
    monkeypatch.setattr(addons.aqt, "mw", mw)
    monkeypatch.setattr(addons, "saveGeom", lambda *args: None)
    monkeypatch.setattr(addons, "saveSplitter", lambda *args: None)
    editor = SimpleNamespace(
        form=SimpleNamespace(
            help=SimpleNamespace(cleanup=lambda: cleaned.append("help")),
            splitter=None,
        )
    )

    addons.ConfigEditor.onClose(editor)  # type: ignore

    assert cleaned == ["help"]
    assert mw.deleted == [editor]
