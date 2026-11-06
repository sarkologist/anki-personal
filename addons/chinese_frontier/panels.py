# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Dialogs as web pages, so one UI runs in Qt Anki and in the native shell.

Every panel is web/form.html, served from /_addons/<module>/web/. It renders a
form spec sent from Python and talks back with pycmd(json, callback): each
message is a dict, and on_message's return value is the reply.

Python to page: {"type": "render", "spec": {"body": [field, ...]}} replaces the
form; {"type": "update", "fields": {name: {prop: value}}} changes fields.
Page to Python: {"type": "action", "name": button, "values": {...}} for a
button, {"type": "change", "name": field, "values": {...}} for a field marked
"notify", and "ready"/"close", which the panel answers itself. A reply that is
itself a message is applied like one sent. Messages sent before the page is
ready ride on the reply to "ready"; so does the last render after a reload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

MODULE = __name__.split(".")[0]
PAGE = "web/form.html"

Message = dict[str, Any]
_open: set[Panel] = set()


def field(kind: str, name: str = "", **props: Any) -> dict[str, Any]:
    return {"kind": kind, "name": name, **props}


def update(fields: dict[str, dict[str, Any]]) -> Message:
    return {"type": "update", "fields": fields}


def shown(body: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each named field's properties in a spec."""
    result = {}
    for spec in body:
        if spec.get("name"):
            result[spec["name"]] = {
                k: v for k, v in spec.items() if k not in ("kind", "name")
            }
        result.update(shown(spec.get("body", []) + spec.get("corner", [])))
        for tab in spec.get("tabs", []):
            result.update(shown(tab["body"]))
    return result


class Panel:
    def __init__(
        self,
        on_message: Callable[[Message], Any],
        on_closed: Callable[[], None] | None,
    ) -> None:
        self.on_message = on_message
        self.on_closed = on_closed
        self.host: Any = None
        self.ready = False
        self.closed = False
        self.queued: list[Message] = []
        self.rendered: Message | None = None
        # What update() last sent, so a periodic refresh sends only changes.
        self.shown: dict[str, dict[str, Any]] = {}

    def send(self, message: Message) -> None:
        if self.closed:
            return
        if message.get("type") == "render":
            self.rendered, self.queued = message, []
            self.shown = shown(message["spec"]["body"])
            if not self.ready:
                return
        if not self.ready:
            self.queued.append(message)
            return
        self.host.eval(f"frontierPanel.receive({json.dumps(message)})")

    def render(self, *body: dict[str, Any], submit: str = "") -> None:
        """submit names the action Enter in a text input takes."""
        self.send({"type": "render", "spec": {"body": list(body), "submit": submit}})

    def update(self, fields: dict[str, dict[str, Any]]) -> None:
        """Send the field properties that changed since the last update."""
        changed: dict[str, dict[str, Any]] = {}
        for name, props in fields.items():
            last = self.shown.setdefault(name, {})
            if diff := {k: v for k, v in props.items() if last.get(k, ...) != v}:
                changed[name] = diff
                last.update(diff)
        if changed:
            self.send(update(changed))

    def close(self) -> None:
        if not self.closed:
            self.host.close()
            self.finished()

    def message(self, raw: str) -> Any:
        try:
            message = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(message, dict) or self.closed:
            return None
        if message.get("type") == "ready":
            if self.ready and self.rendered:
                # A reload shows the render alone.
                self.shown = shown(self.rendered["spec"]["body"])
            self.ready = True
            queued, self.queued = self.queued, []
            from aqt.theme import theme_manager

            return {
                "dark": bool(theme_manager.night_mode),
                "messages": ([self.rendered] if self.rendered else []) + queued,
            }
        if message.get("type") == "close":
            self.close()
            return None
        reply = self.on_message(message)
        # The page applies a reply that's a message, as if sent.
        if isinstance(reply, dict) and reply.get("type") == "update":
            for name, props in reply["fields"].items():
                self.shown.setdefault(name, {}).update(props)
        elif isinstance(reply, dict) and reply.get("type") == "render":
            self.rendered = reply
            self.shown = shown(reply["spec"]["body"])
        return reply

    def finished(self) -> None:
        """The host's window closed, by the learner or by close()."""
        if self.closed:
            return
        self.closed = True
        _open.discard(self)
        if self.on_closed:
            self.on_closed()


def open_panel(
    title: str,
    on_message: Callable[[Message], Any],
    *,
    width: int = 640,
    height: int = 480,
    on_closed: Callable[[], None] | None = None,
) -> Panel:
    panel = Panel(on_message, on_closed)
    panel.host = open_host(
        MODULE, PAGE, title, width, height, panel.message, panel.finished
    )
    _open.add(panel)
    return panel


def close_all() -> None:
    """A profile's panels must not act on the next profile's collection."""
    for panel in list(_open):
        panel.close()


def open_host(
    module: str,
    path: str,
    title: str,
    width: int,
    height: int,
    on_message: Callable[[str], Any],
    on_closed: Callable[[], None],
) -> Any:
    """A window showing the page: the shell's when running there, else Qt's.
    The handle has eval(js) and close()."""
    try:
        from aqt import native  # type: ignore[attr-defined]
    except ImportError:
        return QtHost(module, path, title, width, height, on_message, on_closed)
    return native.open_panel(module, path, title, width, height, on_message, on_closed)


class QtHost:
    def __init__(
        self,
        module: str,
        path: str,
        title: str,
        width: int,
        height: int,
        on_message: Callable[[str], Any],
        on_closed: Callable[[], None],
    ) -> None:
        from aqt import mw
        from aqt.qt import QDialog, Qt, QUrl, QVBoxLayout, qconnect
        from aqt.webview import AnkiWebView

        assert mw
        self.dialog = dialog = QDialog(mw)
        dialog.setWindowTitle(title)
        dialog.resize(width, height)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        self.web = AnkiWebView(dialog, title=title)
        # Otherwise the page hands an add-on URL to the system browser.
        self.web.page().open_links_externally = False
        self.web.set_bridge_command(on_message, dialog)
        layout.addWidget(self.web)

        def finished() -> None:
            # Release the view's hooks and media-server page with the dialog.
            self.web.cleanup()
            on_closed()

        qconnect(dialog.finished, finished)
        self.web.load_url(QUrl(f"{mw.serverURL()}_addons/{module}/{path}"))
        dialog.show()

    def eval(self, js: str) -> None:
        self.web.eval(js)

    def close(self) -> None:
        self.dialog.close()


def save_file(default_name: str, data: bytes) -> str | None:
    """Save an export; the path, or None when the learner cancels."""
    try:
        from aqt import native  # type: ignore[attr-defined]
    except ImportError:
        pass
    else:
        return native.save_file(default_name, data)
    from aqt import mw
    from aqt.qt import QFileDialog

    filename, _ = QFileDialog.getSaveFileName(
        mw, "Export Frontier diagnostics", default_name, "JSON (*.json)"
    )
    if filename:
        Path(filename).write_bytes(data)
    return filename or None


def install() -> None:
    from aqt import gui_hooks, mw

    assert mw
    mw.addonManager.setWebExports(MODULE, r"web/.*")
    gui_hooks.profile_will_close.append(close_all)
