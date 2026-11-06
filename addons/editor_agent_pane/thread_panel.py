# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Showing and acting on one queue thread, shared by the pane and the queue window.

``ThreadPanel`` is a mixin for a widget that owns a transcript ``surface``
(an ``AnkiWebView`` with ``surface_body()``) and Apply/Discard/Stop buttons.
It renders the selected thread from the store and implements Apply, Discard
and Stop, including the base-revision check. The host supplies ``editor``
(the editor whose note an update is filled into, or None to write the note
through the collection), ``browser`` (the Browser of a multi-card pane, or
None) and ``_broadcast_thread_changed``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import aqt
from aqt.operations import CollectionOp
from aqt.operations.note import update_notes
from aqt.qt import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    Qt,
    QTextCursor,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import showWarning, tooltip
from aqt.webview import AnkiWebView

from . import queue_runtime
from .latex_preview import LegacyLatexPreviewRenderer
from .patches import (
    EditorSnapshot,
    FieldSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    NoteCreate,
    NotePatch,
    PatchValidationError,
    SelectedCardSnapshot,
)
from .preview import PartialApplyError, apply_proposal_changes, create_preview
from .queue_runner import refresh_snapshot, snapshot_note_ids
from .queue_store import (
    APPLIED,
    STALE,
    Proposal,
    QueueTransitionError,
    Thread,
)
from .snapshots import Patch, Snapshot, patch_from_dict, snapshot_from_dict
from .surface import (
    CreatePreview,
    js_append_to_activity,
    js_append_transcript,
    js_clear_proposal,
    js_clear_transcript,
    js_set_proposal,
    multi_note_patch_card_ids,
    render_activity_line,
    render_creates_proposal,
    render_multi_note_card_proposal_diff,
    render_proposal_diff,
    surface_body,
)
from .thread_view import (
    ACTIVE_STATUSES,
    APPLICABLE_STATUSES,
    DISCARDABLE_STATUSES,
    LIVE_ACTIVITY_ID,
    PANE_CLOSED_HINT,
    RERUNNABLE_STATUSES,
    RETRYABLE_STATUSES,
    created_note_ids,
    created_notes_fragment,
    parse_open_note_command,
    selected_text_of,
    stale_reason,
    status_fragment,
    transcript_fragments,
)


