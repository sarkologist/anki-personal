# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The plugin host as the shell runs it: a subprocess on a socket, with a
probe add-on, against a fake shell that serves a real collection."""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from anki import remote
from anki.collection import Collection, OpChanges

REPO = Path(__file__).resolve().parents[3]
HOST = REPO / "shell/plugin_host/host.py"

PROBE = textwrap.dedent(
    """
    import aqt.native
    from aqt import gui_hooks, mw
    from aqt.operations import CollectionOp
    from aqt.qt import QAction, QTimer, qconnect
    from aqt.utils import tooltip

    seen = []

    def opened():
        seen.append(["opened", mw.col.note_count()])

    gui_hooks.profile_did_open.append(opened)
    gui_hooks.profile_will_close.append(lambda: seen.append(["closing"]))
    gui_hooks.theme_did_change.append(lambda: seen.append(["theme"]))
    gui_hooks.state_did_change.append(lambda new, old: seen.append(["state", new, old]))
    gui_hooks.operation_did_execute.append(
        lambda changes, initiator: seen.append(["op", changes.note_text, changes.notetype])
    )
    gui_hooks.reviewer_did_answer_card.append(
        lambda reviewer, card, ease: seen.append(["answered", card.id, ease])
    )

    def add_note():
        def op(col):
            note = col.newNote()
            note["Front"] = "from the plugin"
            return col.add_note(note, 1)

        CollectionOp(parent=mw, op=op).success(lambda _: tooltip("added")).run_in_background()

    action = QAction("Probe…", mw)
    qconnect(action.triggered, add_note)
    mw.form.menuTools.addAction(action)

    def context(reviewer, menu):
        item = menu.addAction(f"card {reviewer.card.id}")
        qconnect(item.triggered, lambda: tooltip("context"))

    gui_hooks.reviewer_will_show_context_menu.append(context)
    gui_hooks.deck_browser_will_render_content.append(
        lambda _browser, content: setattr(content, "stats", content.stats + "<b>probe</b>")
    )

    def message(handled, text, context):
        if text == "probe:seen":
            return (True, seen)
        if text == "probe:night":
            from aqt.theme import theme_manager
            return (True, theme_manager.night_mode)
        if text == "probe:latex":
            from anki import latex
            return (True, [latex.pngCommands, latex.svgCommands])
        if text == "probe:child":
            import subprocess
            return (True, subprocess.Popen(["sleep", "60"]).pid)
        if text == "probe:boom":
            raise Exception("boom")
        if text == "probe:panel":
            panel = aqt.native.open_panel(
                "probe", "web/page.html", "Probe", 300, 200,
                lambda m: {"echo": m}, lambda: seen.append(["panel closed"]),
            )
            panel.eval("hello()")
            return (True, panel.id)
        if text == "probe:timer":
            QTimer.singleShot(10, lambda: tooltip("timer"))
            return (True, None)
        if text == "probe:fill cache":
            mw.col.models.all()
        if text in ("probe:fill cache", "probe:cache"):
            return (True, len(mw.col.models._cache))
        if text == "probe:config":
            config = mw.addonManager.getConfig(__name__)
            config["n"] += 1
            mw.addonManager.writeConfig(__name__, config)
            return (True, config)
        return handled

    gui_hooks.webview_did_receive_js_message.append(message)
    mw.addonManager.setWebExports(__name__, r"web/.*")
    """
)


class FakeShell:
    """The shell's end: serves backend calls from its own collection,
    collects the host's notifications, and sends it events."""

    def __init__(self, col: Collection, server: socket.socket) -> None:
        self.col = col
        self.server = server
        self.notes: list[dict[str, Any]] = []
        self.taken: set[int] = set()
        self.arrived = threading.Condition()
        self.replies: dict[int, queue.Queue[tuple[int, bytes]]] = {}
        self.ids = iter(range(1, 1_000_000))
        self.write = threading.Lock()
        self.sock: socket.socket | None = None
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        self.sock, _ = self.server.accept()
        native = self.col._backend._backend
        while frame := remote.read_frame(self.sock):
            kind, id, payload = frame
            if kind in (remote.OK, remote.ERR):
                self.replies[id].put((kind, payload))
                continue
            if kind == remote.HOST:
                with self.arrived:
                    self.notes.append(json.loads(payload))
                    self.arrived.notify_all()
                if id:
                    self.send(remote.OK, id, b"null")
                continue
            try:
                if kind == remote.CALL:
                    out = native.command(*remote.split_call(payload))
                else:
                    out = native.db_command(payload)
                status = remote.OK
            except Exception as error:
                out, status = bytes(error.args[0]), remote.ERR
            self.send(status, id, out)

    def send(self, kind: int, id: int, payload: bytes) -> None:
        assert self.sock
        with self.write:
            remote.write_frame(self.sock, kind, id, payload)

    def event(self, name: str, **fields: Any) -> tuple[int, Any]:
        id = next(self.ids)
        self.replies[id] = queue.Queue()
        self.send(remote.EVENT, id, json.dumps({"event": name, **fields}).encode())
        kind, payload = self.replies[id].get(timeout=10)
        return kind, json.loads(payload) if kind == remote.OK else payload.decode()

    def message(self, text: str, context: str = "deckBrowser") -> Any:
        kind, out = self.event(
            "webview_did_receive_js_message", message=text, context=context
        )
        assert kind == remote.OK, out
        return out

    def wait_for(self, op: str, timeout: float = 10) -> dict[str, Any]:
        """The first `op` from the host not returned before."""
        deadline = time.time() + timeout
        with self.arrived:
            while True:
                for i, note in enumerate(self.notes):
                    if i not in self.taken and note["op"] == op:
                        self.taken.add(i)
                        return note
                left = deadline - time.time()
                if left <= 0:
                    raise AssertionError(f"no {op} from the plugin host")
                self.arrived.wait(left)


