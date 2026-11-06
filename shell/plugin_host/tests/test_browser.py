# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The browser hooks in this process: the real suspended_card_priority
add-on on a scratch collection, through the events the shell sends."""

from __future__ import annotations

import base64
import importlib.machinery
import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[3]


class ShimPackages:
    PATHS = [str(REPO / "shell/plugin_host"), str(REPO / "out/qt")]

    def find_spec(self, name: str, path: object, target: object = None) -> Any:
        if name in ("aqt", "_aqt"):
            return importlib.machinery.PathFinder.find_spec(name, self.PATHS)
        return None


sys.meta_path.insert(0, ShimPackages())
sys.path.insert(0, str(REPO / "addons"))

import aqt  # noqa: E402
from anki.collection import BrowserRow, Collection  # noqa: E402
from aqt import browser as browsers  # noqa: E402
from aqt import gui_hooks  # noqa: E402
from aqt.main import MainWindow, ProfileManager  # noqa: E402

KEY = "suspendedCardPriority"


class FakeNative:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.answers: dict[str, Any] = {}
        self.actions: dict[str, Any] = {}

    def notify(self, op: str, **fields: Any) -> None:
        self.sent.append((op, fields))

    def request(self, op: str, **fields: Any) -> Any:
        self.sent.append((op, fields))
        return self.answers.get(op)

    def menu_changed(self, menu: Any) -> None:
        for action in menu.actions:
            self.actions[action.text()] = action
        self.notify("menu", menu=menu.name, items=[a.text() for a in menu.actions])

    def ops(self, op: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.sent if name == op]


@pytest.fixture
def mw() -> Iterator[MainWindow]:
    with tempfile.TemporaryDirectory() as folder:
        mw = MainWindow(FakeNative(), str(Path(folder) / "addons21"))
        aqt.mw = mw
        mw.col = Collection(str(Path(folder) / "collection.anki2"))
        mw.pm = ProfileManager("test", folder, folder)
        try:
            yield mw
        finally:
            browsers.handle("browser_closed", {})
            gui_hooks.profile_will_close()
            mw.col.close()
            aqt.mw = None


@pytest.fixture
def addon(mw: MainWindow) -> Any:
    # installs its hooks once, at import (as at Anki's startup)
    import suspended_card_priority.browser as addon  # noqa: F401

    if addon._load not in gui_hooks.profile_did_open._hooks:
        addon.install()
    return addon


def suspended_cards(mw: MainWindow, n: int) -> list[int]:
    col = mw.col
    ids = []
    for i in range(n):
        note = col.newNote()
        note["Front"] = f"question {i}"
        note["Back"] = f"answer {i}"
        col.add_note(note, 1)
        ids.extend(note.card_ids())
    col.sched.suspend_cards(ids)
    return ids


def score(mw: MainWindow, scores: dict[int, int | None]) -> None:
    from suspended_card_priority.core import CardText

    out = {}
    for cid, value in scores.items():
        card = mw.col.get_card(cid)
        text = CardText.from_html(cid, card.nid, card.question(), card.answer())
        out[str(cid)] = {
            "score": value,
            "fingerprint": text.fingerprint,
            "reason": "test",
            "conceptual": 1,
            "computation": 1,
            "overlap": 0,
            "confidence": "high",
            "matches": [],
        }
    path = Path(mw.pm.profileFolder()) / "suspended-card-priority.json"
    path.write_text(json.dumps({"version": 1, "scores": out}))
    gui_hooks.profile_did_open()


def open_browser() -> dict[str, Any]:
    return browsers.handle("browser_will_show", {"columns": ["noteFld", "cardDue"]})


def test_opening_the_browser_gets_the_column_menu_and_hooks(mw, addon):
    out = open_browser()
    (column,) = out["columns"]
    assert column["key"] == KEY
    assert column["cards_label"] == "Priority"
    # BrowserColumns.SORTING_DESCENDING, SORTING_NONE, ALIGNMENT_CENTER
    assert (column["sorting_cards"], column["sorting_notes"], column["alignment"]) == (
        2,
        0,
        1,
    )
    assert sorted(out["hooks"]) == sorted(browsers.HOOKS)
    menu = mw.native.ops("menu")[-1]
    assert menu["menu"] == "browser:Priority"
    assert "Explain current priority" in menu["items"]


def test_sorting_by_priority_puts_unscored_cards_last(mw, addon):
    a, b, c = suspended_cards(mw, 3)
    score(mw, {a: 10, b: 90})
    open_browser()
    out = browsers.handle(
        "browser_will_search",
        {"search": "is:suspended", "order": KEY, "reverse": True, "notes_mode": False},
    )
    # rslib can't sort by it: the add-on does, after the search
    assert out["changed"] and out["order"] is None
    out = browsers.handle("browser_did_search", {"ids": [a, b, c], "notes_mode": False})
    assert out["ids"] == [b, a, c]
    # ascending keeps the unscored card last too
    browsers.handle(
        "browser_will_search",
        {"search": "is:suspended", "order": KEY, "reverse": False, "notes_mode": False},
    )
    out = browsers.handle("browser_did_search", {"ids": [c, b, a], "notes_mode": False})
    assert out["ids"] == [a, b, c]


def test_other_sorts_are_left_alone(mw, addon):
    open_browser()
    out = browsers.handle(
        "browser_will_search",
        {"search": "", "order": "noteFld", "reverse": False, "notes_mode": False},
    )
    assert out == {
        "changed": False,
        "search": "",
        "order": "noteFld",
        "reverse": False,
        "ids": None,
    }


def test_rows_show_the_scores(mw, addon):
    a, b, c = suspended_cards(mw, 3)
    score(mw, {a: 42, b: None})
    open_browser()
    rows = []
    for cid in (a, b, c):
        row = BrowserRow()
        row.cells.add(text="front")
        row.cells.add(text="")
        rows.append(
            {"id": cid, "row": base64.b64encode(row.SerializeToString()).decode()}
        )
    out = browsers.handle(
        "browser_did_fetch_row",
        {"rows": rows, "notes_mode": False, "columns": ["noteFld", KEY]},
    )
    texts = []
    for encoded in out:
        row = BrowserRow()
        row.ParseFromString(base64.b64decode(encoded))
        texts.append([cell.text for cell in row.cells])
    assert texts == [["front", "42"], ["front", "Review"], ["front", ""]]


def test_menu_actions_ask_the_shell_for_the_browser(mw, addon):
    (a,) = suspended_cards(mw, 1)
    score(mw, {a: 77})
    open_browser()
    mw.native.answers["browser_state"] = {
        "current_card": a,
        "search": "is:suspended",
        "count": 1,
    }
    assert browsers.current().table.len() == 1
    mw.native.actions["Explain current priority"].trigger()
    (info,) = mw.native.ops("show_info")
    assert info["text"].startswith("Priority 77/100")
    assert "<" not in info["text"]
    # the column, the search, then sorting by it, in that order
    mw.native.answers["browser_state"] = {"columns": ["noteFld"], "notes_mode": False}
    mw.native.sent.clear()

    def shown(op: str, **fields: Any) -> None:
        mw.native.sent.append((op, fields))
        if op == "browser_column":
            mw.native.answers["browser_state"]["columns"].append(fields["key"])

    mw.native.notify = shown
    mw.native.actions["Show Math priority queue"].trigger()
    sent = [(op, f) for op, f in mw.native.sent if op != "browser_state"]
    assert sent == [
        ("browser_column", {"key": KEY, "shown": True}),
        ("browser_search", {"search": "deck:math is:suspended"}),
        ("browser_sort", {"key": KEY, "descending": True}),
    ]


def test_a_closed_browser_is_deleted(mw, addon):
    from aqt.qt import sip

    open_browser()
    browser = browsers.current()
    assert not sip.isdeleted(browser)
    browsers.handle("browser_closed", {})
    assert sip.isdeleted(browser)
    assert browsers.handle("browser_will_search", {"search": ""}) is None