class PromptEdit(QPlainTextEdit):
    def __init__(
        self,
        send_callback: Callable[[], None],
        history_callback: Callable[[], tuple[str, ...]],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._send_callback = send_callback
        self._history_callback = history_callback
        self._history_index: int | None = None
        self._draft_text: str | None = None

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._reset_history_navigation()
            self._send_callback()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up and self._cursor_on_first_line():
            if self._show_previous_history_item():
                event.accept()
                return
        if event.key() == Qt.Key.Key_Down and self._cursor_on_last_line():
            if self._show_next_history_item():
                event.accept()
                return
        if event.key() not in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._reset_history_navigation()
        super().keyPressEvent(event)

    def _cursor_on_first_line(self) -> bool:
        return self.textCursor().blockNumber() == 0

    def _cursor_on_last_line(self) -> bool:
        return self.textCursor().blockNumber() == self.document().blockCount() - 1

    def _show_previous_history_item(self) -> bool:
        history = self._history_callback()
        if not history:
            return False
        if self._history_index is None:
            self._draft_text = self.toPlainText()
            self._history_index = 0
        elif self._history_index < len(history) - 1:
            self._history_index += 1
        self._replace_text(history[self._history_index])
        return True

    def _show_next_history_item(self) -> bool:
        if self._history_index is None:
            return False
        history = self._history_callback()
        if self._history_index > 0 and self._history_index <= len(history):
            self._history_index -= 1
            self._replace_text(history[self._history_index])
            return True
        draft = self._draft_text or ""
        self._reset_history_navigation()
        self._replace_text(draft)
        return True

    def _replace_text(self, text: str) -> None:
        self.setPlainText(text)
        self.moveCursor(QTextCursor.MoveOperation.End)

    def _reset_history_navigation(self) -> None:
        self._history_index = None
        self._draft_text = None


class MultiCardProposalDialog(QDialog):
    def __init__(
        self,
        *,
        parent: QWidget,
        snapshot: MultiCardSnapshot,
        patch: MultiNotePatch,
        apply_callback: Callable[[set[int]], bool],
        discard_callback: Callable[[], object],
        create_previews: list[CreatePreview | None] | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.snapshot = snapshot
        self.patch = patch
        self.create_previews = create_previews
        self.apply_callback = apply_callback
        self.discard_callback = discard_callback
        self.cards = tuple(
            card
            for card in snapshot.cards
            if patch.update_for_note(card.note_id) is not None
        )
        self.index = 0
        self.checked_note_ids = set(patch.affected_note_ids())

        self.setWindowTitle("Agent proposal")
        self.resize(1000, 720)
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.setLayout(layout)

        top_row = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.page_label = QLabel("")
        qconnect(self.previous_button.clicked, self._previous_card)
        qconnect(self.next_button.clicked, self._next_card)
        top_row.addWidget(self.previous_button)
        top_row.addWidget(self.next_button)
        top_row.addWidget(self.page_label, 1)
        layout.addLayout(top_row)

        self.apply_note_checkbox = QCheckBox("")
        qconnect(self.apply_note_checkbox.toggled, self._on_note_checked)
        layout.addWidget(self.apply_note_checkbox)

        self.surface = AnkiWebView(self)
        self.surface.stdHtml(
            surface_body(),
            js=[
                "js/mathjax.js",
                "js/vendor/mathjax/tex-chtml-full.js",
            ],
            context=self,
        )
        layout.addWidget(self.surface, 1)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton(
            "Apply checked",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.discard_button = buttons.addButton(
            "Discard",
            QDialogButtonBox.ButtonRole.RejectRole,
        )
        assert self.apply_button is not None
        assert self.discard_button is not None
        qconnect(self.apply_button.clicked, self._apply_checked)
        qconnect(self.discard_button.clicked, self._discard)
        layout.addWidget(buttons)

        self._show_current_card()

    def _current_card(self) -> SelectedCardSnapshot:
        return self.cards[self.index]

    def _show_current_card(self) -> None:
        if not self.cards:
            self.page_label.setText("No affected cards")
            self.apply_note_checkbox.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.surface.eval(js_clear_proposal())
            return

        card = self._current_card()
        note_cards = self.snapshot.cards_for_note(card.note_id)
        self.page_label.setText(f"Card {self.index + 1} of {len(self.cards)}")
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < len(self.cards) - 1)
        self.apply_note_checkbox.blockSignals(True)
        self.apply_note_checkbox.setText(
            f"Apply changes to note {card.note_id} "
            f"({len(note_cards)} selected card{'s' if len(note_cards) != 1 else ''})"
        )
        self.apply_note_checkbox.setChecked(card.note_id in self.checked_note_ids)
        self.apply_note_checkbox.blockSignals(False)
        self.surface.eval(
            js_set_proposal(
                render_multi_note_card_proposal_diff(
                    self.snapshot,
                    self.patch,
                    card.card_id,
                    create_previews=self.create_previews,
                )
            )
        )

    def _previous_card(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._show_current_card()

    def _next_card(self) -> None:
        if self.index < len(self.cards) - 1:
            self.index += 1
            self._show_current_card()

    def _on_note_checked(self, checked: bool) -> None:
        card = self._current_card()
        if checked:
            self.checked_note_ids.add(card.note_id)
        else:
            self.checked_note_ids.discard(card.note_id)

    def _apply_checked(self) -> None:
        if self.apply_callback(set(self.checked_note_ids)):
            self.accept()

    def _discard(self) -> None:
        self.discard_callback()
        self.reject()


PROPOSAL_CHANGED = (
    "The proposal changed while this window was open. Review the new version."
)
NOT_ADDED_YET = (
    "This proposal changes a note that has not been added yet. Apply it from "
    "the Add Cards window it was requested in."
)
APPLY_ANYWAY_LABEL = "Apply anyway"
APPLY_ANYWAY_TIP = (
    "Apply the proposal although the note changed since it was made: the "
    "proposed fields and tags overwrite the current ones"
)


class ThreadPanel:
    """Mixin: render the selected queue thread and apply/discard/stop it."""

    editor: Any = None
    browser: Any = None
    surface: Any
    apply_button: Any
    discard_button: Any
    stop_button: Any
    # Optional: a host without them simply does not offer the action.
    apply_anyway_button: Any = None
    rerun_button: Any = None
    retry_button: Any = None
    # Follows "[Applied." / "[Discarded." in the status banner.
    _closed_hint = PANE_CLOSED_HINT
    _thread_id: str | None
    _last_rendered: tuple[str, str] | None
    _activity_id: str | None
    _activity_open: bool
    _multi_proposal_dialog: MultiCardProposalDialog | None
    # (thread id, proposal version) the open multi-card dialog shows.
    _multi_proposal_key: tuple[str, int] | None = None

    def _broadcast_thread_changed(self, thread_id: str) -> None:
        raise NotImplementedError

    def _context_fragment(self, thread: Thread) -> str | None:
        """Shown above the transcript; the pane already shows its note."""
        return None

    def _after_thread_rendered(
        self,
        thread: Thread | None,
        previous: tuple[str, str] | None,
        snapshot: Snapshot | None,
        shown: Patch | None,
    ) -> None:
        pass

    def _after_apply(
        self, thread_id: str, updated_ids: list[int], created_ids: list[int]
    ) -> None:
        """Called once an apply's collection write succeeded."""

    def _following_thread_id(self, thread_id: str) -> str | None:
        """Where the selection goes after the thread is queued again (window)."""
        return None

    def _after_requeue(self, thread_id: str, following: str | None) -> None:
        self._broadcast_thread_changed(thread_id)

    def _append_transcript(self, fragment: str) -> None:
        self.surface.eval(js_append_transcript(fragment))

    def _append_activity_line(self, line: str) -> None:
        if self._activity_open and self._activity_id:
            self.surface.eval(
                js_append_to_activity(self._activity_id, render_activity_line(line))
            )

    def _clear_proposal(self) -> None:
        self.surface.eval(js_clear_proposal())

    def _set_proposal(
        self,
        snapshot: EditorSnapshot,
        patch: NotePatch,
        notetype: dict[str, Any],
    ) -> None:
        renderer = LegacyLatexPreviewRenderer(col=self._col(), notetype=notetype)
        self.surface.eval(
            js_set_proposal(
                render_proposal_diff(
                    snapshot,
                    patch,
                    renderer.render,
                    create_previews=self._create_previews(patch.note_creates),
                )
            )
        )

    def _col(self) -> Any:
        editor = getattr(self, "editor", None)
        if editor is not None:
            return getattr(editor.mw, "col", None)
        browser = getattr(self, "browser", None)
        if browser is not None:
            return getattr(browser, "col", None)
        mw = aqt.mw
        return getattr(mw, "col", None) if mw is not None else None

    def _create_previews(
        self, creates: tuple[NoteCreate, ...]
    ) -> list[CreatePreview | None]:
        """Rendered cards and duplicate hits for proposed notes, at render time."""
        if not creates:
            return []
        col = self._col()
        previews: list[CreatePreview | None] = []
        for create in creates:
            try:
                previews.append(create_preview(col, create) if col else None)
            except Exception:
                previews.append(None)
        return previews

    def _on_bridge_cmd(self, cmd: str) -> Any:
        note_id = parse_open_note_command(cmd)
        if note_id is None:
            return None
        mw = aqt.mw
        if mw is not None:
            aqt.dialogs.open("Browser", mw, search=(f"nid:{note_id}",))
        return None

    def _set_multi_proposal(
        self,
        snapshot: MultiCardSnapshot,
        patch: MultiNotePatch,
    ) -> None:
        card_ids = multi_note_patch_card_ids(snapshot, patch)
        previews = self._create_previews(patch.note_creates)
        if not card_ids:
            if patch.note_creates:
                self.surface.eval(
                    js_set_proposal(
                        render_creates_proposal(patch, create_previews=previews)
                    )
                )
                return
            self._clear_proposal()
            self.apply_button.setEnabled(False)
            return
        self.surface.eval(
            js_set_proposal(
                render_multi_note_card_proposal_diff(
                    snapshot,
                    patch,
                    card_ids[0],
                    create_previews=previews,
                )
            )
        )
        self.apply_button.setText("Review proposal")

    # queue store access

    def _service(self) -> queue_runtime.QueueService | None:
        return queue_runtime.service()

    def _selected_thread(self) -> Thread | None:
        service = self._service()
        if service is None or self._thread_id is None:
            return None
        return service.store.get_thread(self._thread_id)

    def _thread_snapshot(self, thread: Thread) -> Snapshot | None:
        try:
            return snapshot_from_dict(thread.context["snapshot"])
        except Exception:
            return None

    def _read_note_state(self, note_ids: list[int]) -> dict[int, dict[str, Any] | None]:
        return queue_runtime.read_note_state(note_ids)

    def _note_states(self, note_ids: list[int]) -> dict[int, dict[str, Any] | None]:
        """Current note state: mod from the collection, and for the note in the
        editor its fields and tags as typed (which may not be saved yet)."""
        if not note_ids:
            return {}
        states = dict(self._read_note_state(note_ids))
        note = self.editor.note if self.editor is not None else None
        if note is not None and note.id and int(note.id) in states:
            state = states[int(note.id)]
            if state is not None:
                states[int(note.id)] = {
                    **state,
                    "fields": [str(html) for _name, html in note.items()],
                    "tags": list(note.tags),
                }
        return states

    def _current_proposal(
        self, thread_id: str | None = None
    ) -> tuple[Thread, Proposal, Snapshot, Patch] | None:
        service = self._service()
        thread_id = thread_id or self._thread_id
        if service is None or thread_id is None:
            return None
        thread = service.store.get_thread(thread_id)
        if thread is None or thread.status not in APPLICABLE_STATUSES:
            return None
        proposal = service.store.current_proposal(thread_id)
        snapshot = self._thread_snapshot(thread)
        if proposal is None or snapshot is None:
            return None
        try:
            patch = patch_from_dict(proposal.patch, snapshot)
        except Exception:
            return None
        return thread, proposal, snapshot, patch

    def _render_selected_thread(self) -> None:
        self._activity_id = None
        self._activity_open = False
        self.surface.eval(js_clear_transcript())
        self._clear_proposal()
        self.apply_button.setText("Apply proposal")
        thread = self._selected_thread()
        service = self._service()
        if thread is None or service is None:
            self._last_rendered = None
            self.apply_button.setEnabled(False)
            self.stop_button.setEnabled(False)
            self.discard_button.setEnabled(False)
            self._update_requeue_actions(None, False)
            self._after_thread_rendered(None, None, None, None)
            return
        previous = self._last_rendered
        self._last_rendered = (thread.id, thread.status)
        snapshot = self._thread_snapshot(thread)
        context = self._context_fragment(thread)
        if context is not None:
            self._append_transcript(context)
        selected = selected_text_of(snapshot) if snapshot is not None else None
        for fragment in transcript_fragments(
            thread, service.store.messages(thread.id), selected
        ):
            self._append_transcript(fragment)
        created = created_notes_fragment(
            created_note_ids(service.store.proposals(thread.id))
        )
        if created is not None:
            self._append_transcript(created)
        banner = status_fragment(thread, closed_hint=self._closed_hint)
        if banner is not None:
            self._append_transcript(banner)
            if thread.status in ACTIVE_STATUSES:
                self._activity_id = LIVE_ACTIVITY_ID
                self._activity_open = True
        shown = self._show_proposal(thread, snapshot)
        # A stale proposal is applied only through Apply anyway.
        self.apply_button.setEnabled(shown is not None and thread.status != STALE)
        self.stop_button.setEnabled(thread.status in ACTIVE_STATUSES)
        self.discard_button.setEnabled(thread.status in DISCARDABLE_STATUSES)
        self._update_requeue_actions(thread, shown is not None)
        self._after_thread_rendered(thread, previous, snapshot, shown)

    def _update_requeue_actions(
        self, thread: Thread | None, has_proposal: bool
    ) -> None:
        """Re-run, Retry and Apply anyway, for the hosts that have them."""
        status = thread.status if thread is not None else None
        if self.rerun_button is not None:
            self.rerun_button.setEnabled(status in RERUNNABLE_STATUSES)
        if self.retry_button is not None:
            retryable = status in RETRYABLE_STATUSES
            self.retry_button.setVisible(retryable)
            self.retry_button.setEnabled(retryable)
        if self.apply_anyway_button is not None:
            stale = status == STALE and has_proposal
            self.apply_anyway_button.setVisible(stale)
            self.apply_anyway_button.setEnabled(stale)

    def _show_proposal(self, thread: Thread, snapshot: Snapshot | None) -> Patch | None:
        found = self._current_proposal(thread.id)
        if found is None or snapshot is None:
            return None
        _thread, _proposal, _snapshot, patch = found
        # Diff against the note as it is now, which is what Apply changes.
        current = refresh_snapshot(
            snapshot, self._note_states(snapshot_note_ids(snapshot))
        )
        if isinstance(patch, MultiNotePatch) and isinstance(current, MultiCardSnapshot):
            self._set_multi_proposal(current, patch)
            return patch
        if isinstance(patch, NotePatch) and isinstance(current, EditorSnapshot):
            self._set_proposal(current, patch, self._notetype(patch.notetype_id))
            return patch
        return None

    def _notetype(self, notetype_id: int) -> dict[str, Any]:
        try:
            col: Any = self._col()
            return dict(col.models.get(notetype_id) or {})
        except Exception:
            return {}

    def on_thread_activity(self, thread_id: str, line: str) -> None:
        if thread_id == self._thread_id:
            self._append_activity_line(line)

    # stop / discard / apply

    def _stop_running_agent(self) -> None:
        service = self._service()
        thread = self._selected_thread()
        if service is None or thread is None or thread.status not in ACTIVE_STATUSES:
            return
        service.runner.cancel(thread.id)
        self.stop_button.setEnabled(False)
        self._append_activity_line("[status] stopping agent")

    def _discard_pending_patch(
        self, thread_id: str | None = None, reason: str | None = None
    ) -> bool:
        """Discard the thread (with an optional reason), or warn if it can no
        longer be discarded (e.g. a run started meanwhile)."""
        service = self._service()
        thread_id = thread_id or self._thread_id
        if service is None or thread_id is None:
            return False
        try:
            service.store.discard(thread_id, reason)
        except QueueTransitionError as exc:
            showWarning(str(exc), parent=self._widget())
            return False
        self._broadcast_thread_changed(thread_id)
        return True

    def _rerun_thread(self) -> None:
        """Run the last request again against the current note."""
        self._requeue(lambda store, thread_id: store.rerun(thread_id))

    def _retry_thread(self) -> None:
        """Queue a failed run again, with the same request."""
        self._requeue(lambda store, thread_id: store.retry(thread_id))

    def _requeue(self, action: Callable[[Any, str], None]) -> None:
        service = self._service()
        thread = self._selected_thread()
        if service is None or thread is None:
            return
        following = self._following_thread_id(thread.id)
        try:
            action(service.store, thread.id)
        except QueueTransitionError as exc:
            showWarning(str(exc), parent=self._widget())
            return
        service.runner.enqueue_kick()
        self._after_requeue(thread.id, following)

    def _refuse_stale(self, thread_id: str, reason: str) -> None:
        """A proposal's base no longer holds: mark a ready thread stale (so it
        offers re-run / apply anyway / discard) and say why."""
        service = self._service()
        if service is not None:
            try:
                service.store.mark_stale(thread_id, reason)
            except QueueTransitionError:
                pass
            else:
                self._broadcast_thread_changed(thread_id)
        showWarning(reason, parent=self._widget())

    def _finish_thread(self, thread_id: str, from_status: str, to_status: str) -> bool:
        service = self._service()
        if service is None:
            return False
        try:
            service.store.transition(thread_id, from_status, to_status)
        except QueueTransitionError as exc:
            showWarning(str(exc), parent=self._widget())
            return False
        self._broadcast_thread_changed(thread_id)
        return True

    def _apply_anyway(self) -> None:
        """Apply a stale proposal over the note's changes (field-level)."""
        thread = self._selected_thread()
        if thread is None or thread.status != STALE:
            return
        self._apply_pending_patch(force=True)

    def _apply_pending_patch(self, force: bool = False) -> None:
        """Apply the shown proposal; ``force`` skips the base-revision check
        (Apply anyway on a stale proposal)."""
        found = self._current_proposal()
        if found is None:
            return
        thread, proposal, snapshot, patch = found
        if isinstance(patch, MultiNotePatch):
            if not patch.note_updates:
                self._apply_changes(
                    thread.id, thread.status, proposal.version, [], patch.note_creates
                )
            elif isinstance(snapshot, MultiCardSnapshot):
                # The dialog's Apply checked forces when the thread is stale
                # at click time, so an open dialog serves Apply anyway too.
                self._show_multi_proposal_dialog(
                    thread.id, snapshot, patch, proposal.version
                )
            return
        if not patch.has_note_update():
            # Only new notes: nothing in the editor to save or overwrite (in Add
            # Cards they are added directly, not filled into the editor).
            self._apply_changes(
                thread.id, thread.status, proposal.version, [], patch.note_creates
            )
            return
        self._apply_note_update(thread, proposal, snapshot, patch, force=force)

    def _apply_note_update(
        self,
        thread: Thread,
        proposal: Proposal,
        snapshot: Snapshot,
        patch: NotePatch,
        *,
        force: bool = False,
    ) -> None:
        """Apply a proposal that changes its source note, through the collection.

        The pane overrides this to fill its editor instead, so typing that is
        not saved yet is not overwritten.
        """
        col = self._col()
        note_id = thread.note_id or patch.note_id
        if col is None:
            return
        if note_id is None:
            showWarning(NOT_ADDED_YET, parent=self._widget())
            return
        reason = (
            None if force else stale_reason(proposal.base, self._note_states([note_id]))
        )
        if reason is not None:
            self._refuse_stale(thread.id, reason)
            return
        try:
            note = prepare_note_update(col, note_id, patch)
        except PatchValidationError as exc:
            showWarning(str(exc), parent=self._widget())
            return
        self._apply_changes(
            thread.id, thread.status, proposal.version, [note], patch.note_creates
        )

    def _widget(self) -> QWidget:
        return cast(QWidget, self)

    def _apply_changes(
        self,
        thread_id: str,
        from_status: str,
        version: int,
        updates: list[Any],
        creates: tuple[NoteCreate, ...],
    ) -> bool:
        """Save ``updates`` and add ``creates`` in one undoable CollectionOp.

        The thread is marked applied before the write starts, so Apply, Reply
        and Discard are unavailable while it runs; a failed write reverts it.
        """
        if not self._finish_thread(thread_id, from_status, APPLIED):
            return False
        created: list[int] = []

        def op(col: Any) -> Any:
            return apply_proposal_changes(
                col, updates=updates, creates=creates, created_ids=created
            )

        def on_success(_changes: Any) -> None:
            service = self._service()
            if created and service is not None:
                service.store.set_proposal_result(
                    thread_id, version, {"created_note_ids": list(created)}
                )
                self._broadcast_thread_changed(thread_id)
            tooltip("Agent proposal applied.", parent=self._widget())
            self._after_apply(
                thread_id, [int(note.id) for note in updates], list(created)
            )

        def on_failure(exc: Exception) -> None:
            message = str(exc) or type(exc).__name__
            if isinstance(exc, PartialApplyError):
                # Something was written: stay applied (a re-Apply would add the
                # same notes again) and keep what was added.
                self._record_partial_apply(thread_id, version, created, message)
            else:
                self._finish_thread(thread_id, APPLIED, from_status)
            showWarning(message, parent=self._widget())

        CollectionOp(parent=self._widget(), op=op).success(on_success).failure(
            on_failure
        ).run_in_background(initiator=self)
        return True

    def _record_partial_apply(
        self, thread_id: str, version: int, created: list[int], message: str
    ) -> None:
        service = self._service()
        if service is None:
            return
        service.store.set_proposal_result(
            thread_id, version, {"created_note_ids": list(created), "error": message}
        )
        service.store.add_message(
            thread_id,
            "system",
            f"{message}. What was written stays; Undo reverts it.",
        )
        self._broadcast_thread_changed(thread_id)

    def _show_multi_proposal_dialog(
        self,
        thread_id: str,
        snapshot: MultiCardSnapshot,
        patch: MultiNotePatch,
        version: int,
    ) -> None:
        if self._multi_proposal_dialog is not None:
            self._multi_proposal_dialog.raise_()
            self._multi_proposal_dialog.activateWindow()
            return
        dialog = MultiCardProposalDialog(
            parent=self._widget(),
            snapshot=snapshot,
            patch=patch,
            apply_callback=lambda checked: self._apply_checked_multi_patch(
                thread_id, checked, version
            ),
            discard_callback=lambda: self._discard_pending_patch(thread_id),
            create_previews=self._create_previews(patch.note_creates),
        )
        self._multi_proposal_dialog = dialog
        self._multi_proposal_key = (thread_id, version)

        def on_finished(_result: int) -> None:
            if self._multi_proposal_dialog is dialog:
                self._multi_proposal_dialog = None
                self._multi_proposal_key = None

        qconnect(dialog.finished, on_finished)
        dialog.show()

    def _sync_multi_proposal_dialog(self, thread_id: str) -> None:
        """Close the multi-card dialog when its proposal is no longer current,
        and reopen it on the thread's new proposal if there is one."""
        dialog = self._multi_proposal_dialog
        key = self._multi_proposal_key
        if dialog is None or key is None or key[0] != thread_id:
            return
        found = self._current_proposal(thread_id)
        if found is not None and found[1].version == key[1]:
            return
        self._multi_proposal_dialog = None
        self._multi_proposal_key = None
        dialog.reject()
        if found is None:
            return
        _thread, proposal, snapshot, patch = found
        if (
            isinstance(patch, MultiNotePatch)
            and patch.note_updates
            and isinstance(snapshot, MultiCardSnapshot)
        ):
            self._show_multi_proposal_dialog(
                thread_id, snapshot, patch, proposal.version
            )

    def _apply_checked_multi_patch(
        self,
        thread_id: str,
        checked_note_ids: set[int],
        version: int | None = None,
        *,
        force: bool = False,
    ) -> bool:
        """Apply the checked notes of the current proposal; with ``version``
        (the one the dialog shows), refuse if a newer proposal replaced it.
        ``force``, or a stale thread (the user was told and chose to apply
        again: Apply anyway), skips the base-revision check."""
        found = self._current_proposal(thread_id)
        col = self._col()
        if found is None or col is None:
            return False
        thread, proposal, snapshot, patch = found
        if version is not None and proposal.version != version:
            showWarning(PROPOSAL_CHANGED, parent=self._widget())
            return False
        if not isinstance(snapshot, MultiCardSnapshot) or not isinstance(
            patch, MultiNotePatch
        ):
            return False
        force = force or thread.status == STALE
        states = self._note_states(sorted(checked_note_ids))
        reason = None if force else stale_reason(proposal.base, states)
        if reason is not None:
            self._refuse_stale(thread_id, reason)
            return False
        current = refresh_snapshot(snapshot, states)
        assert isinstance(current, MultiCardSnapshot)
        try:
            notes = prepare_multi_note_updates(
                col,
                current,
                patch,
                checked_note_ids,
            )
        except PatchValidationError as exc:
            showWarning(str(exc), parent=self._widget())
            return False
        if not notes and not patch.note_creates:
            showWarning(
                "No checked cards have proposed changes.", parent=self._widget()
            )
            return False
        if patch.note_creates:
            return self._apply_changes(
                thread_id, thread.status, proposal.version, notes, patch.note_creates
            )

        # Mark applied before the write starts, so Apply, Reply and Discard are
        # unavailable while it runs; a failed write reverts it.
        from_status = thread.status
        if not self._finish_thread(thread_id, from_status, APPLIED):
            return False

        def on_success(_changes: Any) -> None:
            tooltip("Agent proposal applied.", parent=self._widget())
            self._after_apply(thread_id, [int(note.id) for note in notes], [])

        def on_failure(exc: Exception) -> None:
            self._finish_thread(thread_id, APPLIED, from_status)
            showWarning(str(exc) or type(exc).__name__, parent=self._widget())

        update_notes(parent=self._widget(), notes=notes).success(on_success).failure(
            on_failure
        ).run_in_background(initiator=self)
        return True


def prepare_multi_note_updates(
    col: Any,
    snapshot: MultiCardSnapshot,
    patch: MultiNotePatch,
    checked_note_ids: set[int],
) -> list[Any]:
    notes = []
    for update in patch.note_updates:
        if update.note_id not in checked_note_ids:
            continue
        snapshot_note = snapshot.note_by_id(update.note_id)
        try:
            note = col.get_note(update.note_id)
        except Exception as exc:
            raise PatchValidationError(
                f"Selected note {update.note_id} no longer exists."
            ) from exc
        if int(note.mid) != snapshot_note.notetype_id:
            raise PatchValidationError(
                f"Selected note {update.note_id} has changed note type."
            )

        current_fields = tuple(
            FieldSnapshot(name=str(name), html=str(html)) for name, html in note.items()
        )
        current_by_name = {field.name: field.html for field in current_fields}
        field_names = [field.name for field in current_fields]
        for field_name, html in update.field_updates.items():
            if field_name not in current_by_name:
                raise PatchValidationError(f"Unknown field: {field_name}.")
            if current_by_name[field_name] != snapshot_note.field_html(field_name):
                raise PatchValidationError(
                    f"Selected note {update.note_id} has changed since the proposal."
                )
            note.fields[field_names.index(field_name)] = html

        if update.tag_patch.has_changes():
            if tuple(note.tags) != snapshot_note.tags:
                raise PatchValidationError(
                    f"Selected note {update.note_id} tags have changed since the proposal."
                )
            note.tags = list(update.tag_patch.apply(tuple(note.tags)))

        notes.append(note)
    return notes


def prepare_note_update(col: Any, note_id: int, patch: NotePatch) -> Any:
    """The collection's note with the patch's field and tag changes, unsaved."""
    try:
        note = col.get_note(note_id)
    except Exception as exc:
        raise PatchValidationError(f"Note {note_id} no longer exists.") from exc
    if patch.notetype_id != int(note.mid):
        raise PatchValidationError("The note type has changed since the proposal.")
    names = [str(name) for name, _html in note.items()]
    for field_name, html in patch.field_updates.items():
        if field_name not in names:
            raise PatchValidationError(f"Unknown field: {field_name}.")
        note.fields[names.index(field_name)] = html
    if patch.tag_patch.has_changes():
        note.tags = list(patch.tag_patch.apply(tuple(note.tags)))
    return note
