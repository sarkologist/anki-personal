# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""aqt.editor for the plugin host: an `Editor` for each of the shell's
editor windows (Add Cards, Edit Current), as add-ons' hooks see them.

The shell sends the note with every event, so `editor.note` is the note as
the window has it; it's one object while the window shows the same note.
What an add-on changes in it goes back to the window when the task ends
(or with `loadNote`, which also shows it), as the Qt editor's note is
saved with the window's next save. `web` evals in the window's page."""

from __future__ import annotations

import base64
import functools
import html
import itertools
import mimetypes
import os
from collections.abc import Callable
from enum import Enum
from typing import Any

import aqt
from anki import notes_pb2
from anki.notes import Note

from .qt import QMainWindow, QObject

ADD = "add"
EDIT_CURRENT = "editor"


class EditorMode(Enum):
    ADD_CARDS = 0
    EDIT_CURRENT = 1
    BROWSER = 2


class EditorState(Enum):
    INITIAL = -1
    FIELDS = 0
    IO_PICKER = 1
    IO_MASKS = 2
    IO_FIELDS = 3


_editors: dict[str, Editor] = {}
_callbacks: dict[int, Callable[..., Any]] = {}
_callback_ids = itertools.count(1)


def _native() -> Any:
    assert aqt.mw
    return aqt.mw.native


def _callback(fn: Callable[..., Any]) -> int:
    n = next(_callback_ids)
    _callbacks[n] = fn
    return n


def editors() -> list[Editor]:
    return list(_editors.values())


def focus_widget() -> Any:
    """QApplication.focusWidget: an editor's web view while a field in
    its window has the focus."""
    for editor in _editors.values():
        if editor.parentWindow._focused and editor.currentField is not None:
            return editor.web
    return None


class DeckChooser:
    def __init__(self) -> None:
        self.selected_deck_id = 0


class EditorWindow(QMainWindow):
    """`editor.parentWindow`: Add Cards or Edit Current. Docks added to it
    are its side panel."""

    def __init__(self, editor: Editor) -> None:
        super().__init__(None)
        self.editor = editor
        self._focused = False
        self._docks: list[Any] = []
        self.deck_chooser = DeckChooser()

    def isVisible(self) -> bool:
        return not self.editor._closed

    def isActiveWindow(self) -> bool:
        return self._focused

    def activateWindow(self) -> None:
        _native().notify("editor_raise", label=self.editor.label)

    def raise_(self) -> None:
        pass

    def addDockWidget(self, area: Any, dock: Any, *_args: Any) -> None:
        from .widgets import Root

        dock._parent = self
        dock._on_shown = self._dock_shown
        Root(dock)
        self._docks.append(dock)
        _native().notify(
            "editor_side_panel",
            label=self.editor.label,
            root=dock._id,
            visible=not dock.isHidden(),
            title=dock._title,
        )

    def removeDockWidget(self, dock: Any) -> None:
        self._dock_shown(dock, False)

    def _dock_shown(self, dock: Any, visible: bool) -> None:
        _native().notify(
            "editor_side_panel",
            label=self.editor.label,
            root=dock._id,
            visible=visible,
            title=dock._title,
        )

    def _close(self) -> None:
        from .widgets import AnkiWebView, _widgets, root_of

        roots = [root for dock in self._docks if (root := root_of(dock))]
        # the shell can forget their web views' pages
        views = [
            w._id
            for w in list(_widgets.values())
            if isinstance(w, AnkiWebView) and root_of(w) in roots
        ]
        for root in roots:
            root.close()
        self._docks.clear()
        if views:
            _native().notify("widget_pages_closed", widgets=views)


class EditorWebView(QObject):
    """`editor.web`: the window's page."""

    def __init__(self, editor: Editor) -> None:
        super().__init__(None)
        self.editor = editor

    def eval(self, js: str) -> None:
        self.editor._send("editor_eval", js=js)

    def evalWithCallback(self, js: str, cb: Callable[[Any], None]) -> None:
        if self.editor._closed:
            # as for a page that's gone
            self.editor.mw.loop.post(functools.partial(cb, None))
            return
        self.editor._send("editor_eval", js=js, callback=_callback(cb))

    def setFocus(self, *_args: Any) -> None:
        pass

    def hasFocus(self) -> bool:
        return focus_widget() is self

    def isVisible(self) -> bool:
        return not self.editor._closed

    def show(self) -> None:
        pass

    def hide(self) -> None:
        pass

    def parentWidget(self) -> Any:
        return self.editor.parentWindow

    def window(self) -> Any:
        return self.editor.parentWindow


