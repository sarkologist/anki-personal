# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The plugin host's link to the native shell (this module doesn't exist in
the Qt aqt, so add-ons can test for it).

Add-on UI is web panels: `open_panel` shows a page an add-on serves from
its folder (`/_addons/<module>/<path>`, allowed by `setWebExports`) in a
shell window, and the page's `pycmd(text, callback)` reaches `on_message`
here. Events from the shell (hooks, menu clicks, page messages) run on the
host's main loop."""

from __future__ import annotations

import base64
import itertools
import os
from collections.abc import Callable
from typing import Any

from anki import remote
from anki.collection import OpChanges
from anki.utils import from_json_bytes, to_json_bytes

from .main import report_exception


def set_latex_commands(png: list[list[str]], svg: list[list[str]]) -> None:
    """The shell's LaTeX toolchain as latex.py's globals, where an add-on
    such as "Edit LaTeX toolchain" sets them in Anki."""
    from anki import latex

    latex.pngCommands = png
    latex.svgCommands = svg


class Panel:
    def __init__(
        self,
        native: Native,
        id: int,
        on_message: Callable[[str], Any],
        on_closed: Callable[[], None] | None,
    ) -> None:
        self.native = native
        self.id = id
        self.on_message = on_message
        self.on_closed = on_closed
        self.closed = False

    def eval(self, js: str) -> None:
        if not self.closed:
            self.native.notify("panel_eval", panel=self.id, js=js)

    def close(self) -> None:
        if not self.closed:
            self.native.notify("panel_close", panel=self.id)


