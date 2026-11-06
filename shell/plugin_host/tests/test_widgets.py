# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The shim's widgets and editors in this process, against a fake shell
end that records what would be sent."""

from __future__ import annotations

import base64
import importlib.machinery
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]


class ShimPackages:
    """aqt and _aqt from the shim and the build, as the host has them."""

    PATHS = [str(REPO / "shell/plugin_host"), str(REPO / "out/qt")]

    def find_spec(self, name: str, path: object, target: object = None) -> Any:
        if name in ("aqt", "_aqt"):
            return importlib.machinery.PathFinder.find_spec(name, self.PATHS)
        return None


sys.meta_path.insert(0, ShimPackages())

import aqt  # noqa: E402
from anki.collection import Collection  # noqa: E402
from aqt import editor as editors  # noqa: E402
from aqt import gui_hooks, widgets  # noqa: E402
from aqt.main import MainWindow  # noqa: E402
from aqt.qt import (  # noqa: E402
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    Qt,
    QTextCursor,
    QVBoxLayout,
    QWidget,
)
from aqt.webview import AnkiWebView  # noqa: E402


class FakeNative:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, Any] = {}

    def notify(self, op: str, **fields: Any) -> None:
        self.sent.append((op, fields))

    def request(self, op: str, **fields: Any) -> Any:
        self.sent.append((op, fields))
        return self.answers.get(op)

    def menu_changed(self, menu: Any) -> None:
        self.notify("menu", menu=menu.name, items=[a.text() for a in menu.actions])

    def ops(self, op: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.sent if name == op]


@pytest.fixture
def mw() -> Iterator[MainWindow]:
    native = FakeNative()
    with tempfile.TemporaryDirectory() as folder:
        mw = MainWindow(native, str(Path(folder) / "addons21"))
        mw.loop.after_task.append(editors.sync_notes)
        aqt.mw = mw
        try:
            yield mw
        finally:
            for label in list(editors._editors):
                editors.handle("editor_closed", {"label": label})
            if mw.col:
                mw.col.close()
            aqt.mw = None


def shown(mw: MainWindow, child: QWidget) -> QDockWidget:
    """`child` in a dock of an editor window, as the agent pane shows."""
    editor = editors.Editor("add", editors.EditorMode.ADD_CARDS)
    dock = QDockWidget("Agent", editor.parentWindow)
    dock.setWidget(child)
    editor.parentWindow.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    mw.loop.run_pending()
    mw.native.sent.clear()
    return dock


def event(dock: QDockWidget, op: str, widget: Any = None, **fields: Any) -> Any:
    out = widgets.handle_event(
        {"root": dock._id, "op": op, "id": widget._id if widget else 0, **fields}
    )
    aqt.mw.loop.run_pending()
    return out


def updates(mw: MainWindow) -> list[dict[str, Any]]:
    mw.loop.run_pending()
    return [op for batch in mw.native.ops("widget_update") for op in batch["ops"]]


# Widgets
##########################################################################


def test_the_page_gets_the_tree_of_layouts_and_widgets(mw):
    pane = QWidget()
    layout = QVBoxLayout()
    layout.setContentsMargins(8, 8, 8, 8)
    pane.setLayout(layout)
    label = QLabel("Context")
    layout.addWidget(label)
    form = QFormLayout()
    combo = QComboBox()
    combo.addItem("Codex", "codex")
    combo.addItem("Claude", "claude")
    form.addRow("Provider", combo)
    # a layout filled before it's added
    row = QHBoxLayout()
    send = QPushButton("Send")
    row.addStretch(1)
    row.addWidget(send)
    layout.addLayout(form)
    layout.addLayout(row)
    dock = shown(mw, pane)

    tree = event(dock, "tree")["tree"]
    assert tree["type"] == "dock" and tree["title"] == "Agent"
    items = tree["child"]["layout"]["items"]
    assert tree["child"]["layout"]["margins"] == [8, 8, 8, 8]
    assert items[0]["w"]["text"] == "Context"
    (provider,) = items[1]["l"]["rows"]
    assert provider["label"]["text"] == "Provider"
    assert provider["w"]["items"] == ["Codex", "Claude"]
    assert provider["w"]["index"] == 0
    assert items[2]["l"]["items"] == [
        {"stretch": 1},
        {"w": send._tree(), "stretch": 0},
    ]
    # and the widgets know where they are
    assert send.window() is dock.parentWidget() or send.parentWidget() is pane
    assert send.isVisible()


def test_changes_go_out_after_the_task_in_order_with_each_widget_once(mw):
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    label, web = QLabel("a"), AnkiWebView()
    pane.layout().addWidget(label)
    pane.layout().addWidget(web)
    dock = shown(mw, pane)

    label.setText("b")
    web.eval("one()")
    label.setText("c")
    label.setEnabled(False)
    web.eval("two()")
    assert mw.native.ops("widget_update") == []
    ops = updates(mw)
    assert [op.get("eval") or op["props"]["text"] for op in ops] == [
        "c",
        "one()",
        "two()",
    ]
    assert ops[0]["props"]["enabled"] is False
    (batch,) = mw.native.ops("widget_update")
    assert batch["root"] == dock._id and batch["seq"] == 1
    # the next tree says which batches it already has
    assert event(dock, "tree")["seq"] == 1


def test_a_hidden_dock_is_not_visible_and_says_when_it_changes(mw):
    pane = QWidget()
    dock = shown(mw, pane)
    seen = []
    dock.visibilityChanged.connect(seen.append)
    dock.hide()
    assert not dock.isVisible() and not pane.isVisible()
    (panel,) = mw.native.ops("editor_side_panel")
    assert panel == {
        "label": "add",
        "root": dock._id,
        "visible": False,
        "title": "Agent",
    }
    event(dock, "close", dock)
    dock.setVisible(True)
    assert dock.isVisible() and pane.isVisible() and seen == [False, True]


def test_typing_reaches_the_widget_before_a_click_is_handled(mw):
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    edit, prompt, send = QLineEdit(), QPlainTextEdit(), QPushButton("Send")
    for w in (edit, prompt, send):
        pane.layout().addWidget(w)
    dock = shown(mw, pane)
    texts, sent = [], []
    edit.textChanged.connect(texts.append)
    send.clicked.connect(lambda: sent.append(prompt.toPlainText()))

    event(dock, "text", edit, value="/tmp/x")
    event(dock, "clicked", send, values={str(prompt._id): "explain this"})
    assert texts == ["/tmp/x"] and sent == ["explain this"]
    # the page has it already
    assert updates(mw) == []
    # disabled widgets don't act
    send.setEnabled(False)
    event(dock, "clicked", send)
    assert sent == ["explain this"]


def test_combo_boxes_signal_as_qt_does(mw):
    combo = QComboBox()
    seen: list[Any] = []
    combo.currentIndexChanged.connect(lambda i: seen.append(("index", i)))
    combo.activated.connect(lambda i: seen.append(("activated", i)))
    combo.addItem("a", 1)
    combo.addItem("b", 2)
    assert seen == [("index", 0)] and combo.currentData() == 1
    combo.blockSignals(True)
    combo.setCurrentIndex(1)
    combo.blockSignals(False)
    assert seen == [("index", 0)] and combo.currentText() == "b"
    assert combo.findData(1) == 0 and combo.findData(3) == -1
    combo.clear()
    assert seen[-1] == ("index", -1) and combo.currentData() is None

    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    pane.layout().addWidget(combo)
    combo.addItems(["x", "y"])
    dock = shown(mw, pane)
    seen.clear()
    event(dock, "index", combo, value=1)
    assert seen == [("index", 1), ("activated", 1)]

    combo.setEditable(True)
    texts: list[str] = []
    combo.currentTextChanged.connect(texts.append)
    combo.lineEdit().setPlaceholderText("folder")
    event(dock, "text", combo, value="/tmp/p")
    assert combo.currentText() == "/tmp/p" and texts == ["/tmp/p"]
    props = combo._props()
    assert props["editable"] and props["placeholder"] == "folder"


class Prompt(QPlainTextEdit):
    """As the agent pane's: Enter sends, Up on the first line recalls."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_Return and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self.sent.append(self.toPlainText())
            return
        if event.key() == Qt.Key.Key_Up and self.textCursor().blockNumber() == 0:
            self.setPlainText("earlier")
            self.moveCursor(QTextCursor.MoveOperation.End)
            return
        super().keyPressEvent(event)