class Editor:
    def __init__(self, label: str, mode: EditorMode) -> None:
        assert aqt.mw
        self.label = label
        self.mw = aqt.mw
        self.editorMode = mode
        self.addMode = mode == EditorMode.ADD_CARDS
        self.parentWindow = EditorWindow(self)
        self.widget = self.parentWindow
        self.web = EditorWebView(self)
        self.note: Note | None = None
        self.card: Any = None
        self.currentField: int | None = None
        self.last_field_index: int | None = None
        self.state = EditorState.INITIAL
        self._links: dict[str, Callable[[Editor], Any]] = {}
        # addButton's keys, for the shell
        self._keys: list[dict[str, str]] = []
        self._closed = False
        # the window's note, as last sent or synced: which note it is
        # (id, or the Add window's generation) and its content
        self._page_id = 0
        self._shell_state: tuple[list[str], list[str]] | None = None

    # from the shell

    def _update(self, event: dict[str, Any]) -> None:
        col = self.mw.col
        self.parentWindow._focused = bool(event.get("focused"))
        if "deck_id" in event:
            self.parentWindow.deck_chooser.selected_deck_id = int(event["deck_id"])
        card_id = int(event.get("card_id") or 0)
        if card_id and col and (self.card is None or self.card.id != card_id):
            self.card = col.get_card(card_id)
        elif not card_id:
            self.card = None
        data = event.get("note")
        if not data or col is None:
            return
        proto = notes_pb2.Note()
        proto.ParseFromString(base64.b64decode(data))
        page_id = int(event.get("page_id", proto.id))
        if self.note is None or page_id != self._page_id:
            # another note: a new object, as Qt's set_note
            self.note = Note.__new__(Note)
            self.note.col = col.weakref()
        self._page_id = page_id
        self.note._load_from_backend_note(proto)
        self._shell_state = (list(self.note.fields), list(self.note.tags))

    def _send(self, op: str, **fields: Any) -> None:
        """To this window, while it's open: a reopened one is another."""
        if not self._closed:
            _native().notify(op, label=self.label, **fields)

    def _changes(self) -> dict[str, Any] | None:
        """What an add-on changed in the note since the window sent it:
        only that, so typing the window has since isn't undone."""
        if self.note is None or self._shell_state is None:
            return None
        fields, tags = self._shell_state
        changed = {
            str(i): html
            for i, html in enumerate(self.note.fields)
            if i >= len(fields) or html != fields[i]
        }
        new_tags = list(self.note.tags) if list(self.note.tags) != tags else None
        self._shell_state = (list(self.note.fields), list(self.note.tags))
        return {"page_id": self._page_id, "fields": changed, "tags": new_tags}

    def _sync(self) -> None:
        """Send what an add-on changed in the note to the window."""
        changes = self._changes()
        if changes and (changes["fields"] or changes["tags"] is not None):
            self._send("editor_note_changed", **changes)

    # Qt's API

    def note_type(self) -> Any:
        assert self.note
        return self.note.note_type()

    def loadNote(self, focusTo: int | None = None) -> None:
        changes = self._changes()
        if changes is not None:
            self._send("editor_load_note", **changes, focus_to=focusTo)

    def loadNoteKeepingFocus(self) -> None:
        self.loadNote(self.currentField)

    def call_after_note_saved(
        self, callback: Callable[[], None], keepFocus: bool = False
    ) -> None:
        self._send("editor_after_saved", callback=_callback(callback))

    saveNow = call_after_note_saved

    def doPaste(self, html: str, internal: bool, extended: bool = False) -> None:
        self._send(
            "editor_paste",
            html=html,
            internal=internal,
            extended=extended,
        )

    def checkValid(self) -> None:
        self._send("editor_check_valid")

    def addButton(
        self,
        icon: str | None,
        cmd: str,
        func: Callable[[Editor], None],
        tip: str = "",
        label: str = "",
        id: str | None = None,
        toggleable: bool = False,
        keys: str | None = None,
        disables: bool = True,
        rightside: bool = True,
    ) -> str:
        """As Qt's: the button runs `func` after a save; `keys` (Qt's names,
        so Ctrl is Cmd) go to the shell."""

        def wrapped(editor: Editor) -> None:
            self.call_after_note_saved(functools.partial(func, editor), keepFocus=True)

        self._links[cmd] = wrapped
        if keys:
            self._keys.append({"keys": keys, "cmd": cmd})
        return _button_html(icon, cmd, tip, label, id, toggleable, disables, rightside)

    def onBridgeCmd(self, cmd: str) -> Any:
        if func := self._links.get(cmd):
            func(self)
            return True
        return None


