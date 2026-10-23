# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The agent queue window: every thread in the store, reviewed in one place.

A thread list on the left and the selected thread on the right. The detail
is the same ``ThreadPanel`` the editor pane uses, without an editor, so Apply
writes notes through the collection (with the base-revision check) and
Discard/Stop behave exactly as in the pane. Reply sends the thread back to
the worker and moves on; it never waits for the run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import aqt
from aqt import gui_hooks
from aqt.qt import (
    QAbstractItemView,
    QAction,
    QByteArray,
    QCheckBox,
    QHBoxLayout,
    QInputDialog,
    QKeySequence,
    QMainWindow,
    QPushButton,
    QShortcut,
    QSplitter,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import showWarning, tooltip
from aqt.webview import AnkiWebView

from . import queue_runtime
from .queue_store import ANSWERED, READY, QueueTransitionError, Thread
from .surface import surface_body
from .thread_panel import PromptEdit, ThreadPanel
from .thread_view import (
    REPLYABLE_STATUSES,
    captured_context_fragment,
    created_note_ids,
    neighbour_id,
    queue_row,
    queue_threads,
    thread_note_ids,
)

DEFAULT_SHORTCUT = "Ctrl+Alt+Shift+Q"
DEFAULT_SIZE = (1100, 720)
DEFAULT_SPLITTER = [380, 720]
COLUMNS = ("", "Request", "Card", "Deck", "Age", "Ver.")
WINDOW_TITLE = "Agent Queue"
QUEUE_NOT_OPEN = "The agent queue is not open yet."
SHORTCUTS = {
    "apply": "Ctrl+Shift+A",
    "reply": "Ctrl+Shift+R",
    "discard": "Ctrl+Shift+D",
    "browse": "Ctrl+Shift+B",
    "stop": "Ctrl+Shift+S",
}

ConfigGetter = Callable[[], dict[str, Any]]
ConfigWriter = Callable[[dict[str, Any]], None]

_config: ConfigGetter = dict
_write_config: ConfigWriter = lambda _config: None  # noqa: E731
_window: AgentQueueWindow | None = None
# Offered at most once per profile session (see _on_state_did_change).
_offered = False
_installed = False


def install(*, config: ConfigGetter, write_config: ConfigWriter) -> None:
    global _installed, _config, _write_config
    _config, _write_config = config, write_config
    if _installed:
        return
    _installed = True
    gui_hooks.state_did_change.append(_on_state_did_change)
    gui_hooks.profile_did_open.append(_on_profile_did_open)
    gui_hooks.profile_will_close.append(close_window)
    _add_menu_action()


def shortcut() -> str:
    return str(_config().get("queue_shortcut") or DEFAULT_SHORTCUT)


def _add_menu_action() -> None:
    """Tools > Agent Queue, with an application-wide shortcut."""
    mw = aqt.mw
    menu = getattr(getattr(mw, "form", None), "menuTools", None)
    if mw is None or menu is None:
        return
    action = QAction(WINDOW_TITLE, mw)
    action.setShortcut(QKeySequence(shortcut()))
    # Reachable from the editor and the Browser too, not just the main window.
    action.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
    qconnect(action.triggered, lambda _checked=False: open_window())
    menu.addAction(action)


def open_window(note_id: int | None = None) -> AgentQueueWindow | None:
    """Show the queue window (one per main window), filtered to ``note_id``."""
    global _window
    mw = aqt.mw
    if mw is None:
        return None
    if queue_runtime.service() is None:
        showWarning(QUEUE_NOT_OPEN)
        return None
    if _window is None:
        _window = AgentQueueWindow(mw, note_id=note_id)
    else:
        _window.set_note_filter(note_id)
    _window.show()
    _window.raise_()
    _window.activateWindow()
    return _window


def open_windows() -> list[AgentQueueWindow]:
    """For queue_runtime's fan-out of thread changes."""
    return [_window] if _window is not None else []


def close_window() -> None:
    if _window is not None:
        _window.close()


def _on_profile_did_open() -> None:
    global _offered
    _offered = False


def offer_text(count: int) -> str:
    noun = "request" if count == 1 else "requests"
    return f"Agent queue: {count} {noun} ready for review ({shortcut()})"


def _on_state_did_change(new_state: str, old_state: str) -> None:
    """Leaving the reviewer with results waiting: say so, once per session."""
    global _offered
    if _offered or old_state != "review" or new_state == "review":
        return
    service = queue_runtime.service()
    if service is None:
        return
    count = len(service.store.list_threads((READY, ANSWERED)))
    if not count:
        return
    _offered = True
    # Anki's tooltip label swallows clicks, so it names the shortcut instead of
    # carrying a link.
    tooltip(offer_text(count), period=6000)


class AgentQueueWindow(ThreadPanel, QMainWindow):
    def __init__(self, mw: Any, *, note_id: int | None = None) -> None:
        QMainWindow.__init__(self, mw, Qt.WindowType.Window)
        self._init_state(mw, note_id=note_id)
        self.setWindowTitle(WINDOW_TITLE)
        self._build_ui()
        self._restore_layout()
        self.set_note_filter(note_id)

    def _init_state(self, mw: Any, *, note_id: int | None) -> None:
        self.mw = mw
        self.editor = None
        self.browser = None
        self._thread_id: str | None = None
        self._last_rendered: tuple[str, str] | None = None
        self._activity_id: str | None = None
        self._activity_open = False
        self._multi_proposal_dialog = None
        self._note_filter = note_id
        self._rows: list[Thread] = []
        self._cells: list[tuple[str, ...]] = []
        self._rendered_id: str | None = None
        self._populating = False

    # layout

    def _build_ui(self) -> None:
        left = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(6, 6, 6, 6)
        left.setLayout(left_layout)
        filters = QHBoxLayout()
        self.note_only_checkbox = QCheckBox("This note only")
        self.show_closed_checkbox = QCheckBox("Show applied and discarded")
        qconnect(self.note_only_checkbox.toggled, self._on_filter_changed)
        qconnect(self.show_closed_checkbox.toggled, self._on_filter_changed)
        filters.addWidget(self.note_only_checkbox)
        filters.addWidget(self.show_closed_checkbox)
        filters.addStretch(1)
        left_layout.addLayout(filters)

        table = QTableWidget(0, len(COLUMNS))
        table.setHorizontalHeaderLabels(list(COLUMNS))
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)
        vertical = table.verticalHeader()
        if vertical is not None:
            vertical.hide()
        header = table.horizontalHeader()
        if header is not None:
            header.setStretchLastSection(False)
            for column, width in enumerate((28, 220, 160, 100, 44, 40)):
                table.setColumnWidth(column, width)
        qconnect(table.itemSelectionChanged, self._on_table_selection_changed)
        self.table = table
        left_layout.addWidget(table, 1)

        right = QWidget()
        right_layout = QVBoxLayout()
        right_layout.setContentsMargins(6, 6, 6, 6)
        right.setLayout(right_layout)
        self.surface = AnkiWebView(self)
        self.surface.stdHtml(
            surface_body(),
            js=["js/mathjax.js", "js/vendor/mathjax/tex-chtml-full.js"],
            context=self,
        )
        self.surface.set_bridge_command(self._on_bridge_cmd, self)
        right_layout.addWidget(self.surface, 1)
        self.prompt = PromptEdit(self._reply, lambda: (), self)
        self.prompt.setPlaceholderText("Reply (Enter sends; the agent runs it next)")
        self.prompt.setMaximumHeight(90)
        right_layout.addWidget(self.prompt)

        buttons = QHBoxLayout()
        self.apply_button = self._button("apply", "Apply", self._apply_pending_patch)
        self.reply_button = self._button("reply", "Reply", self._reply)
        self.discard_button = self._button("discard", "Discard", self._discard)
        self.browse_button = self._button(
            "browse", "Open in Browser", self._open_in_browser
        )
        self.stop_button = self._button("stop", "Stop", self._stop_running_agent)
        for button in (self.apply_button, self.reply_button, self.discard_button):
            buttons.addWidget(button)
        buttons.addWidget(self.browse_button)
        buttons.addStretch(1)
        buttons.addWidget(self.stop_button)
        right_layout.addLayout(buttons)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setStretchFactor(1, 1)
        self.setCentralWidget(self.splitter)
        close = QShortcut(QKeySequence("Escape"), self)
        qconnect(close.activated, self.close)

    def _button(self, name: str, label: str, slot: Callable[[], None]) -> QPushButton:
        button = QPushButton(label)
        button.setShortcut(QKeySequence(SHORTCUTS[name]))
        button.setToolTip(f"{label} ({SHORTCUTS[name]})")
        button.setEnabled(False)
        qconnect(button.clicked, lambda _checked=False: slot())
        return button

    def _restore_layout(self) -> None:
        config = _config()
        geometry = str(config.get("queue_window_geometry") or "")
        restored = False
        if geometry:
            try:
                restored = bool(
                    self.restoreGeometry(QByteArray.fromBase64(geometry.encode()))
                )
            except Exception:
                restored = False
        if not restored:
            self.resize(*DEFAULT_SIZE)
        sizes = config.get("queue_window_splitter")
        valid = (
            isinstance(sizes, list)
            and len(sizes) == 2
            and all(isinstance(size, int) and size > 0 for size in sizes)
        )
        self.splitter.setSizes(list(sizes) if valid else list(DEFAULT_SPLITTER))

    def _save_layout(self) -> None:
        config = _config()
        config["queue_window_geometry"] = bytes(
            self.saveGeometry().toBase64().data()
        ).decode()
        config["queue_window_splitter"] = [int(size) for size in self.splitter.sizes()]
        _write_config(config)

    def closeEvent(self, event: Any) -> None:
        global _window
        try:
            self._save_layout()
        except Exception:
            pass
        if _window is self:
            _window = None
        super().closeEvent(event)

    # the list

    def set_note_filter(self, note_id: int | None) -> None:
        """Show ``note_id``'s threads (with a toggle to show all), or all."""
        self._note_filter = note_id
        self.note_only_checkbox.setVisible(note_id is not None)
        if note_id is not None:
            self.note_only_checkbox.setText(f"Note {note_id} only")
            self.note_only_checkbox.setChecked(True)
        self.refresh()

    def _on_filter_changed(self, _checked: bool = False) -> None:
        self.refresh()

    def _visible_threads(self) -> list[Thread]:
        service = self._service()
        if service is None:
            return []
        note_id = (
            self._note_filter
            if self._note_filter is not None and self.note_only_checkbox.isChecked()
            else None
        )
        return queue_threads(
            service.store.list_threads(),
            include_closed=self.show_closed_checkbox.isChecked(),
            note_id=note_id,
        )

    def refresh(self, select: str | None = None, *, render: bool = False) -> None:
        """Reload the list and keep (or move) the selection.

        A selected thread that dropped out of the list (applied, discarded)
        hands the selection to the row that followed it. The detail is
        re-rendered only when the selection changes or ``render`` is set.
        """
        previous = [thread.id for thread in self._rows]
        self._rows = self._visible_threads()
        ids = [thread.id for thread in self._rows]
        wanted = select if select is not None else self._thread_id
        if wanted not in ids:
            wanted = self._replacement(previous, ids, wanted)
        self._populate_table()
        self._select(wanted, render=render)

    @staticmethod
    def _replacement(
        previous: list[str], ids: list[str], gone: str | None
    ) -> str | None:
        if gone in previous:
            index = previous.index(gone)
            for candidate in previous[index + 1 :] + previous[:index][::-1]:
                if candidate in ids:
                    return candidate
        return ids[0] if ids else None

    def _populate_table(self) -> None:
        service = self._service()
        now = time.time()
        decks: dict[int, str] = {}
        self._cells = []
        for thread in self._rows:
            proposals = service.store.proposals(thread.id) if service else []
            version = proposals[-1].version if proposals else None
            self._cells.append(
                queue_row(
                    thread,
                    deck=self._deck_name(thread, decks),
                    version=version,
                    now=now,
                )
            )
        self._populating = True
        blocked = self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self._cells))
            for row, cells in enumerate(self._cells):
                for column, text in enumerate(cells):
                    self.table.setItem(row, column, self._item(text))
        finally:
            self.table.blockSignals(blocked)
            self._populating = False

    @staticmethod
    def _item(text: str) -> Any:
        item = QTableWidgetItem(text)
        try:
            item.setToolTip(text)
        except AttributeError:
            pass
        return item

    def _deck_name(self, thread: Thread, cache: dict[int, str]) -> str:
        deck_id = thread.context.get("deck_id")
        if deck_id is None:
            cards = (thread.context.get("snapshot") or {}).get("cards") or ()
            return str(cards[0].get("deck_name") or "") if cards else ""
        if deck_id not in cache:
            col = self._col()
            try:
                cache[deck_id] = str(col.decks.name(deck_id)) if col else ""
            except Exception:
                cache[deck_id] = ""
        return cache[deck_id]

    def _select(self, thread_id: str | None, *, render: bool = False) -> None:
        self._thread_id = thread_id
        ids = [thread.id for thread in self._rows]
        blocked = self.table.blockSignals(True)
        try:
            if thread_id in ids:
                self.table.selectRow(ids.index(thread_id))
            else:
                self.table.clearSelection()
        finally:
            self.table.blockSignals(blocked)
        if render or thread_id != self._rendered_id:
            self._rendered_id = thread_id
            self._render_selected_thread()

    def _on_table_selection_changed(self) -> None:
        if self._populating:
            return
        self._on_row_selected(self.table.currentRow())

    def _on_row_selected(self, row: int) -> None:
        if 0 <= row < len(self._rows):
            self._select(self._rows[row].id)

    # ThreadPanel hooks

    def _context_fragment(self, thread: Thread) -> str | None:
        return captured_context_fragment(thread)

    def _after_thread_rendered(
        self,
        thread: Thread | None,
        previous: tuple[str, str] | None,
        snapshot: Any,
        shown: Any,
    ) -> None:
        replyable = thread is not None and thread.status in REPLYABLE_STATUSES
        self.reply_button.setEnabled(replyable)
        self.browse_button.setEnabled(bool(self._note_ids(thread)))

    def _broadcast_thread_changed(self, thread_id: str) -> None:
        self.refresh(select=thread_id, render=True)
        queue_runtime.thread_changed(thread_id, exclude=self)

    def on_thread_changed(self, thread_id: str) -> None:
        """Called by the queue's fan-out (runs finishing, pane actions...)."""
        self.refresh(render=thread_id == self._thread_id)

    # actions

    def _reply(self) -> None:
        text = self.prompt.toPlainText().strip()
        if not text:
            self.prompt.setFocus()
            return
        service = self._service()
        thread = self._selected_thread()
        if service is None or thread is None:
            return
        if thread.status not in REPLYABLE_STATUSES:
            showWarning("This request is still running or already closed.", parent=self)
            return
        following = neighbour_id([row.id for row in self._rows], thread.id)
        try:
            service.store.reply(thread.id, text)
        except QueueTransitionError as exc:
            showWarning(str(exc), parent=self)
            return
        self.prompt.clear()
        service.runner.enqueue_kick()
        # The thread waits in the queue; move on to the next one meanwhile.
        self.refresh(select=following or thread.id, render=True)
        queue_runtime.thread_changed(thread.id, exclude=self)

    def _ask_discard_reason(self) -> str | None:
        """An optional reason, or None if the user cancelled."""
        text, ok = QInputDialog.getText(
            self, "Discard", "Why discard it? (optional, kept with the request)"
        )
        return str(text).strip() if ok else None

    def _discard(self) -> None:
        thread = self._selected_thread()
        service = self._service()
        if thread is None or service is None:
            return
        reason = self._ask_discard_reason()
        if reason is None:
            return
        if reason:
            service.store.add_message(thread.id, "user", f"Discarded: {reason}")
        self._discard_pending_patch(thread.id)

    def _note_ids(self, thread: Thread | None) -> list[int]:
        service = self._service()
        if thread is None or service is None:
            return []
        ids = thread_note_ids(thread)
        ids += [
            note_id
            for note_id in created_note_ids(service.store.proposals(thread.id))
            if note_id not in ids
        ]
        return ids

    def _open_in_browser(self) -> None:
        note_ids = self._note_ids(self._selected_thread())
        if not note_ids:
            return
        search = "nid:" + ",".join(str(note_id) for note_id in note_ids)
        aqt.dialogs.open("Browser", self.mw, search=(search,))
