# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""The add-on's web panels, driven through a fake host with the shell's
contract. The other Frontier tests import FakeHost from here."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))
from chinese_frontier import panels  # noqa: E402

PREFIX = "frontierPanel.receive("


class Page:
    """One open panel: the host handle, and the page's view of the form."""

    def __init__(self, title, width, height, on_message, on_closed) -> None:
        self.title, self.size = title, (width, height)
        self.on_message, self.on_closed = on_message, on_closed
        self.received: list[dict[str, Any]] = []
        self.closed = False

    # The host contract: aqt.native.open_panel's handle.
    def eval(self, js: str) -> None:
        assert js.startswith(PREFIX) and js.endswith(")")
        self.received.append(json.loads(js[len(PREFIX) : -1]))

    def close(self) -> None:
        self.closed = True

    # The page's side of the bridge.
    def post(self, message: dict[str, Any]) -> Any:
        # Replies cross the bridge as JSON.
        reply = json.loads(json.dumps(self.on_message(json.dumps(message))))
        if isinstance(reply, dict) and reply.get("type"):
            self.received.append(reply)
        return reply

    def load(self) -> Page:
        reply = self.post({"type": "ready"})
        self.received += reply["messages"]
        return self

    def act(self, name: str, **values: Any) -> Any:
        return self.post(
            {"type": "action", "name": name, "values": {**self.values(), **values}}
        )

    def change(self, name: str, **values: Any) -> Any:
        return self.post(
            {"type": "change", "name": name, "values": {**self.values(), **values}}
        )

    def fields(self) -> dict[str, dict[str, Any]]:
        """Each named field as the page shows it: the last render, updated."""
        renders = [i for i, m in enumerate(self.received) if m["type"] == "render"]
        assert renders, "nothing rendered"
        found: dict[str, dict[str, Any]] = {}

        def walk(specs: list[dict[str, Any]]) -> None:
            for spec in specs:
                if spec.get("name"):
                    found[spec["name"]] = dict(spec)
                walk(spec.get("body", []) + spec.get("corner", []))
                for tab in spec.get("tabs", []):
                    walk(tab["body"])

        walk(self.received[renders[-1]]["spec"]["body"])
        for message in self.received[renders[-1] + 1 :]:
            for name, props in message.get("fields", {}).items():
                found[name].update(props)
        return found

    def __getitem__(self, name: str) -> dict[str, Any]:
        return self.fields()[name]

    def values(self) -> dict[str, Any]:
        """What the page would send without the learner touching anything."""
        defaults = {
            "checkbox": False,
            "input": "",
            "textarea": "",
            "select": None,
            "checklist": [],
        }
        result = {}
        for name, spec in self.fields().items():
            if spec["kind"] in defaults:
                result[name] = spec.get("value", defaults[spec["kind"]])
            elif spec["kind"] == "list":
                result[name] = [] if spec.get("multiple") else None
        return result


class FakeHost:
    def __init__(self, monkeypatch) -> None:
        self.pages: list[Page] = []
        monkeypatch.setattr(panels, "open_host", self.open)
        monkeypatch.setattr(panels, "_open", set())

    def open(self, module, path, title, width, height, on_message, on_closed):
        assert (module, path) == ("chinese_frontier", "web/form.html")
        self.pages.append(Page(title, width, height, on_message, on_closed))
        return self.pages[-1]

    def page(self) -> Page:
        """The last panel opened, loaded."""
        return self.pages[-1].load()


def test_messages_before_the_page_is_ready_ride_on_its_reply(monkeypatch):
    host = FakeHost(monkeypatch)
    seen = []
    panel = panels.open_panel("Title", lambda m: seen.append(m) or {"ok": 1})
    panel.render(panels.field("text", "intro", text="<b>hi</b>"))
    panel.update({"intro": {"text": "two"}})
    (page,) = host.pages
    assert page.received == []
    page.load()
    assert [m["type"] for m in page.received] == ["render", "update"]
    assert page["intro"]["text"] == "two"
    # Once ready, messages go straight to the page; unchanged fields are not resent.
    panel.update({"intro": {"text": "two", "hidden": False}})
    assert page.received[-1] == {
        "type": "update",
        "fields": {"intro": {"hidden": False}},
    }
    panel.update({"intro": {"text": "two", "hidden": False}})
    assert len(page.received) == 3
    assert page.act("go") == {"ok": 1} and seen[0]["name"] == "go"


def test_a_reloaded_page_gets_the_form_again(monkeypatch):
    host = FakeHost(monkeypatch)
    panel = panels.open_panel("Title", lambda _m: None)
    panel.render(panels.field("text", "intro", text="one"))
    page = host.page()
    panel.update({"intro": {"text": "two"}})
    page.received.clear()
    page.load()
    assert page["intro"]["text"] == "one"
    # Updates after the reload start from scratch.
    panel.update({"intro": {"text": "two"}})
    assert page["intro"]["text"] == "two"


def test_closing_once_from_either_side(monkeypatch):
    host = FakeHost(monkeypatch)
    closed = []
    first = panels.open_panel("A", lambda _m: None, on_closed=lambda: closed.append(1))
    second = panels.open_panel("B", lambda _m: refuse())
    a, b = host.pages
    a.load().post({"type": "close"})
    assert a.closed and first.closed and closed == [1]
    first.close()
    a.on_closed()
    assert closed == [1]
    # The learner closed the window: nothing more reaches the handler.
    b.on_closed()
    assert second.closed and b.post({"type": "ready"}) is None
    second.send({"type": "update", "fields": {}})
    assert b.received == []


def test_profile_close_closes_every_panel(monkeypatch):
    host = FakeHost(monkeypatch)
    panels.open_panel("A", lambda _m: None)
    panels.open_panel("B", lambda _m: None)
    panels.close_all()
    assert all(page.closed for page in host.pages)


def refuse():
    raise AssertionError("handled a message after closing")


def test_updates_sent_as_replies_count_as_shown(monkeypatch):
    host = FakeHost(monkeypatch)
    panel = panels.open_panel(
        "Title", lambda _m: panels.update({"detail": {"text": "picked"}})
    )
    panel.render(panels.field("text", "detail", text="first"))
    page = host.page()
    page.act("pick")
    assert page["detail"]["text"] == "picked"
    # going back to the first value must reach the page
    panel.update({"detail": {"text": "first"}})
    assert page["detail"]["text"] == "first"