def test_a_key_handler_sees_the_pages_keys_and_leaves_the_rest_to_it(mw):
    prompt = Prompt()
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    pane.layout().addWidget(prompt)
    dock = shown(mw, pane)
    assert prompt._props()["keys"] and not QPlainTextEdit()._props()["keys"]

    key = lambda **k: event(dock, "key", prompt, **k)  # noqa: E731
    assert key(key="Enter", text="hi", cursor=2) == {"default": False}
    assert prompt.sent == ["hi"]
    assert key(key="Enter", shift=True, text="hi", cursor=2) == {"default": True}
    assert key(key="a", text="hi\nthere", cursor=4) == {"default": True}
    assert prompt.textCursor().blockNumber() == 1
    assert key(key="ArrowUp", text="hi", cursor=1) == {"default": False}
    ops = updates(mw)
    assert ops[0]["props"]["text"] == "earlier"
    assert ops[-1] == {"id": prompt._id, "cursor": len("earlier")}


def test_web_views_replay_their_evals_and_answer_their_pages(mw):
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    web = AnkiWebView(pane)
    pane.layout().addWidget(web)
    web.stdHtml("<p>hi</p>", js=["js/mathjax.js"], context=pane)
    web.eval("append(1)")
    dock = shown(mw, pane)

    (node,) = event(dock, "tree")["tree"]["child"]["layout"]["items"]
    assert node["w"]["page"] == 1 and node["w"]["replay"] == ["append(1)"]
    web.set_bridge_command(lambda cmd: {"got": cmd}, pane)
    assert event(dock, "bridge", web, value="openNote:3") == {"got": "openNote:3"}
    got = []
    web.evalWithCallback("location.href", got.append)
    (op,) = updates(mw)
    event(dock, "evalResult", web, value="anki://x", n=op["callback"])
    assert got == ["anki://x"]

    # a new page starts a new replay
    web.stdHtml("<p>again</p>")
    page = mw.native.ops("widget_page")[-1]
    assert (page["widget"], page["page"], page["body"]) == (web._id, 2, "<p>again</p>")
    assert "replay" not in web._tree()