def _button_html(
    icon: str | None,
    cmd: str,
    tip: str,
    label: str,
    id: str | None,
    toggleable: bool,
    disables: bool,
    rightside: bool,
) -> str:
    """Editor._addButton's HTML, which the toolbar's AddonButtons read."""
    if icon:
        if icon.startswith("qrc:/"):
            iconstr = icon
        elif os.path.isabs(icon):
            iconstr = _data_url(icon)
        else:
            iconstr = f"/_anki/imgs/{icon}.png"
        imgelm = f'<img class="topbut" src="{html.escape(iconstr)}">'
    else:
        imgelm = ""
    if label or not imgelm:
        labelelm = label or cmd
    else:
        labelelm = ""
    idstr = f'id="{html.escape(id)}"' if id else ""
    class_ = "linkb" if rightside else "rounded"
    if not disables:
        class_ += " perm"
    return (
        f'<button tabindex=-1 {idstr} class="anki-addon-button {class_}" '
        f'type="button" title="{html.escape(tip)}" '
        f'data-cantoggle="{int(toggleable)}" data-command="{html.escape(cmd)}">'
        f"{imgelm}{labelelm}</button>"
    )


def _data_url(path: str) -> str:
    """Editor.resourceToData."""
    mime = mimetypes.guess_type(path)[0] or "image/png"
    try:
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode()
    except OSError:
        return ""
    return f"data:{mime};base64,{data}"


# events from the shell
##########################################################################


def handle(name: str, event: dict[str, Any]) -> Any:
    from aqt import gui_hooks

    if name == "editor_callback":
        callback = _callbacks.pop(int(event.get("callback", 0)), None)
        if callback:
            if "value" in event:
                callback(event["value"])
            else:
                callback()
        return None
    label = str(event.get("label", ""))
    if name == "editor_closed":
        editor = _editors.pop(label, None)
        if editor:
            editor._closed = True
            editor.parentWindow._close()
        return None
    editor = _editors.get(label)
    created = editor is None
    if editor is None:
        mode = EditorMode.ADD_CARDS if label == ADD else EditorMode.EDIT_CURRENT
        editor = Editor(label, mode)
        _editors[label] = editor
    editor._update(event)
    if created:
        # as Editor.__init__: the buttons, then the editor
        buttons: list[str] = []
        gui_hooks.editor_did_init_buttons(buttons, editor)
        gui_hooks.editor_did_init(editor)
        if buttons or editor._keys:
            _native().notify(
                "editor_buttons", label=label, buttons=buttons, keys=editor._keys
            )
    note = editor.note
    if name == "editor_did_load_note":
        gui_hooks.editor_did_load_note(editor)
    elif name == "editor_did_focus_field":
        ord = int(event.get("ord", 0))
        editor.currentField = editor.last_field_index = ord
        gui_hooks.editor_did_focus_field(note, ord)
    elif name == "editor_did_unfocus_field":
        ord = int(event.get("ord", 0))
        editor.currentField = None
        editor.last_field_index = ord
        if gui_hooks.editor_did_unfocus_field(False, note, ord):
            # after the next field's focus, as Qt
            editor.mw.progress.single_shot(100, editor.loadNoteKeepingFocus, False)
    elif name == "editor_did_fire_typing_timer":
        gui_hooks.editor_did_fire_typing_timer(note)
    elif name == "editor_did_update_tags":
        gui_hooks.editor_did_update_tags(note)
    elif name == "editor_state_did_change":
        old, editor.state = editor.state, _state(event.get("new"))
        gui_hooks.editor_state_did_change(editor, editor.state, old)
    elif name == "add_cards_did_add_note":
        col = editor.mw.col
        nid = int(event.get("nid", 0))
        if col and nid:
            gui_hooks.add_cards_did_add_note(col.get_note(nid))
    elif name == "editor_focus":
        pass
    elif name == "editor_bridge_cmd":
        cmd = str(event.get("cmd", ""))
        if editor.onBridgeCmd(cmd):
            return {"handled": True, "value": None}
        handled, value = gui_hooks.webview_did_receive_js_message(
            (False, None), cmd, editor
        )
        return {"handled": handled, "value": value}
    else:
        raise Exception(f"unknown editor event {name}")
    return None


def _state(value: Any) -> EditorState:
    try:
        return EditorState(int(value))
    except (TypeError, ValueError):
        return EditorState.INITIAL


def sync_notes() -> None:
    """After each task on the host's loop."""
    for editor in list(_editors.values()):
        try:
            editor._sync()
        except Exception:
            from .main import report_exception

            report_exception(f"syncing {editor.label}'s note")
