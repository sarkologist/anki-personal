# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The agent queue window: every thread in the store, reviewed in one place.

A thread list on the left and the selected thread on the right. The detail
is the same ``ThreadPanel`` the editor pane uses, without an editor, so Apply
writes notes through the collection (with the base-revision check) and
Discard/Stop behave exactly as in the pane. Reply, Re-run and Retry send the
thread back to the worker and move on; they never wait for the run. Defer
hides a thread until a chosen time, the settings row changes the provider,
model, effort and card access of its next run, and Apply and edit opens the
applied note in the Browser afterwards.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import aqt
from aqt import gui_hooks
from aqt.qt import (
    QAbstractItemView,
    QAction,
    QByteArray,
    QCheckBox,
    QComboBox,
    QDateTime,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QInputDialog,
    QKeySequence,
    QLabel,
    QMainWindow,
    QMenu,
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
from .card_access import CARD_ACCESS_OPTIONS, normalize_card_access
from .effort_options import (
    effort_choice_for_options,
    effort_option_index,
    effort_options_for,
    effort_options_with_legacy,
)
from .model_options import (
    PROVIDER_OLLAMA,
    PROVIDER_OPTIONS,
    model_options_for,
    provider_value,
)
from .ollama_client import discover_ollama_models, ollama_model_names
from .queue_store import ANSWERED, READY, STALE, QueueTransitionError, Thread
from .surface import surface_body
from .thread_panel import (
    APPLY_ANYWAY_LABEL,
    APPLY_ANYWAY_TIP,
    PromptEdit,
    ThreadPanel,
)
from .thread_view import (
    CLOSED_STATUSES,
    DEFER_CHOICES,
    DEFER_TOMORROW,
    PROVIDER_LABELS,
    REPLYABLE_STATUSES,
    captured_context_fragment,
    created_note_ids,
    defer_choice_time,
    is_deferred,
    merge_thread_settings,
    neighbour_id,
    queue_row,
    queue_threads,
    thread_deck_ids,
    thread_note_ids,
    until_text,
)

DEFAULT_SHORTCUT = "Ctrl+Alt+Shift+Q"
DEFAULT_SIZE = (1100, 720)
DEFAULT_SPLITTER = [380, 720]
COLUMNS = ("", "Request", "Card", "Deck", "Age", "Ver.")
WINDOW_TITLE = "Agent Queue"
QUEUE_NOT_OPEN = "The agent queue is not open yet."
SHORTCUTS = {
    "apply": "Ctrl+Shift+A",
    "apply_edit": "Ctrl+Shift+E",
    "apply_anyway": "Ctrl+Shift+O",
    "rerun": "Ctrl+Shift+N",
    "retry": "Ctrl+Shift+T",
    "reply": "Ctrl+Shift+R",
    "discard": "Ctrl+Shift+D",
    "defer": "Ctrl+Shift+L",
    "browse": "Ctrl+Shift+B",
    "stop": "Ctrl+Shift+S",
}
SETTINGS_SAVED = "The next run of this request uses {provider} {model}."
SELECT_OLLAMA_MODEL = "Select an Ollama model first."

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
    # Deferred threads are not counted (list_threads leaves them out).
    count = len(service.store.list_threads((READY, ANSWERED)))
    if not count:
        return
    _offered = True
    # Anki's tooltip label swallows clicks, so it names the shortcut instead of
    # carrying a link.
    tooltip(offer_text(count), period=6000)


class AgentQueueWindow(ThreadPanel, QMainWindow):
    # Applied/discarded threads cannot be continued from the window.
    _closed_hint = ""

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
        # The thread whose successful apply opens the Browser (Apply and edit).
        self._edit_after_apply: str | None = None
        self._settings_thread: Thread | None = None
        self._filling_settings = False
        self._ollama_models: tuple[str, ...] = ()
        self._ollama_unavailable = False
        self._ollama_discovered = False

    # layout

    def _build_ui(self) -> None:
        left = QWidget()
        left_layout = QVBoxLayout()
        left_layout.setContentsMargins(6, 6, 6, 6)
        left.setLayout(left_layout)
        filters = QHBoxLayout()
        self.note_only_checkbox = QCheckBox("This note only")
        self.show_closed_checkbox = QCheckBox("Show applied and discarded")
        self.show_deferred_checkbox = QCheckBox("Show deferred")
        qconnect(self.note_only_checkbox.toggled, self._on_filter_changed)
        qconnect(self.show_closed_checkbox.toggled, self._on_filter_changed)
        qconnect(self.show_deferred_checkbox.toggled, self._on_filter_changed)
        filters.addWidget(self.note_only_checkbox)
        filters.addWidget(self.show_closed_checkbox)
        filters.addWidget(self.show_deferred_checkbox)
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
        right_layout.addLayout(self._build_settings_row())

        buttons = QHBoxLayout()
        self.apply_button = self._button("apply", "Apply", self._apply)
        self.apply_edit_button = self._button(
            "apply_edit", "Apply and edit", self._apply_and_edit
        )
        self.apply_anyway_button = self._button(
            "apply_anyway", APPLY_ANYWAY_LABEL, self._apply_anyway
        )
        self.apply_anyway_button.setToolTip(
            f"{APPLY_ANYWAY_TIP} ({SHORTCUTS['apply_anyway']})"
        )
        self.apply_anyway_button.setVisible(False)
        self.rerun_button = self._button("rerun", "Re-run", self._rerun_thread)
        self.retry_button = self._button("retry", "Retry", self._retry_thread)
        self.retry_button.setVisible(False)
        self.reply_button = self._button("reply", "Reply", self._reply)
        self.discard_button = self._button("discard", "Discard", self._discard)
        self.defer_button = self._button("defer", "Defer", lambda: None)
        self.defer_button.setMenu(self._build_defer_menu())
        self.browse_button = self._button(
            "browse", "Open in Browser", self._open_in_browser
        )
        self.stop_button = self._button("stop", "Stop", self._stop_running_agent)
        for button in (
            self.apply_button,
            self.apply_edit_button,
            self.apply_anyway_button,
            self.rerun_button,
            self.retry_button,
            self.reply_button,
            self.discard_button,
            self.defer_button,
            self.browse_button,
        ):
            buttons.addWidget(button)
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

    def _build_settings_row(self) -> QHBoxLayout:
        """Provider, model, effort and card access for the thread's next run."""
        row = QHBoxLayout()
        row.addWidget(QLabel("Next run:"))
        self.settings_provider_combo = QComboBox()
        self.settings_model_combo = QComboBox()
        self.settings_effort_combo = QComboBox()
        self.settings_card_access_combo = QComboBox()
        self.settings_provider_combo.setToolTip("Provider")
        self.settings_model_combo.setToolTip("Model")
        self.settings_effort_combo.setToolTip("Reasoning effort")
        self.settings_card_access_combo.setToolTip("Card access")
        qconnect(
            self.settings_provider_combo.currentIndexChanged,
            self._on_settings_provider_changed,
        )
        qconnect(
            self.settings_model_combo.currentIndexChanged,
            self._on_settings_model_changed,
        )
        for combo in (
            self.settings_provider_combo,
            self.settings_model_combo,
            self.settings_effort_combo,
            self.settings_card_access_combo,
        ):
            row.addWidget(combo)
        self.settings_button = QPushButton("Use for next run")
        self.settings_button.setToolTip(
            "Save these settings for this request's next reply, re-run or retry"
        )
        self.settings_button.setEnabled(False)
        qconnect(
            self.settings_button.clicked,
            lambda _checked=False: self._apply_thread_settings(),
        )
        row.addWidget(self.settings_button)
        row.addStretch(1)
        return row

    def _build_defer_menu(self) -> QMenu:
        menu = QMenu(self)
        for label, choice in DEFER_CHOICES:
            action = menu.addAction(label)
            qconnect(
                action.triggered,
                lambda _checked=False, choice=choice: self._defer(choice),
            )
        pick = menu.addAction("Until a date and time…")
        qconnect(pick.triggered, lambda _checked=False: self._defer_pick())
        menu.addSeparator()
        self._undefer_action = menu.addAction("Undefer")
        qconnect(self._undefer_action.triggered, lambda _checked=False: self._undefer())
        return menu

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
            service.store.list_threads(
                include_deferred=self.show_deferred_checkbox.isChecked()
            ),
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
        self.apply_edit_button.setEnabled(
            thread is not None and shown is not None and thread.status != STALE
        )
        open_thread = thread is not None and thread.status not in CLOSED_STATUSES
        self.defer_button.setEnabled(open_thread)
        undefer = getattr(self, "_undefer_action", None)
        if undefer is not None:
            undefer.setEnabled(thread is not None and is_deferred(thread, time.time()))
        self.browse_button.setEnabled(bool(self._note_ids(thread)))
        self._load_thread_settings(thread)

    def _broadcast_thread_changed(self, thread_id: str) -> None:
        self.refresh(select=thread_id, render=True)
        queue_runtime.thread_changed(thread_id, exclude=self)

    def _following_thread_id(self, thread_id: str) -> str | None:
        return neighbour_id([row.id for row in self._rows], thread_id)

    def _after_requeue(self, thread_id: str, following: str | None) -> None:
        # The thread waits in the queue; move on to the next one meanwhile.
        self.refresh(select=following or thread_id, render=True)
        queue_runtime.thread_changed(thread_id, exclude=self)

    def _after_apply(
        self, thread_id: str, updated_ids: list[int], created_ids: list[int]
    ) -> None:
        """Apply and edit: open the applied (or first created) note."""
        if self._edit_after_apply != thread_id:
            return
        self._edit_after_apply = None
        note_ids = updated_ids or created_ids[:1]
        if note_ids:
            search = "nid:" + ",".join(str(note_id) for note_id in note_ids)
            aqt.dialogs.open("Browser", self.mw, search=(search,))

    def on_thread_changed(self, thread_id: str) -> None:
        """Called by the queue's fan-out (runs finishing, pane actions...)."""
        self._sync_multi_proposal_dialog(thread_id)
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
        following = self._following_thread_id(thread.id)
        try:
            service.store.reply(thread.id, text)
        except QueueTransitionError as exc:
            showWarning(str(exc), parent=self)
            return
        self.prompt.clear()
        service.runner.enqueue_kick()
        self._after_requeue(thread.id, following)

    def _apply(self) -> None:
        self._edit_after_apply = None
        self._apply_pending_patch()

    def _apply_and_edit(self) -> None:
        """Apply, then open the note in the Browser for manual touch-ups."""
        self._edit_after_apply = self._thread_id
        self._apply_pending_patch()

    def _apply_anyway(self) -> None:
        self._edit_after_apply = None
        super()._apply_anyway()

    # defer

    def _defer(self, choice: str) -> None:
        self._defer_until(defer_choice_time(choice, time.time()))

    def _defer_pick(self) -> None:
        until = self._pick_defer_time()
        if until is not None:
            self._defer_until(until)

    def _pick_defer_time(self) -> float | None:
        """A date and time from a small dialog, or None if cancelled."""
        dialog = QDialog(self)
        dialog.setWindowTitle("Defer until")
        layout = QVBoxLayout()
        dialog.setLayout(layout)
        edit = QDateTimeEdit(
            QDateTime.fromSecsSinceEpoch(
                int(defer_choice_time(DEFER_TOMORROW, time.time()))
            )
        )
        edit.setCalendarPopup(True)
        edit.setMinimumDateTime(QDateTime.currentDateTime())
        layout.addWidget(edit)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        qconnect(buttons.accepted, dialog.accept)
        qconnect(buttons.rejected, dialog.reject)
        layout.addWidget(buttons)
        if not dialog.exec():  # Rejected is 0
            return None
        return float(edit.dateTime().toSecsSinceEpoch())

    def _defer_until(self, until: float) -> None:
        service = self._service()
        thread = self._selected_thread()
        if service is None or thread is None:
            return
        try:
            service.store.defer(thread.id, until)
        except QueueTransitionError as exc:
            showWarning(str(exc), parent=self)
            return
        tooltip(f"Deferred {until_text(until, time.time())}.", parent=self)
        # Hidden unless deferred threads are shown: the selection moves on.
        self.refresh(render=True)
        queue_runtime.thread_changed(thread.id, exclude=self, notify=False)

    def _undefer(self) -> None:
        service = self._service()
        thread = self._selected_thread()
        if service is None or thread is None:
            return
        try:
            service.store.undefer(thread.id)
        except QueueTransitionError as exc:
            showWarning(str(exc), parent=self)
            return
        self.refresh(select=thread.id, render=True)
        queue_runtime.thread_changed(thread.id, exclude=self, notify=False)

    # change model

    def _load_thread_settings(self, thread: Thread | None) -> None:
        """Prefill the settings row from the shown thread's settings."""
        self._settings_thread = thread
        if thread is None:
            self.settings_button.setEnabled(False)
            return
        settings = thread.settings
        provider = provider_value(settings.get("provider"))
        self._filling_settings = True
        try:
            combo = self.settings_provider_combo
            combo.clear()
            for label, value in PROVIDER_OPTIONS:
                combo.addItem(label, value)
            combo.setCurrentIndex(
                [value for _label, value in PROVIDER_OPTIONS].index(provider)
            )
            self._fill_models(str(settings.get("model") or ""))
            self._fill_efforts(str(settings.get("reasoning_effort") or ""))
            access = normalize_card_access(settings.get("card_access"))
            combo = self.settings_card_access_combo
            combo.clear()
            for label, value in CARD_ACCESS_OPTIONS:
                combo.addItem(label, value)
            combo.setCurrentIndex(
                [value for _label, value in CARD_ACCESS_OPTIONS].index(access)
            )
        finally:
            self._filling_settings = False
        self.settings_button.setEnabled(thread.status not in CLOSED_STATUSES)
        if provider == PROVIDER_OLLAMA:
            self._ensure_ollama_models(settings)

    def _settings_provider(self) -> str:
        return provider_value(self.settings_provider_combo.currentData())

    def _fill_models(self, model: str) -> None:
        options = model_options_for(
            self._settings_provider(),
            model,
            self._ollama_models,
            ollama_unavailable=self._ollama_unavailable,
        )
        combo = self.settings_model_combo
        filling, self._filling_settings = self._filling_settings, True
        try:
            combo.clear()
            for label, value in options:
                combo.addItem(label, value)
            values = [value for _label, value in options]
            combo.setCurrentIndex(values.index(model) if model in values else 0)
        finally:
            self._filling_settings = filling

    def _fill_efforts(self, effort: str) -> None:
        options = effort_options_for(
            self._settings_provider(),
            str(self.settings_model_combo.currentData() or ""),
        )
        effort = effort_choice_for_options(effort, options)
        combo = self.settings_effort_combo
        filling, self._filling_settings = self._filling_settings, True
        try:
            combo.clear()
            for label, value in effort_options_with_legacy(effort, options):
                combo.addItem(label, value)
            combo.setCurrentIndex(effort_option_index(effort, options))
        finally:
            self._filling_settings = filling

    def _on_settings_provider_changed(self, _index: int = 0) -> None:
        if self._filling_settings:
            return
        thread = self._settings_thread
        provider = self._settings_provider()
        same = (
            thread is not None
            and provider_value(thread.settings.get("provider")) == provider
        )
        model = str(thread.settings.get("model") or "") if same and thread else ""
        effort = str(self.settings_effort_combo.currentData() or "")
        self._fill_models(model)
        self._fill_efforts(effort)
        if provider == PROVIDER_OLLAMA and thread is not None:
            self._ensure_ollama_models(thread.settings)

    def _on_settings_model_changed(self, _index: int = 0) -> None:
        if self._filling_settings:
            return
        self._fill_efforts(str(self.settings_effort_combo.currentData() or ""))

    def _ensure_ollama_models(self, settings: dict[str, Any]) -> None:
        if not self._ollama_discovered:
            self._ollama_discovered = True
            self._discover_ollama_models(settings)

    def _discover_ollama_models(self, settings: dict[str, Any]) -> None:
        """Installed Ollama models, in the background (as the pane does)."""
        paths = settings.get("paths") if isinstance(settings.get("paths"), dict) else {}
        config = _config()
        ollama_path = str(paths.get("ollama_path") or config.get("ollama_path") or "")
        ollama_host = str(paths.get("ollama_host") or config.get("ollama_host") or "")

        def task() -> tuple[tuple[str, ...], bool]:
            try:
                models = discover_ollama_models(
                    ollama_path=ollama_path, ollama_host=ollama_host
                )
            except Exception:
                return (), True
            return ollama_model_names(models), False

        def on_done(future: Future) -> None:
            models, unavailable = future.result()
            self._set_ollama_models(models, unavailable=unavailable)

        self.mw.taskman.run_in_background(task, on_done, uses_collection=False)

    def _set_ollama_models(self, models: tuple[str, ...], *, unavailable: bool) -> None:
        self._ollama_models = tuple(models)
        self._ollama_unavailable = unavailable
        if self._settings_provider() != PROVIDER_OLLAMA:
            return
        effort = str(self.settings_effort_combo.currentData() or "")
        self._fill_models(str(self.settings_model_combo.currentData() or ""))
        self._fill_efforts(effort)

    def _apply_thread_settings(self) -> None:
        """Store the row's settings (merged over the thread's) for its next run."""
        service = self._service()
        thread = self._selected_thread()
        if service is None or thread is None:
            return
        provider = self._settings_provider()
        model = str(self.settings_model_combo.currentData() or "")
        if provider == PROVIDER_OLLAMA and not model:
            showWarning(SELECT_OLLAMA_MODEL, parent=self)
            return
        merged = merge_thread_settings(
            thread.settings,
            provider=provider,
            model=model,
            reasoning_effort=str(self.settings_effort_combo.currentData() or ""),
            card_access=normalize_card_access(
                self.settings_card_access_combo.currentData()
            ),
            deck_ids=thread_deck_ids(thread),
        )
        service.store.set_settings(thread.id, merged)
        tooltip(
            SETTINGS_SAVED.format(
                provider=PROVIDER_LABELS.get(provider, provider),
                model=model or "(default model)",
            ),
            parent=self,
        )
        self.refresh(select=thread.id, render=True)
        queue_runtime.thread_changed(thread.id, exclude=self, notify=False)

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
        # Re-checked by the store: a run may have started while the dialog was open.
        self._discard_pending_patch(thread.id, reason)

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