def test_checkboxes_and_the_folder_picker(mw):
    box = QCheckBox("Fast")
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    pane.layout().addWidget(box)
    dock = shown(mw, pane)
    toggled = []
    box.toggled.connect(toggled.append)
    event(dock, "checked", box, value=True)
    assert box.isChecked() and toggled == [True]
    box.setChecked(False)
    assert toggled == [True, False]

    mw.native.answers["choose_folder"] = "/Users/x/notes"
    assert QFileDialog.getExistingDirectory(pane, "Project", "/Users/x") == (
        "/Users/x/notes"
    )
    assert mw.native.ops("choose_folder") == [{"title": "Project", "folder": "/Users/x"}]


def test_what_the_shell_cant_show_says_so(mw):
    from aqt.qt import QDateTimeEdit, QDialog, QTableWidget

    with pytest.raises(NotImplementedError, match="QTableWidget"):
        QTableWidget()
    # but its enums can be read where a module only mentions them
    assert QTableWidget.SelectionBehavior.SelectRows is not None
    with pytest.raises(NotImplementedError):
        QDateTimeEdit()
    with pytest.raises(NotImplementedError, match="dialogs"):
        QDialog().show()
    with pytest.raises(NotImplementedError, match="windows"):
        QWidget().show()


def test_add_ons_log_where_anki_puts_their_logs(mw):
    base = Path(mw.addonManager.folder).parent
    logger = mw.addonManager.get_logger("probe.module")
    logger.warning("hello")
    for handler in logger.handlers:
        handler.flush()
    folder = mw.addonManager.logs_folder("probe")
    assert folder == base / "logs/addons/probe"
    assert "hello" in (folder / "probe.log").read_text()
    assert mw.addonManager.get_logger("probe") is logger


# Editors
##########################################################################


@pytest.fixture
def col(mw) -> Collection:
    folder = Path(mw.addonManager.folder).parent
    mw.col = Collection(str(folder / "collection.anki2"))
    return mw.col


