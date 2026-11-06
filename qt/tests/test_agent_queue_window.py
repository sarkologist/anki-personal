# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The agent queue window: thread list, detail, and its actions."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any, Callable

import pytest

from .test_editor_agent_pane import (
    FakeButton,
    FakeCollectionOp,
    FakeMutableNote,
    FakePrompt,
    FakeSurface,
    _import_runtime_with_aqt_stubs,
    _note_create,
    _open_queue,
    _patch_ui,
    multi_snapshot,
    snapshot,
)

from editor_agent_pane.patches import (  # noqa: E402  # isort: skip
    MultiNotePatch,
    MultiNoteUpdate,
    NotePatch,
)
from editor_agent_pane.queue_runner import base_for  # noqa: E402  # isort: skip
from editor_agent_pane.queue_store import QueueStore  # noqa: E402  # isort: skip
from editor_agent_pane.snapshots import patch_to_dict  # noqa: E402  # isort: skip
from editor_agent_pane.thread_view import (  # noqa: E402  # isort: skip
    STALE_WARNING,
    card_excerpt,
    queue_row,
    queue_threads,
    thread_context,
)


class FakeTable:
    def __init__(self) -> None:
        self.rows = 0
        self.selected = -1
        self.items: dict[tuple[int, int], Any] = {}

    def setRowCount(self, rows: int) -> None:
        self.rows = rows

    def setItem(self, row: int, column: int, item: Any) -> None:
        self.items[(row, column)] = item

    def selectRow(self, row: int) -> None:
        self.selected = row

    def clearSelection(self) -> None:
        self.selected = -1

    def blockSignals(self, _block: bool) -> bool:
        return False


class FakeCheck:
    def __init__(self, checked: bool = False) -> None:
        self.checked = checked
        self.visible = True
        self.text = ""

    def isChecked(self) -> bool:
        return self.checked

    def setChecked(self, checked: bool) -> None:
        self.checked = checked

    def setVisible(self, visible: bool) -> None:
        self.visible = visible

    def setText(self, text: str) -> None:
        self.text = text


class FakeNotesCol:
    """Notes by id, with ``mod`` per note, for the collection apply path."""

    def __init__(self, notes: dict[int, FakeMutableNote]) -> None:
        self.notes = notes
        self.decks = types.SimpleNamespace(name=lambda deck_id: f"Deck {deck_id}")
        self.models = types.SimpleNamespace(get=lambda _id: {"name": "Basic"})

    def get_note(self, note_id: int) -> FakeMutableNote:
        return self.notes[note_id]


def _note(note_id: int = 123, back: str = "old back") -> FakeMutableNote:
    note = FakeMutableNote(
        field_names=("Front", "Back"),
        note_id=note_id,
        mid=7,
        fields=("old front", back),
        tags=("keep", "remove-me"),
    )
    note.mod = 100  # type: ignore[attr-defined]
    return note


@pytest.fixture
def qw(monkeypatch: pytest.MonkeyPatch) -> Any:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    module = runtime.queue_window
    module.runtime = runtime
    module.warnings = []
    module.tips = []
    module.dialogs = []
    _patch_ui(
        monkeypatch,
        runtime,
        "showWarning",
        lambda message, parent=None: module.warnings.append(message),
    )
    _patch_ui(
        monkeypatch, runtime, "tooltip", lambda text, **_kw: module.tips.append(text)
    )
    _patch_ui(
        monkeypatch,
        runtime,
        "LegacyLatexPreviewRenderer",
        lambda **_kwargs: types.SimpleNamespace(render=lambda html: html),
    )
    for name in ("showWarning", "tooltip"):
        monkeypatch.setattr(module, name, getattr(runtime.thread_panel, name))
    runtime.aqt.dialogs = types.SimpleNamespace(
        open=lambda *args, **kwargs: module.dialogs.append((args, kwargs))
    )
    runtime.aqt.mw = types.SimpleNamespace(
        col=FakeNotesCol({123: _note(), 101: _note(101), 202: _note(202)}),
        reviewer=None,
    )
    monkeypatch.setattr(module, "_window", None)
    monkeypatch.setattr(module, "_offered", False)
    return module