@pytest.fixture
def host() -> Iterator[tuple[FakeShell, subprocess.Popen, Path]]:
    # Unix socket paths must be short
    folder = Path(tempfile.mkdtemp(prefix="ph", dir="/tmp"))
    base = folder / "base"
    profile = base / "User 1"
    profile.mkdir(parents=True)
    probe = base / "addons21/probe"
    probe.mkdir(parents=True)
    (probe / "__init__.py").write_text(PROBE)
    (probe / "config.json").write_text('{"n": 1}')
    col = Collection(str(profile / "collection.anki2"))
    note = col.newNote()
    note["Front"] = "existing"
    col.add_note(note, 1)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(folder / "s"))
    server.listen(1)
    shell = FakeShell(col, server)
    paths = [str(REPO / "pylib"), str(REPO / "out/pylib"), str(REPO / "out/qt")]
    proc = subprocess.Popen(
        [sys.executable, "-I", "-u", str(HOST), "--socket", str(folder / "s")]
        + ["--base", str(base), "--profile", "User 1", "--addon", "probe"]
        + ["--collection", col.path]
        + [arg for path in paths for arg in ("--path", path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # as the shell starts it
        process_group=0,
    )
    try:
        assert shell.wait_for("ready")["addons"] == ["probe"]
        yield shell, proc, probe
    finally:
        proc.kill()
        print(proc.communicate()[0])
        col.close()
        shutil.rmtree(folder)


def test_add_ons_load_before_the_profile_opens_on_the_shells_collection(host):
    shell, _proc, _probe = host
    assert shell.message("probe:seen") == {"handled": True, "value": [["opened", 1]]}


def test_menu_items_reach_the_shell_and_their_ops_report_changes(host):
    shell, _proc, _probe = host
    menu = shell.wait_for("menu")
    assert menu["menu"] == "tools"
    (item,) = menu["items"]
    assert item["text"] == "Probe…"
    shell.event("menu_action", id=item["id"])
    changes = OpChanges()
    changes.ParseFromString(
        base64.b64decode(shell.wait_for("operation_did_execute")["changes"])
    )
    assert changes.note_text
    assert shell.col.find_notes("from") != []
    assert shell.wait_for("tooltip")["text"] == "added"
    # the add-on's own hook saw it too
    assert ["op", True, False] in shell.message("probe:seen")["value"]


def test_the_shells_operations_reach_the_hooks_and_clear_notetype_caches(host):
    shell, _proc, _probe = host
    assert shell.message("probe:fill cache")["value"] > 0
    changes = OpChanges(notetype=True)
    shell.event(
        "operation_did_execute",
        changes=base64.b64encode(changes.SerializeToString()).decode(),
    )
    assert ["op", False, True] in shell.message("probe:seen")["value"]
    assert shell.message("probe:cache")["value"] == 0


def test_reviewer_hooks_get_the_card(host):
    shell, _proc, _probe = host
    card_id = shell.col.find_cards("")[0]
    kind, items = shell.event("reviewer_will_show_context_menu", card_id=card_id)
    assert kind == remote.OK and [i["text"] for i in items] == [f"card {card_id}"]
    shell.event("menu_action", id=items[0]["id"])
    assert shell.wait_for("tooltip")["text"] == "context"
    shell.event("reviewer_did_answer_card", card_id=card_id, ease=3)
    shell.event("state_did_change", new="review", old="overview")
    seen = shell.message("probe:seen")["value"]
    assert ["answered", card_id, 3] in seen and ["state", "review", "overview"] in seen


def test_add_ons_see_the_shells_theme(host):
    shell, _proc, _probe = host
    shell.event("set_theme", night=True)
    assert shell.message("probe:night")["value"] is True
    assert ["theme"] not in shell.message("probe:seen")["value"]
    shell.event("theme_did_change", night=False)
    assert shell.message("probe:night")["value"] is False
    assert ["theme"] in shell.message("probe:seen")["value"]


def test_add_ons_render_latex_with_the_shells_toolchain(host):
    shell, _proc, _probe = host
    png = [["latex", "tmp.tex"], ["dvipng", "tmp.dvi", "-o", "tmp.png"]]
    svg = [["pdflatex", "tmp.tex"], ["pdf2svg", "tmp.pdf", "tmp.svg"]]
    shell.event("set_latex_commands", png=png, svg=svg)
    assert shell.message("probe:latex")["value"] == [png, svg]


def test_the_deck_browser_gets_the_add_ons_html(host):
    shell, _proc, _probe = host
    assert shell.event("deck_browser_will_render_content") == (
        remote.OK,
        {"stats": "<b>probe</b>"},
    )


def test_unhandled_page_messages_say_so(host):
    shell, _proc, _probe = host
    assert shell.message("someone else's") == {"handled": False, "value": None}


def test_a_failing_hook_fails_its_event_but_not_the_host(host):
    shell, _proc, _probe = host
    kind, error = shell.event(
        "webview_did_receive_js_message", message="probe:boom", context=""
    )
    assert kind == remote.ERR and "boom" in error
    # as in Anki, the failing hook is dropped; the host carries on
    assert shell.event("deck_browser_will_render_content")[0] == remote.OK


def test_panels_round_trip_messages(host):
    shell, _proc, _probe = host
    panel = shell.message("probe:panel")["value"]
    opened = shell.wait_for("open_panel")
    assert (opened["panel"], opened["module"], opened["path"]) == (
        panel,
        "probe",
        "web/page.html",
    )
    assert shell.wait_for("panel_eval")["js"] == "hello()"
    assert shell.event("panel_message", panel=panel, message="hi") == (
        remote.OK,
        {"echo": "hi"},
    )
    shell.event("panel_closed", panel=panel)
    assert ["panel closed"] in shell.message("probe:seen")["value"]


def test_web_exports_and_timers(host):
    shell, _proc, _probe = host
    assert shell.wait_for("web_exports") == {
        "op": "web_exports",
        "module": "probe",
        "pattern": "web/.*",
    }
    shell.message("probe:timer")
    assert shell.wait_for("tooltip")["text"] == "timer"


def test_config_is_shared_with_anki_through_meta_json(host):
    shell, _proc, probe = host
    assert shell.message("probe:config")["value"] == {"n": 2}
    assert json.loads((probe / "meta.json").read_text())["config"] == {"n": 2}
    assert shell.message("probe:config")["value"] == {"n": 3}


def test_profile_close_runs_the_hooks_then_the_host_exits_with_the_shell(host):
    shell, proc, _probe = host
    child = shell.message("probe:child")["value"]
    assert shell.event("profile_will_close") == (remote.OK, None)
    assert ["closing"] in shell.message("probe:seen")["value"]
    assert shell.sock
    shell.sock.shutdown(socket.SHUT_RDWR)
    assert proc.wait(timeout=10) == 0
    # what the add-on started goes too, even if the shell can't end it
    deadline = time.time() + 5
    while time.time() < deadline and _alive(child):
        time.sleep(0.05)
    assert not _alive(child)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