def note_event(col: Collection, label: str = "add", **fields: Any) -> dict[str, Any]:
    note = fields.pop("note", None) or col.new_note(col.models.by_name("Basic"))
    return {
        "label": label,
        "note": base64.b64encode(note._to_backend_note().SerializeToString()).decode(),
        "page_id": fields.pop("page_id", -1),
        "deck_id": 1,
        "focused": True,
        **fields,
    }


def test_editor_events_reach_the_hooks_with_one_note_object(mw, col):
    seen: list[Any] = []
    gui_hooks.editor_did_init.append(lambda e: seen.append(("init", e.label)))
    gui_hooks.editor_did_load_note.append(lambda e: seen.append(("load", e.note)))
    gui_hooks.editor_did_unfocus_field.append(
        lambda changed, note, ord: seen.append(("unfocus", note, ord)) or changed
    )
    gui_hooks.editor_did_fire_typing_timer.append(lambda note: seen.append(("type", note)))
    try:
        editors.handle("editor_did_load_note", note_event(col))
        editor = editors.editors()[0]
        note = editor.note
        assert editor.editorMode is editors.EditorMode.ADD_CARDS
        assert editor.parentWindow.deck_chooser.selected_deck_id == 1
        typed = col.new_note(col.models.by_name("Basic"))
        typed.guid = note.guid
        typed["Front"] = "typed"
        editors.handle("editor_did_focus_field", note_event(col, note=typed, ord=0))
        from aqt.qt import QApplication

        assert editor.currentField == 0 and QApplication.focusWidget() is editor.web
        editors.handle("editor_did_fire_typing_timer", note_event(col, note=typed))
        editors.handle("editor_did_unfocus_field", note_event(col, note=typed, ord=0))
        assert editor.currentField is None and editor.last_field_index == 0
        # the same note throughout, as the window has it
        assert editor.note is note and note["Front"] == "typed"
        assert seen == [
            ("init", "add"),
            ("load", note),
            ("type", note),
            ("unfocus", note, 0),
        ]
        # the next note (after Add) is another object
        editors.handle("editor_did_load_note", note_event(col, page_id=-2))
        assert editor.note is not note
    finally:
        gui_hooks.editor_did_init._hooks.clear()
        gui_hooks.editor_did_load_note._hooks.clear()
        gui_hooks.editor_did_unfocus_field._hooks.clear()
        gui_hooks.editor_did_fire_typing_timer._hooks.clear()


def test_changes_to_the_editors_note_go_back_to_the_window(mw, col):
    editors.handle("editor_did_load_note", note_event(col))
    editor = editors.editors()[0]
    mw.native.sent.clear()

    def change() -> None:
        assert editor.note
        editor.note.fields[1] = "<b>back</b>"
        editor.note.tags = ["agent"]

    mw.loop.post(change)
    mw.loop.run_pending()
    assert mw.native.ops("editor_note_changed") == [
        {
            "label": "add",
            "page_id": -1,
            "fields": {"1": "<b>back</b>"},
            "tags": ["agent"],
        }
    ]
    # loadNote sends the whole note, and nothing more after it
    editor.note["Front"] = "shown"
    editor.loadNote(focusTo=0)
    mw.loop.post(lambda: None)
    mw.loop.run_pending()
    (load,) = mw.native.ops("editor_load_note")
    assert load["focus_to"] == 0 and len(mw.native.ops("editor_note_changed")) == 1