class Native:
    def __init__(self, connection: remote.Connection | None) -> None:
        self.connection = connection
        self._ids = itertools.count(1)
        self.panels: dict[int, Panel] = {}
        self.actions: dict[int, Any] = {}
        self._context_actions: dict[int, Any] = {}
        # set by the host once the add-ons are loaded
        self.mw: Any = None

    # to the shell
    ##################################################################

    def notify(self, op: str, **fields: Any) -> None:
        if self.connection:
            self.connection.notify(remote.HOST, to_json_bytes({"op": op, **fields}))

    def request(self, op: str, **fields: Any) -> Any:
        if not self.connection:
            return None
        return self.connection.json_request(remote.HOST, {"op": op, **fields})

    def action_id(self, action: Any) -> int:
        if not hasattr(action, "_native_id"):
            action._native_id = next(self._ids)
        return action._native_id

    def menu_changed(self, menu: Any) -> None:
        items = []
        for action in menu.actions:
            id = self.action_id(action)
            self.actions[id] = action
            if action.isVisible():
                item = {"id": id, "text": action.text(), "enabled": action.isEnabled()}
                if shortcut := action.shortcut().toString():
                    item["shortcut"] = shortcut
                items.append(item)
        self.notify("menu", menu=menu.name, items=items)

    def operation_did_execute(self, changes: OpChanges) -> None:
        self.notify(
            "operation_did_execute",
            changes=base64.b64encode(changes.SerializeToString()).decode(),
        )

    def open_panel(
        self,
        module: str,
        path: str,
        title: str,
        width: int,
        height: int,
        on_message: Callable[[str], Any],
        on_closed: Callable[[], None] | None = None,
    ) -> Panel:
        panel = Panel(self, next(self._ids), on_message, on_closed)
        self.panels[panel.id] = panel
        self.notify(
            "open_panel",
            panel=panel.id,
            module=module,
            path=path,
            title=title,
            width=width,
            height=height,
        )
        return panel

    # from the shell
    ##################################################################

    def on_request(self, kind: int, id: int, payload: bytes) -> None:
        """On the connection's reader thread: run the event on the loop."""
        if kind != remote.EVENT:
            self.connection and self.connection.reply(id, False, b"unexpected request")
            return

        def run() -> None:
            try:
                out = self.handle(from_json_bytes(payload))
            except Exception as exc:
                report_exception(f"event {payload[:80]!r}")
                if id and self.connection:
                    self.connection.reply(id, False, str(exc).encode())
                return
            if id and self.connection:
                self.connection.reply(id, True, to_json_bytes(out))

        self.mw.loop.post(run)

    def handle(self, event: dict[str, Any]) -> Any:
        from aqt import gui_hooks

        mw = self.mw
        name = event["event"]
        if name == "operation_did_execute":
            changes = OpChanges()
            changes.ParseFromString(base64.b64decode(event["changes"]))
            if changes.notetype and mw.col:
                mw.col.models._clear_cache()
            gui_hooks.operation_did_execute(changes, None)
        elif name == "set_state":
            mw.state = event["state"]
        elif name == "set_latex_commands":
            set_latex_commands(event["png"], event["svg"])
        elif name in ("set_theme", "theme_did_change"):
            from aqt.theme import theme_manager

            theme_manager.night_mode = bool(event["night"])
            if name == "theme_did_change":
                gui_hooks.theme_did_change()
        elif name == "state_did_change":
            old, mw.state = event.get("old", mw.state), event["new"]
            gui_hooks.state_did_change(mw.state, old)
        elif name == "reviewer_did_answer_card":
            card = self._card(event["card_id"])
            gui_hooks.reviewer_did_answer_card(mw.reviewer, card, event["ease"])
        elif name == "reviewer_will_show_context_menu":
            from aqt.qt import QMenu

            mw.reviewer.card = self._card(event["card_id"])
            menu = QMenu()
            gui_hooks.reviewer_will_show_context_menu(mw.reviewer, menu)
            self._context_actions = {
                self.action_id(action): action for action in menu.actions
            }
            return [
                {"id": id, "text": action.text(), "enabled": action.isEnabled()}
                for id, action in self._context_actions.items()
                if action.isVisible()
            ]
        elif name == "menu_action":
            action = self.actions.get(event["id"]) or self._context_actions.get(
                event["id"]
            )
            if action:
                action.trigger()
        elif name == "deck_browser_will_render_content":
            from aqt.deckbrowser import DeckBrowserContent

            content = DeckBrowserContent(tree="", stats="")
            gui_hooks.deck_browser_will_render_content(mw.deckBrowser, content)
            return {"stats": content.stats}
        elif name == "webview_did_receive_js_message":
            context = {"deckBrowser": mw.deckBrowser, "review": mw.reviewer}.get(
                event.get("context", ""), event.get("context")
            )
            handled, value = gui_hooks.webview_did_receive_js_message(
                (False, None), event["message"], context
            )
            return {"handled": handled, "value": value}
        elif name == "panel_message":
            panel = self.panels.get(event["panel"])
            return panel.on_message(event["message"]) if panel else None
        elif name == "panel_closed":
            panel = self.panels.pop(event["panel"], None)
            if panel:
                panel.closed = True
                if panel.on_closed:
                    panel.on_closed()
        elif name == "sync_did_finish":
            if mw.col:
                mw.col.models._clear_cache()
                if event.get("full"):
                    # the collection was replaced, as `reopen(after_full_sync)`
                    mw.col._load_scheduler()
            gui_hooks.sync_did_finish()
        elif name == "profile_will_close":
            gui_hooks.profile_will_close()
            mw.col = None
        elif name == "shutdown":
            mw.loop.stop()
        elif name == "widget_event":
            from aqt import widgets

            return widgets.handle_event(event)
        elif name.startswith("browser_"):
            from aqt import browser

            return browser.handle(name, event)
        elif name.startswith("editor_") or name == "add_cards_did_add_note":
            from aqt import editor

            return editor.handle(name, event)
        else:
            raise Exception(f"unknown event {name}")
        return None

    def _card(self, card_id: int) -> Any:
        return self.mw.col.get_card(card_id) if self.mw.col else None


def _native() -> Native:
    import aqt

    assert aqt.mw
    return aqt.mw.native


def open_panel(
    module: str,
    path: str,
    title: str,
    width: int,
    height: int,
    on_message: Callable[[str], Any],
    on_closed: Callable[[], None] | None = None,
) -> Panel:
    """Show `/_addons/<module>/<path>` in a shell window. The page's
    `pycmd(text, cb)` calls `on_message(text)` on the main loop, and `cb`
    gets its (JSON-serialisable) result."""
    return _native().open_panel(
        module, path, title, width, height, on_message, on_closed
    )


def save_file(default_name: str, data: bytes) -> str | None:
    """Save `data` in ~/Downloads under `default_name` (made unique), and
    return the path."""
    folder = os.path.expanduser("~/Downloads")
    stem, ext = os.path.splitext(default_name)
    path = os.path.join(folder, default_name)
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(folder, f"{stem} {n}{ext}")
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError as exc:
        _native().notify("show_info", text=f"Saving {path} failed: {exc}")
        return None
    return path