@pytest.fixture
def queue(qw: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    store, runner = _open_queue(qw.runtime, monkeypatch, tmp_path)
    yield store, runner
    store.close()


@pytest.fixture
def ops(qw: Any, monkeypatch: pytest.MonkeyPatch) -> list[FakeCollectionOp]:
    made: list[FakeCollectionOp] = []

    def make(parent: Any, op: Callable[[Any], Any]) -> FakeCollectionOp:
        made.append(FakeCollectionOp(parent, op))
        return made[-1]

    _patch_ui(monkeypatch, qw.runtime, "CollectionOp", make)
    return made


def _window(qw: Any, *, note_id: int | None = None) -> Any:
    window = qw.AgentQueueWindow.__new__(qw.AgentQueueWindow)
    window._init_state(qw.runtime.aqt.mw, note_id=note_id)
    window.table = FakeTable()
    window.show_closed_checkbox = FakeCheck(False)
    window.note_only_checkbox = FakeCheck(note_id is not None)
    window.surface = FakeSurface()
    window.prompt = FakePrompt("")
    window.prompt.setFocus = lambda: None
    window.prompt.setEnabled = lambda _enabled: None
    window.show_deferred_checkbox = FakeCheck(False)
    for name in (
        "apply",
        "apply_edit",
        "apply_anyway",
        "rerun",
        "retry",
        "reply",
        "discard",
        "defer",
        "browse",
        "stop",
    ):
        setattr(window, f"{name}_button", FakeButton(False))
    _with_settings_row(window)
    window.note_mods = {}

    def read_note_state(note_ids: list[int]) -> dict[int, Any]:
        col = qw.runtime.aqt.mw.col
        states: dict[int, Any] = {}
        for nid in note_ids:
            note = col.notes.get(nid)
            states[nid] = (
                None
                if note is None
                else {
                    "mod": window.note_mods.get(nid, note.mod),
                    "fields": list(note.fields),
                    "tags": list(note.tags),
                }
            )
        return states

    window._read_note_state = read_note_state
    return window


def _thread(
    store: QueueStore,
    *,
    prompt: str = "Fix back",
    status: str = "ready",
    patch: Any = None,
    snap: Any = None,
    note_id: int | None = 123,
    reviewer: dict[str, Any] | None = None,
) -> Any:
    snap = snap if snap is not None else snapshot()
    thread = store.create_thread(
        thread_context("edit_current", snap, reviewer=reviewer, deck_id=3),
        {"provider": "codex", "model": ""},
        prompt,
        note_id=note_id,
    )
    if status == "queued":
        return thread
    store.transition(thread.id, "queued", "running")
    if status == "running":
        return thread
    store.add_message(thread.id, "agent", "Done", "<p>Done</p>")
    if status == "answered":
        store.transition(thread.id, "running", "answered")
        return thread
    if patch is None:
        patch = NotePatch(
            summary="Fix", note_id=123, notetype_id=7, field_updates={"Back": "new"}
        )
    ids = (
        list(patch.affected_note_ids())
        if isinstance(patch, MultiNotePatch)
        else ([patch.note_id] if patch.has_note_update() and patch.note_id else [])
    )
    col = store_col_states(ids)
    store.add_proposal(thread.id, patch_to_dict(patch), base_for(ids, col))
    store.transition(thread.id, "running", "ready")
    if status in ("applied", "discarded"):
        store.transition(thread.id, "ready", status)
    return thread


def store_col_states(note_ids: list[int]) -> dict[int, Any]:
    import aqt

    col = aqt.mw.col  # type: ignore[union-attr]
    return {
        nid: {
            "mod": col.notes[nid].mod,
            "fields": list(col.notes[nid].fields),
            "tags": list(col.notes[nid].tags),
        }
        for nid in note_ids
    }


def _status(store: QueueStore, thread_id: str) -> str:
    thread = store.get_thread(thread_id)
    assert thread is not None
    return thread.status


def _age(store: QueueStore, thread_id: str, seconds_ago: float) -> None:
    import time

    store._set_updated_at_for_tests(thread_id, time.time() - seconds_ago)


# list contents, ordering and filters


def test_queue_threads_sorts_waiting_on_user_first_then_newest(
    qw: Any, queue: Any
) -> None:
    store, _runner = queue
    queued = _thread(store, prompt="queued", status="queued")
    running = _thread(store, prompt="running", status="running")
    old_ready = _thread(store, prompt="old ready")
    new_ready = _thread(store, prompt="new ready")
    _thread(store, prompt="answered", status="answered")
    applied = _thread(store, prompt="applied", status="applied")
    _age(store, old_ready.id, 600)
    _age(store, new_ready.id, 60)

    rows = queue_threads(store.list_threads())

    assert [t.title for t in rows] == [
        "new ready",
        "old ready",
        "answered",
        "running",
        "queued",
    ]
    assert applied.id not in [t.id for t in rows]
    with_closed = queue_threads(store.list_threads(), include_closed=True)
    assert with_closed[-1].id == applied.id
    assert {queued.id, running.id} <= {t.id for t in with_closed}


def test_queue_threads_filters_to_a_note_including_multi_card_threads(
    qw: Any, queue: Any
) -> None:
    store, _runner = queue
    mine = _thread(store, prompt="mine")
    _thread(store, prompt="other", note_id=999)
    multi = _thread(
        store,
        prompt="multi",
        snap=multi_snapshot(),
        note_id=None,
        patch=MultiNotePatch(
            summary="s",
            note_updates=(
                MultiNoteUpdate(
                    note_id=101, notetype_id=7, field_updates={"Back": "B"}
                ),
            ),
        ),
    )

    assert [t.id for t in queue_threads(store.list_threads(), note_id=123)] == [mine.id]
    assert [t.id for t in queue_threads(store.list_threads(), note_id=202)] == [
        multi.id
    ]


def test_queue_row_shows_glyph_title_excerpt_deck_age_version(
    qw: Any, queue: Any
) -> None:
    store, _runner = queue
    thread = store.get_thread(_thread(store, prompt="Fix the back field").id)
    assert thread is not None

    cells = queue_row(thread, deck="Chinese", version=2, now=thread.updated_at + 7200)

    assert cells == ("●", "Fix the back field", "old front", "Chinese", "2h", "v2")
    assert card_excerpt(thread) == "old front"


def test_window_lists_threads_and_renders_the_first(qw: Any, queue: Any) -> None:
    store, _runner = queue
    _thread(store, prompt="queued one", status="queued")
    ready = _thread(
        store,
        prompt="ready one",
        reviewer={"side": "answer", "selection": "the example"},
    )
    window = _window(qw)

    window.refresh()

    assert [cells[1] for cells in window._cells] == ["ready one", "queued one"]
    assert window._cells[0][3] == "Deck 3"
    assert window._cells[0][5] == "v1"
    assert window.table.rows == 2
    assert window.table.selected == 0
    assert window._thread_id == ready.id
    shown = "".join(window.surface.evals)
    assert "Captured context" in shown and "the example" in shown
    assert "ready one" in shown and "Done" in shown
    assert window.apply_button.enabled is True
    assert window.reply_button.enabled is True
    assert window.discard_button.enabled is True
    assert window.browse_button.enabled is True
    assert window.stop_button.enabled is False


def test_window_hides_closed_threads_until_asked(qw: Any, queue: Any) -> None:
    store, _runner = queue
    applied = _thread(store, prompt="applied", status="applied")
    _thread(store, prompt="ready")
    window = _window(qw)

    window.refresh()
    assert [cells[1] for cells in window._cells] == ["ready"]

    window.show_closed_checkbox.setChecked(True)
    window._on_filter_changed()
    assert [cells[1] for cells in window._cells] == ["ready", "applied"]
    assert window._cells[1][0] == "✓"
    assert applied.id in [t.id for t in window._rows]


def test_window_note_filter_and_show_all(qw: Any, queue: Any) -> None:
    store, _runner = queue
    _thread(store, prompt="mine")
    _thread(store, prompt="other", note_id=999)
    window = _window(qw, note_id=123)

    window.refresh()
    assert [cells[1] for cells in window._cells] == ["mine"]
    assert window.note_only_checkbox.visible is True

    window.note_only_checkbox.setChecked(False)
    window._on_filter_changed()
    assert sorted(cells[1] for cells in window._cells) == ["mine", "other"]


# selection and live updates


def test_selecting_a_row_renders_that_thread(qw: Any, queue: Any) -> None:
    store, _runner = queue
    _thread(store, prompt="first")
    second = _thread(store, prompt="second", status="running")
    window = _window(qw)
    window.refresh()
    window.surface.evals.clear()

    window._on_row_selected(1)

    assert window._thread_id == second.id
    shown = "".join(window.surface.evals)
    assert "second" in shown
    assert window.stop_button.enabled is True
    assert window.apply_button.enabled is False
    assert window.reply_button.enabled is False


def test_thread_changed_updates_the_row_and_the_detail(qw: Any, queue: Any) -> None:
    store, _runner = queue
    thread = _thread(store, prompt="running", status="running")
    window = _window(qw)
    window.refresh()
    assert window._cells[0][0] == "⟳"
    store.add_message(thread.id, "agent", "All done", "<p>All done</p>")
    store.transition(thread.id, "running", "answered")
    window.surface.evals.clear()

    window.on_thread_changed(thread.id)

    assert window._cells[0][0] == "✉"
    assert "All done" in "".join(window.surface.evals)
    assert window.reply_button.enabled is True


def test_thread_changed_elsewhere_keeps_the_selection_and_detail(
    qw: Any, queue: Any
) -> None:
    store, _runner = queue
    shown = _thread(store, prompt="shown")
    other = _thread(store, prompt="other", status="running")
    window = _window(qw)
    window.refresh()
    window.surface.evals.clear()

    window.on_thread_changed(other.id)

    assert window._thread_id == shown.id
    assert window.surface.evals == []


def test_window_receives_queue_fan_out(qw: Any, queue: Any) -> None:
    store, _runner = queue
    window = _window(qw)
    qw._window = window

    assert window in list(qw.runtime._all_panes())


# apply


def test_apply_writes_the_note_in_one_op_and_marks_applied(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    thread = _thread(store)
    window = _window(qw)
    window.refresh()

    window._apply_pending_patch()

    assert _status(store, thread.id) == "applied"
    [op] = ops
    assert op.started
    col = types.SimpleNamespace(
        add_custom_undo_entry=lambda _name: 1,
        update_notes=lambda notes: setattr(col, "updated", notes),
        merge_undo_entries=lambda _target: "changes",
    )
    op.finish(col)
    [note] = col.updated
    assert note.id == 123 and note.fields == ["old front", "new"]
    assert qw.tips == ["Agent proposal applied."]


def test_apply_refuses_a_stale_proposal(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    thread = _thread(store)
    window = _window(qw)
    window.refresh()
    window.note_mods[123] = 200

    window._apply_pending_patch()

    assert ops == []
    assert _status(store, thread.id) == "stale"
    assert store.messages(thread.id)[-1].text == STALE_WARNING
    assert qw.warnings == [STALE_WARNING]


def test_apply_reverts_when_the_write_fails(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    thread = _thread(store)
    window = _window(qw)
    window.refresh()

    window._apply_pending_patch()
    [op] = ops
    assert op.on_failure is not None
    op.on_failure(RuntimeError("disk full"))

    assert _status(store, thread.id) == "ready"
    assert qw.warnings == ["disk full"]


def test_apply_creates_only_adds_the_notes(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    patch = NotePatch(
        summary="New", note_id=123, notetype_id=7, note_creates=(_note_create(),)
    )
    thread = _thread(store, patch=patch)
    window = _window(qw)
    window.refresh()

    window._apply_pending_patch()

    assert _status(store, thread.id) == "applied"
    [op] = ops
    added: list[Any] = []
    col = types.SimpleNamespace(
        add_custom_undo_entry=lambda _name: 1,
        update_notes=lambda notes: pytest.fail("no updates"),
        models=types.SimpleNamespace(
            get=lambda _id: {"flds": [{"name": "Front"}, {"name": "Back"}]}
        ),
        new_note=lambda _notetype: _Writable(),
        add_note=lambda note, deck_id: added.append(deck_id) or setattr(note, "id", 9),
        merge_undo_entries=lambda _target: "changes",
    )
    op.finish(col)
    assert added == [3]
    proposal = store.proposals(thread.id)[-1]
    assert proposal.result == {"created_note_ids": [9]}


def test_apply_partial_failure_keeps_applied_and_records_what_was_added(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    patch = NotePatch(
        summary="New",
        note_id=123,
        notetype_id=7,
        note_creates=(_note_create(), _note_create(deck_id=1)),
    )
    thread = _thread(store, patch=patch)
    window = _window(qw)
    window.refresh()

    window._apply_pending_patch()
    [op] = ops
    added: list[int] = []

    def add_note(note: Any, deck_id: int) -> None:
        if added:
            raise RuntimeError("disk full")
        added.append(deck_id)
        note.id = 9

    col = types.SimpleNamespace(
        add_custom_undo_entry=lambda _name: 1,
        models=types.SimpleNamespace(
            get=lambda _id: {"flds": [{"name": "Front"}, {"name": "Back"}]}
        ),
        new_note=lambda _notetype: _Writable(),
        add_note=add_note,
        merge_undo_entries=lambda _target: "changes",
    )
    with pytest.raises(Exception) as raised:
        op.op(col)
    assert op.on_failure is not None
    op.on_failure(raised.value)

    assert (
        _status(store, thread.id) == "applied"
    )  # not reverted: a re-Apply would duplicate
    proposal = store.proposals(thread.id)[-1]
    assert proposal.result is not None
    assert proposal.result["created_note_ids"] == [9]
    assert "disk full" in proposal.result["error"]
    assert "disk full" in store.messages(thread.id)[-1].text
    assert len(qw.warnings) == 1 and "disk full" in qw.warnings[0]


class _Writable:
    id = 0
    tags: list[str] = []

    def __setitem__(self, _key: str, _value: str) -> None:
        pass


def test_apply_multi_note_opens_the_checkbox_dialog(qw: Any, queue: Any) -> None:
    store, _runner = queue
    thread = _thread(
        store,
        snap=multi_snapshot(),
        note_id=None,
        patch=MultiNotePatch(
            summary="s",
            note_updates=(
                MultiNoteUpdate(
                    note_id=101, notetype_id=7, field_updates={"Back": "B"}
                ),
            ),
        ),
    )
    window = _window(qw)
    opened: list[Any] = []
    window._show_multi_proposal_dialog = lambda *args: opened.append(args)
    window.refresh()

    window._apply_pending_patch()

    assert [args[0] for args in opened] == [thread.id]
    assert _status(store, thread.id) == "ready"


def test_apply_refuses_an_add_cards_note_that_is_not_added(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    patch = NotePatch(
        summary="Fix", note_id=None, notetype_id=7, field_updates={"Back": "x"}
    )
    thread = _thread(store, patch=patch, note_id=None)
    window = _window(qw)
    window.refresh()

    window._apply_pending_patch()

    assert ops == []
    assert _status(store, thread.id) == "ready"
    assert qw.warnings == [qw.runtime.thread_panel.NOT_ADDED_YET]


# reply, discard, browser, stop


def test_reply_queues_the_thread_and_moves_to_the_next(qw: Any, queue: Any) -> None:
    store, runner = queue
    first = _thread(store, prompt="first")
    second = _thread(store, prompt="second")
    _age(store, first.id, 10)
    _age(store, second.id, 20)
    window = _window(qw)
    window.refresh()
    assert window._thread_id == first.id
    window.prompt = FakePrompt("Move the example to the back")

    window._reply()

    assert _status(store, first.id) == "queued"
    assert store.messages(first.id)[-1].text == "Move the example to the back"
    assert runner.kicks == 1
    assert window.prompt.cleared
    assert window._thread_id == second.id
    assert [t.id for t in window._rows] == [second.id, first.id]


def test_reply_needs_text_and_a_replyable_thread(qw: Any, queue: Any) -> None:
    store, runner = queue
    thread = _thread(store, status="running")
    window = _window(qw)
    window.refresh()
    window.prompt = FakePrompt("again")

    window._reply()

    assert _status(store, thread.id) == "running"
    assert runner.kicks == 0
    assert window.prompt.toPlainText() == "again"


def test_discard_stores_the_reason_and_moves_on(qw: Any, queue: Any) -> None:
    store, _runner = queue
    first = _thread(store, prompt="first")
    second = _thread(store, prompt="second")
    _age(store, first.id, 10)
    _age(store, second.id, 20)
    window = _window(qw)
    window._ask_discard_reason = lambda: "wrong pinyin"
    window.refresh()

    window._discard()

    assert _status(store, first.id) == "discarded"
    assert store.messages(first.id)[-1].text == "Discarded: wrong pinyin"
    assert window._thread_id == second.id
    assert [t.id for t in window._rows] == [second.id]


def test_discard_cancelled_or_without_reason(qw: Any, queue: Any) -> None:
    store, _runner = queue
    thread = _thread(store)
    window = _window(qw)
    window.refresh()
    messages = len(store.messages(thread.id))

    window._ask_discard_reason = lambda: None
    window._discard()
    assert _status(store, thread.id) == "ready"

    window._ask_discard_reason = lambda: ""
    window._discard()
    assert _status(store, thread.id) == "discarded"
    assert len(store.messages(thread.id)) == messages


def test_discard_refuses_a_thread_that_started_running_meanwhile(
    qw: Any, queue: Any
) -> None:
    store, _runner = queue
    thread = _thread(store)
    window = _window(qw)
    window.refresh()
    messages = len(store.messages(thread.id))

    def reason() -> str:
        # A reply elsewhere re-queues it and the worker claims it while the
        # reason dialog is open.
        store.reply(thread.id, "more")
        store.transition(thread.id, "queued", "running")
        return "wrong pinyin"

    window._ask_discard_reason = reason
    window._discard()

    assert _status(store, thread.id) == "running"
    assert len(store.messages(thread.id)) == messages + 1  # only the reply
    assert all("Discarded" not in m.text for m in store.messages(thread.id))
    assert qw.warnings == ["Cannot discard a running thread."]


def test_open_in_browser_searches_the_threads_notes(qw: Any, queue: Any) -> None:
    store, _runner = queue
    _thread(store)
    window = _window(qw)
    window.refresh()

    window._open_in_browser()

    [(args, kwargs)] = qw.dialogs
    assert args == ("Browser", qw.runtime.aqt.mw)
    assert kwargs == {"search": ("nid:123",)}


def test_open_in_browser_for_multi_card_and_created_notes(qw: Any, queue: Any) -> None:
    store, _runner = queue
    thread = _thread(
        store,
        snap=multi_snapshot(),
        note_id=None,
        patch=MultiNotePatch(
            summary="s",
            note_updates=(
                MultiNoteUpdate(
                    note_id=101, notetype_id=7, field_updates={"Back": "B"}
                ),
            ),
        ),
    )
    store.transition(thread.id, "ready", "applied")
    store.set_proposal_result(thread.id, 1, {"created_note_ids": [555]})
    window = _window(qw)
    window.show_closed_checkbox.setChecked(True)
    window.refresh()

    window._open_in_browser()

    [(_args, kwargs)] = qw.dialogs
    assert kwargs == {"search": ("nid:101,202,555",)}


def test_stop_cancels_a_running_thread(qw: Any, queue: Any) -> None:
    store, runner = queue
    thread = _thread(store, status="running")
    window = _window(qw)
    window.refresh()

    window._stop_running_agent()

    assert runner.cancelled == [thread.id]


# opening the window, and the once-per-session offer


class _FakeWindow:
    made: list[Any] = []

    def __init__(self, mw: Any, *, note_id: int | None = None) -> None:
        self.mw = mw
        self.note_id = note_id
        self.raised = 0
        _FakeWindow.made.append(self)

    def set_note_filter(self, note_id: int | None) -> None:
        self.note_id = note_id

    def show(self) -> None:
        pass

    def raise_(self) -> None:
        self.raised += 1

    def activateWindow(self) -> None:
        pass


def test_open_window_is_a_singleton_and_takes_the_note_filter(
    qw: Any, queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeWindow.made = []
    monkeypatch.setattr(qw, "AgentQueueWindow", _FakeWindow)

    first = qw.open_window(note_id=123)
    second = qw.open_window()

    assert first is second and len(_FakeWindow.made) == 1
    assert second.note_id is None
    assert second.raised == 2


def test_pane_queue_button_opens_the_window_for_its_note(
    qw: Any, queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[Any] = []
    monkeypatch.setattr(qw, "open_window", lambda **kw: opened.append(kw))
    pane = qw.runtime.EditorAgentPane.__new__(qw.runtime.EditorAgentPane)
    pane.editor = types.SimpleNamespace(note=_note(123))
    pane.browser = None

    pane._open_queue_window()

    assert opened == [{"note_id": 123}]


def test_leaving_the_reviewer_offers_the_queue_once_per_session(
    qw: Any, queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _runner = queue
    monkeypatch.setattr(qw, "_config", lambda: {"queue_shortcut": "Ctrl+Alt+Shift+Q"})

    qw._on_state_did_change("overview", "review")
    assert qw.tips == []  # nothing ready: not offered, and not used up

    _thread(store)
    _thread(store, status="answered")
    qw._on_state_did_change("review", "overview")
    assert qw.tips == []  # entering the reviewer is not leaving it

    qw._on_state_did_change("overview", "review")
    assert qw.tips == ["Agent queue: 2 requests ready for review (Ctrl+Alt+Shift+Q)"]

    qw._on_state_did_change("deckBrowser", "review")
    assert len(qw.tips) == 1

    qw._on_profile_did_open()
    qw._on_state_did_change("overview", "review")
    assert len(qw.tips) == 2


# phase 3b: stale handling, retry, defer, change model, apply then edit


def _stale(store: QueueStore, **kwargs: Any) -> Any:
    thread = _thread(store, **kwargs)
    store.mark_stale(thread.id, STALE_WARNING)
    return thread


def _failed(store: QueueStore) -> Any:
    thread = _thread(store)
    store.reply(thread.id, "again")
    store.transition(thread.id, "queued", "running")
    store.complete_run(
        thread.id, to_status="failed", messages=[("system", "boom", "")], error="boom"
    )
    return thread


def _shown_buttons(window: Any) -> dict[str, bool]:
    return {
        name: getattr(window, f"{name}_button").visible
        and getattr(window, f"{name}_button").enabled
        for name in ("apply", "apply_edit", "apply_anyway", "rerun", "retry", "discard")
    }


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            "ready",
            {"apply", "apply_edit", "rerun", "discard"},
        ),
        ("stale", {"apply_anyway", "rerun", "discard"}),
        ("failed", {"apply", "apply_edit", "rerun", "retry", "discard"}),
        ("answered", {"discard"}),
        ("running", set()),
    ],
)
def test_actions_offered_by_status(
    qw: Any, queue: Any, status: str, expected: set[str]
) -> None:
    store, _runner = queue
    if status == "stale":
        _stale(store)
    elif status == "failed":
        _failed(store)
    else:
        _thread(store, status=status)
    window = _window(qw)

    window.refresh()

    shown = {name for name, on in _shown_buttons(window).items() if on}
    assert shown == expected


def test_stale_reason_is_a_notice_not_an_error(qw: Any, queue: Any) -> None:
    store, _runner = queue
    _stale(store)
    window = _window(qw)

    window.refresh()

    shown = "".join(window.surface.evals)
    assert "agent-notice" in shown and "Stale" in shown
    assert "agent-error" not in shown


def test_rerun_queues_the_stale_thread_and_moves_on(qw: Any, queue: Any) -> None:
    store, runner = queue
    stale = _stale(store, prompt="stale")
    other = _thread(store, prompt="other")
    window = _window(qw)
    window.refresh(select=stale.id)

    window._rerun_thread()

    assert _status(store, stale.id) == "queued"
    assert runner.kicks == 1
    assert window._thread_id == other.id


def test_retry_queues_a_failed_thread(qw: Any, queue: Any) -> None:
    store, runner = queue
    thread = _failed(store)
    window = _window(qw)
    window.refresh()

    window._retry_thread()

    assert _status(store, thread.id) == "queued"
    assert runner.kicks == 1
    assert store.get_thread(thread.id).error is None  # type: ignore[union-attr]


def test_retry_refuses_a_thread_that_is_not_failed(qw: Any, queue: Any) -> None:
    store, runner = queue
    thread = _thread(store)
    window = _window(qw)
    window.refresh()

    window._retry_thread()

    assert _status(store, thread.id) == "ready"
    assert runner.kicks == 0
    assert qw.warnings == ["Cannot retry a ready thread."]


def test_plain_apply_on_stale_still_refuses(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    thread = _stale(store)
    window = _window(qw)
    window.refresh()
    window.note_mods[123] = 200

    window._apply_pending_patch()

    assert ops == []
    assert _status(store, thread.id) == "stale"
    assert qw.warnings == [STALE_WARNING]


def test_apply_anyway_overwrites_the_changed_note(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    thread = _stale(store)
    window = _window(qw)
    window.refresh()
    window.note_mods[123] = 200
    qw.runtime.aqt.mw.col.notes[123].fields[0] = "edited front"

    window._apply_anyway()

    assert qw.warnings == []
    assert _status(store, thread.id) == "applied"
    [op] = ops
    col = types.SimpleNamespace(
        add_custom_undo_entry=lambda _name: 1,
        update_notes=lambda notes: setattr(col, "updated", notes),
        merge_undo_entries=lambda _target: "changes",
    )
    op.finish(col)
    [note] = col.updated
    # Field-level: the edited Front stays, the proposed Back overwrites.
    assert note.fields == ["edited front", "new"]


def test_a_failed_apply_anyway_returns_to_stale(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    thread = _stale(store)
    window = _window(qw)
    window.refresh()

    window._apply_anyway()
    [op] = ops
    assert op.on_failure is not None
    op.on_failure(RuntimeError("disk full"))

    assert _status(store, thread.id) == "stale"


def test_apply_anyway_multi_note_opens_the_dialog(qw: Any, queue: Any) -> None:
    store, _runner = queue
    thread = _stale(
        store,
        snap=multi_snapshot(),
        note_id=None,
        patch=MultiNotePatch(
            summary="s",
            note_updates=(
                MultiNoteUpdate(
                    note_id=101, notetype_id=7, field_updates={"Back": "B"}
                ),
            ),
        ),
    )
    window = _window(qw)
    opened: list[Any] = []
    window._show_multi_proposal_dialog = lambda *args, **kwargs: opened.append(
        (args, kwargs)
    )
    window.refresh()

    window._apply_anyway()

    # The dialog forces because the thread is stale when Apply checked is
    # clicked (test_apply_anyway_applies_through_an_open_multi_dialog).
    [(args, _kwargs)] = opened
    assert args[0] == thread.id


def test_closed_banner_does_not_mention_send(qw: Any, queue: Any) -> None:
    store, _runner = queue
    _thread(store, status="applied")
    window = _window(qw)
    window.show_closed_checkbox.setChecked(True)

    window.refresh()

    shown = "".join(window.surface.evals)
    assert "Applied." in shown and "Send starts" not in shown


# defer


def test_defer_hides_the_row_and_show_deferred_lists_it(qw: Any, queue: Any) -> None:
    store, _runner = queue
    first = _thread(store, prompt="first")
    second = _thread(store, prompt="second")
    _age(store, first.id, 10)
    _age(store, second.id, 20)
    window = _window(qw)
    window.refresh()
    assert window._thread_id == first.id

    window._defer("hour")

    assert _status(store, first.id) == "ready"
    assert [t.id for t in window._rows] == [second.id]
    assert window._thread_id == second.id

    window.show_deferred_checkbox.setChecked(True)
    window._on_filter_changed()
    assert [t.id for t in window._rows] == [first.id, second.id]
    assert window._cells[0][0] == "●⏸"
    assert window._cells[0][4].startswith("until ")

    window._select(first.id)
    window._undefer()
    assert store.get_thread(first.id).deferred_until is None  # type: ignore[union-attr]


def test_defer_pick_time_and_cancel(qw: Any, queue: Any) -> None:
    import time

    store, _runner = queue
    thread = _thread(store)
    window = _window(qw)
    window.refresh()

    window._pick_defer_time = lambda: None
    window._defer_pick()
    assert store.get_thread(thread.id).deferred_until is None  # type: ignore[union-attr]

    until = time.time() + 86400 * 3
    window._pick_defer_time = lambda: until
    window._defer_pick()
    assert store.get_thread(thread.id).deferred_until == until  # type: ignore[union-attr]


def test_offer_count_excludes_deferred(
    qw: Any, queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    store, _runner = queue
    monkeypatch.setattr(qw, "_config", lambda: {"queue_shortcut": "Q"})
    _thread(store)
    later = _thread(store)
    store.defer(later.id, time.time() + 3600)

    qw._on_state_did_change("overview", "review")

    assert qw.tips == ["Agent queue: 1 request ready for review (Q)"]


# change model


class _SettingsCombo:
    def __init__(self) -> None:
        self.items: list[tuple[str, Any]] = []
        self.current_index = 0

    def clear(self) -> None:
        self.items = []
        self.current_index = 0

    def addItem(self, label: str, value: Any = None) -> None:
        self.items.append((label, value))

    def setCurrentIndex(self, index: int) -> None:
        self.current_index = index

    def currentData(self) -> Any:
        return self.items[self.current_index][1] if self.items else None

    def select(self, value: Any) -> None:
        self.current_index = [v for _l, v in self.items].index(value)

    def setEnabled(self, _enabled: bool) -> None:
        pass


def _with_settings_row(window: Any) -> Any:
    for name in ("provider", "model", "effort", "card_access"):
        setattr(window, f"settings_{name}_combo", _SettingsCombo())
    window.settings_button = FakeButton(False)
    return window


def test_settings_row_is_prefilled_from_the_thread(qw: Any, queue: Any) -> None:
    store, _runner = queue
    thread = _thread(store)
    store.set_settings(
        thread.id,
        {
            "provider": "claude",
            "model": "haiku",
            "reasoning_effort": "high",
            "card_access": "deck",
        },
    )
    window = _with_settings_row(_window(qw))

    window.refresh()

    assert window.settings_provider_combo.currentData() == "claude"
    assert window.settings_model_combo.currentData() == "haiku"
    assert ("Haiku", "haiku") in window.settings_model_combo.items
    assert window.settings_effort_combo.currentData() == "high"
    assert ("Max", "max") not in window.settings_effort_combo.items
    assert window.settings_card_access_combo.currentData() == "deck"
    assert window.settings_button.enabled is True


def test_settings_row_merges_into_the_thread_settings(qw: Any, queue: Any) -> None:
    store, _runner = queue
    thread = _thread(store)
    store.set_settings(
        thread.id,
        {
            "provider": "codex",
            "model": "",
            "reasoning_effort": "",
            "card_access": "all",
            "deck_ids": [],
            "project_folder": "/src",
            "timeout_seconds": 900,
            "paths": {"codex_path": "/bin/codex"},
        },
    )
    window = _with_settings_row(_window(qw))
    window.refresh()

    window.settings_provider_combo.select("claude")
    window._on_settings_provider_changed()
    window.settings_model_combo.select("sonnet")
    window._on_settings_model_changed()
    window.settings_effort_combo.select("max")
    window.settings_card_access_combo.select("deck")
    window._apply_thread_settings()

    stored = store.get_thread(thread.id)
    assert stored is not None
    assert stored.settings == {
        "provider": "claude",
        "model": "sonnet",
        "reasoning_effort": "max",
        "card_access": "deck",
        "deck_ids": [3],
        "project_folder": "/src",
        "timeout_seconds": 900,
        "paths": {"codex_path": "/bin/codex"},
        # Codex-only flags are reset on the provider switch.
        "fast_mode": False,
        "stream_reasoning_summaries": False,
    }
    assert stored.status == "ready"
    assert qw.tips == ["The next run of this request uses Claude sonnet."]


def test_settings_row_ollama_uses_discovered_models(qw: Any, queue: Any) -> None:
    store, _runner = queue
    _thread(store)
    window = _with_settings_row(_window(qw))
    discovered: list[Any] = []
    window._discover_ollama_models = discovered.append
    window.refresh()

    window.settings_provider_combo.select("ollama")
    window._on_settings_provider_changed()
    assert len(discovered) == 1
    window._set_ollama_models(("llama3", "qwen"), unavailable=False)

    assert [v for _l, v in window.settings_model_combo.items] == ["llama3", "qwen"]
    assert window.settings_effort_combo.items == [("Model default", "")]


# apply then edit


def test_apply_and_edit_opens_the_note_in_the_browser(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    _thread(store)
    window = _window(qw)
    window.refresh()

    window._apply_and_edit()
    assert qw.dialogs == []  # not before the write succeeded
    [op] = ops
    col = types.SimpleNamespace(
        add_custom_undo_entry=lambda _name: 1,
        update_notes=lambda notes: None,
        merge_undo_entries=lambda _target: "changes",
    )
    op.finish(col)

    [(args, kwargs)] = qw.dialogs
    assert args == ("Browser", qw.runtime.aqt.mw)
    assert kwargs == {"search": ("nid:123",)}


def test_apply_and_edit_opens_the_first_created_note(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    patch = NotePatch(
        summary="New",
        note_id=123,
        notetype_id=7,
        note_creates=(_note_create(), _note_create()),
    )
    _thread(store, patch=patch)
    window = _window(qw)
    window.refresh()

    window._apply_and_edit()
    [op] = ops
    ids = iter((9, 10))
    col = types.SimpleNamespace(
        add_custom_undo_entry=lambda _name: 1,
        models=types.SimpleNamespace(
            get=lambda _id: {"flds": [{"name": "Front"}, {"name": "Back"}]}
        ),
        new_note=lambda _notetype: _Writable(),
        add_note=lambda note, deck_id: setattr(note, "id", next(ids)),
        merge_undo_entries=lambda _target: "changes",
    )
    op.finish(col)

    [(_args, kwargs)] = qw.dialogs
    assert kwargs == {"search": ("nid:9",)}


def test_plain_apply_does_not_open_the_browser(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    _thread(store)
    window = _window(qw)
    window.refresh()

    window._apply()
    [op] = ops
    op.finish(
        types.SimpleNamespace(
            add_custom_undo_entry=lambda _name: 1,
            update_notes=lambda notes: None,
            merge_undo_entries=lambda _target: "changes",
        )
    )

    assert qw.dialogs == []


def test_apply_and_edit_does_not_open_after_a_failed_write(
    qw: Any, queue: Any, ops: list[FakeCollectionOp]
) -> None:
    store, _runner = queue
    _thread(store)
    window = _window(qw)
    window.refresh()

    window._apply_and_edit()
    [op] = ops
    assert op.on_failure is not None
    op.on_failure(RuntimeError("disk full"))

    assert qw.dialogs == []


# the toolbar badge


class _FakeToolbar:
    def __init__(self) -> None:
        self.link_handlers: dict[str, Any] = {}
        self.evals: list[str] = []
        self.web = types.SimpleNamespace(eval=self.evals.append)


def test_badge_link_counts_results_and_hides_at_zero(
    qw: Any, queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    store, _runner = queue
    badge = qw.runtime.queue_badge
    monkeypatch.setattr(badge, "_config", lambda: {"queue_show_badge": True})
    toolbar = _FakeToolbar()
    qw.runtime.aqt.mw.toolbar = toolbar

    links: list[str] = []
    badge._on_top_toolbar_did_init_links(links, toolbar)
    [link] = links
    assert "display:none" in link.replace(" ", "")
    assert badge.BADGE_COMMAND in toolbar.link_handlers

    _thread(store)
    _thread(store, status="answered")
    _stale(store)
    _thread(store, status="running")
    later = _thread(store)
    store.defer(later.id, time.time() + 3600)
    assert badge.badge_count(store) == 3

    links = []
    badge._on_top_toolbar_did_init_links(links, toolbar)
    assert "Agent: 3 ready" in links[0]
    assert "display:none" not in links[0].replace(" ", "")


def test_badge_updates_on_thread_changes_and_opens_the_window(
    qw: Any, queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _runner = queue
    badge = qw.runtime.queue_badge
    monkeypatch.setattr(badge, "_config", lambda: {"queue_show_badge": True})
    toolbar = _FakeToolbar()
    qw.runtime.aqt.mw.toolbar = toolbar
    badge._on_top_toolbar_did_init_links([], toolbar)
    assert badge.listener() in list(qw.runtime._all_panes())

    thread = _thread(store)
    badge.listener().on_thread_changed(thread.id)

    assert "Agent: 1 ready" in toolbar.evals[-1]
    opened: list[Any] = []
    monkeypatch.setattr(qw, "open_window", lambda **kw: opened.append(kw))
    toolbar.link_handlers[badge.BADGE_COMMAND]()
    assert opened == [{}]


def test_badge_is_off_when_configured_off(
    qw: Any, queue: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _runner = queue
    badge = qw.runtime.queue_badge
    monkeypatch.setattr(badge, "_config", lambda: {"queue_show_badge": False})
    toolbar = _FakeToolbar()
    qw.runtime.aqt.mw.toolbar = toolbar
    _thread(store)

    links: list[str] = []
    badge._on_top_toolbar_did_init_links(links, toolbar)
    badge.listener().on_thread_changed("x")

    assert links == [] and toolbar.evals == []