def test_editor_buttons_their_keys_and_callbacks(mw, col):
    clicked: list[str] = []

    def buttons(buttons: list[str], editor: Any) -> None:
        buttons.append(
            editor.addButton(
                None,
                "agentPane",
                lambda e: clicked.append(e.label),
                tip="Agent",
                label="Agent",
                keys="Ctrl+Alt+Shift+E",
                disables=False,
            )
        )

    gui_hooks.editor_did_init_buttons.append(buttons)
    try:
        editors.handle("editor_did_load_note", note_event(col, label="editor"))
    finally:
        gui_hooks.editor_did_init_buttons._hooks.clear()
    (sent,) = mw.native.ops("editor_buttons")
    assert sent["label"] == "editor"
    assert 'data-command="agentPane"' in sent["buttons"][0]
    assert sent["keys"] == [{"keys": "Ctrl+Alt+Shift+E", "cmd": "agentPane"}]
    editor = editors.editors()[0]
    assert editor.editorMode is editors.EditorMode.EDIT_CURRENT

    out = editors.handle("editor_bridge_cmd", {"label": "editor", "cmd": "agentPane"})
    assert out == {"handled": True, "value": None} and clicked == []
    # the button waits for the save
    (saved,) = mw.native.ops("editor_after_saved")
    editors.handle("editor_callback", {"callback": saved["callback"]})
    assert clicked == ["editor"]
    assert editors.handle("editor_bridge_cmd", {"label": "editor", "cmd": "other"}) == {
        "handled": False,
        "value": None,
    }

    got: list[Any] = []
    editor.web.evalWithCallback("1 + 1", got.append)
    (ev,) = mw.native.ops("editor_eval")
    editors.handle("editor_callback", {"callback": ev["callback"], "value": 2})
    assert got == [2]


def test_a_structure_change_keeps_the_evals_after_it(mw):
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    web = AnkiWebView()
    pane.layout().addWidget(web)
    dock = shown(mw, pane)
    pane.layout().addWidget(QLabel("new"))
    web.eval("after()")
    ops = updates(mw)
    assert "tree" in ops[0] and ops[1:] == [{"id": web._id, "eval": "after()"}]
    assert event(dock, "tree")["seq"] == 1


def test_a_redrawn_view_answers_callbacks_it_lost(mw):
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    web = AnkiWebView()
    pane.layout().addWidget(web)
    dock = shown(mw, pane)
    got: list[Any] = []
    web.evalWithCallback("lost()", got.append)
    updates(mw)
    # the page reloaded before answering: it asks for the tree again
    event(dock, "tree")
    assert got == [None]
    # one not sent yet is kept for the new page
    web.evalWithCallback("kept()", got.append)
    event(dock, "tree")
    assert got == [None]


def test_load_note_sends_only_what_the_add_on_changed(mw, col):
    editors.handle("editor_did_load_note", note_event(col))
    editor = editors.editors()[0]
    mw.native.sent.clear()
    assert editor.note
    editor.note.fields[1] = "tidied"
    editor.loadNote(focusTo=1)
    (load,) = mw.native.ops("editor_load_note")
    assert load == {
        "label": "add",
        "page_id": -1,
        "fields": {"1": "tidied"},
        "tags": None,
        "focus_to": 1,
    }


def test_an_unfocus_hook_that_changed_the_note_reloads_it_after_a_moment(mw, col):
    gui_hooks.editor_did_unfocus_field.append(lambda changed, note, ord: True)
    try:
        editors.handle("editor_did_load_note", note_event(col))
        mw.native.sent.clear()
        editors.handle("editor_did_unfocus_field", note_event(col, ord=0))
        assert mw.native.ops("editor_load_note") == []
        # the next field's focus comes first, as in Qt
        editors.handle("editor_did_focus_field", note_event(col, ord=1))
        import time

        time.sleep(0.15)
        mw.loop.run_pending()
        (load,) = mw.native.ops("editor_load_note")
        assert load["focus_to"] == 1
    finally:
        gui_hooks.editor_did_unfocus_field._hooks.clear()


def test_a_closed_windows_editor_does_nothing(mw, col):
    editors.handle("editor_did_load_note", note_event(col))
    editor = editors.editors()[0]
    pane = QWidget()
    pane.setLayout(QVBoxLayout())
    web = AnkiWebView()
    pane.layout().addWidget(web)
    dock = QDockWidget("Agent", editor.parentWindow)
    dock.setWidget(pane)
    editor.parentWindow.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
    mw.native.sent.clear()
    editors.handle("editor_closed", {"label": "add"})
    assert mw.native.ops("widget_pages_closed") == [{"widgets": [web._id]}]
    mw.native.sent.clear()
    got: list[Any] = []
    editor.web.eval("x()")
    editor.web.evalWithCallback("y()", got.append)
    editor.doPaste("<b>x</b>", False)
    editor.loadNote()
    mw.loop.run_pending()
    assert mw.native.sent == [] and got == [None]
