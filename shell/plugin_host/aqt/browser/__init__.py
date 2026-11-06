# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The shell's card browser, as add-ons see it (`aqt.browser.Browser`).

The shell sends the browser hooks as events: `browser_will_show` (which
also runs `browser_did_fetch_columns` and `browser_menus_did_init`),
`browser_will_search`, `browser_did_search`, `browser_did_fetch_row` (for
rows in batches) and `browser_closed`. What an add-on asks of the browser
outside those (the search, the selection) comes from the shell on request.
During the hooks, the browser's mode is the event's.

`browser_will_show` and `browser_did_change_row` hooks aren't run: the only
add-on using them is the editor agent pane's browser pane, which isn't in
the shell."""

from __future__ import annotations

import base64
from typing import Any

import aqt
from anki.cards import Card
from anki.collection import BrowserRow

from ..main import Menu, report_exception
from ..qt import QObject
from ..widgets import Signal
from .table import Column, SearchContext

__all__ = ["Browser", "SearchContext", "handle"]

# hooks the shell needn't send when no add-on uses them
HOOKS = [
    "browser_will_search",
    "browser_did_search",
    "browser_did_fetch_row",
]


def _native() -> Any:
    assert aqt.mw
    return aqt.mw.native


class _Header:
    """The table has no Qt header: a healthy, empty one."""

    def count(self) -> int:
        return 0

    def length(self) -> int:
        return 0


class _TableView:
    def horizontalHeader(self) -> _Header:
        return _Header()


class _Form:
    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self.menubar = self
        self.tableView = _TableView()

    def addMenu(self, title: str) -> Menu:
        """A menu in the browser's toolbar."""
        assert aqt.mw
        return Menu(f"browser:{title.replace('&', '')}", aqt.mw)


class _ItemState:
    def __init__(self, browser: Browser) -> None:
        self.browser = browser

    def is_notes_mode(self) -> bool:
        if self.browser._notes_mode is not None:
            return self.browser._notes_mode
        return bool(self.browser._state().get("notes_mode"))

    @property
    def sort_column(self) -> str:
        return str(self.browser._state().get("sort_column", ""))

    @property
    def active_columns(self) -> list[str]:
        if self.browser._columns is not None:
            return self.browser._columns
        return list(self.browser._state().get("columns", []))


class _Model:
    def __init__(self, browser: Browser) -> None:
        self._state = _ItemState(browser)
        self.modelReset = Signal()

    def active_column_index(self, column: str) -> int | None:
        columns = self._state.active_columns
        return columns.index(column) if column in columns else None


class _Table:
    def __init__(self, browser: Browser) -> None:
        self.browser = browser
        self._state = _ItemState(browser)
        self._model = _Model(browser)

    def is_notes_mode(self) -> bool:
        return self._state.is_notes_mode()

    def _reverse(self) -> None:
        # the shell sorts add-on columns again by searching
        self.browser.search()

    def _on_column_toggled(self, checked: bool, column: str) -> None:
        """Show or hide a column (the header menu's)."""
        _native().notify("browser_column", key=column, shown=bool(checked))

    def _on_sort_column_changed(self, section: int | None, order: Any) -> None:
        """Sort by the column at `section` (the header's), then search."""
        columns = self._state.active_columns
        if section is None or not 0 <= section < len(columns):
            return
        from ..qt import Qt

        _native().notify(
            "browser_sort",
            key=columns[section],
            descending=order == Qt.SortOrder.DescendingOrder,
        )

    def len(self) -> int:
        return int(self.browser._state().get("count", 0))

    def len_selection(self) -> int:
        return len(self.browser._state().get("selected", []))

    def get_current_card(self) -> Card | None:
        cid = self.browser._state().get("current_card")
        col = self.browser.col
        return col.get_card(cid) if cid and col else None

    def get_current_note(self) -> Any:
        card = self.get_current_card()
        return card.note() if card else None

    def get_selected_card_ids(self) -> list[int]:
        return list(self.browser._state().get("selected_cards", []))

    def get_selected_note_ids(self) -> list[int]:
        state = self.browser._state()
        if state.get("notes_mode"):
            return list(state.get("selected", []))
        cards = state.get("selected_cards", [])
        col = self.browser.col
        if not cards or not col:
            return []
        return col.db.list(
            f"select distinct nid from cards where id in ({','.join(map(str, cards))})"
        )

    def get_single_selected_card(self) -> Card | None:
        state = self.browser._state()
        cards = state.get("selected", [])
        return self.get_current_card() if len(cards) == 1 else None


class Browser(QObject):
    def __init__(self) -> None:
        super().__init__(None)
        assert aqt.mw
        self.mw = aqt.mw
        self.form = _Form(self)
        self.table = _Table(self)
        self.editor = None
        self._deleted = False
        # set during the hooks, from the event
        self._notes_mode: bool | None = None
        self._columns: list[str] | None = None
        self._context: SearchContext | None = None

    @property
    def col(self) -> Any:
        return self.mw.col

    def _state(self) -> dict[str, Any]:
        if self._deleted:
            return {}
        return _native().request("browser_state") or {}

    @property
    def card(self) -> Card | None:
        return self.table.get_current_card()

    def isVisible(self) -> bool:
        return not self._deleted

    def activateWindow(self) -> None:
        pass

    def search(self) -> None:
        """Search again for what's shown."""
        if not self._deleted:
            _native().notify("browser_research")

    def search_for(self, search: str, prompt: str | None = None) -> None:
        _native().notify("browser_search", search=search)

    def search_for_terms(self, *terms: Any) -> None:
        col = self.col
        if col:
            self.search_for(col.build_search_string(*terms))

    def current_search(self) -> str:
        return str(self._state().get("search", ""))

    def selected_cards(self) -> list[int]:
        return self.table.get_selected_card_ids()

    def selected_notes(self) -> list[int]:
        return self.table.get_selected_note_ids()

    selectedCards = selected_cards
    selectedNotes = selected_notes


_browser: Browser | None = None


def current() -> Browser | None:
    return _browser


def _run(hook: Any, *args: Any) -> None:
    """Every handler, even after one fails (Qt drops a failing one)."""
    for handler in list(hook._hooks):
        try:
            handler(*args)
        except Exception:
            report_exception(getattr(handler, "__qualname__", repr(handler)))


def _column_json(column: Column) -> dict[str, Any]:
    return {
        "key": column.key,
        "cards_label": column.cards_mode_label,
        "notes_label": column.notes_mode_label,
        "cards_tooltip": column.cards_mode_tooltip,
        "notes_tooltip": column.notes_mode_tooltip,
        "sorting_cards": int(column.sorting_cards),
        "sorting_notes": int(column.sorting_notes),
        "alignment": int(column.alignment),
        "uses_cell_font": column.uses_cell_font,
    }


def handle(name: str, event: dict[str, Any]) -> Any:
    from aqt import gui_hooks

    global _browser
    browser = _browser
    if name == "browser_will_show":
        if browser:
            browser._deleted = True
        browser = _browser = Browser()
        builtin = [str(k) for k in event.get("columns", [])]
        columns: dict[str, Column] = {key: Column(key=key) for key in builtin}
        _run(gui_hooks.browser_did_fetch_columns, columns)
        _run(gui_hooks.browser_menus_did_init, browser)
        return {
            "columns": [
                _column_json(c) for k, c in columns.items() if k not in builtin
            ],
            "hooks": [h for h in HOOKS if getattr(gui_hooks, h)._hooks],
        }
    if browser is None:
        return None
    if name == "browser_closed":
        browser._deleted = True
        _browser = None
        return None
    notes_mode = bool(event.get("notes_mode"))
    browser._notes_mode = notes_mode
    try:
        if name == "browser_will_search":
            order = event.get("order")
            context = SearchContext(
                search=str(event.get("search", "")),
                browser=browser,
                order=Column(key=order) if order else False,
                reverse=bool(event.get("reverse")),
                addon_metadata={},
                ids=None,
            )
            _run(gui_hooks.browser_will_search, context)
            browser._context = context
            new_order = getattr(context.order, "key", None) or None
            changed = (
                bool(context.addon_metadata)
                or context.ids is not None
                or new_order != order
                or context.search != event.get("search")
                or context.reverse != bool(event.get("reverse"))
            )
            return {
                "changed": changed,
                "search": context.search,
                "order": new_order,
                "reverse": context.reverse,
                "ids": list(context.ids) if context.ids is not None else None,
            }
        if name == "browser_did_search":
            context = browser._context or SearchContext(
                search=str(event.get("search", "")), browser=browser, addon_metadata={}
            )
            context.ids = list(event.get("ids") or [])
            _run(gui_hooks.browser_did_search, context)
            browser._context = None
            return {"ids": list(context.ids or [])}
        if name == "browser_did_fetch_row":
            columns = [str(c) for c in event.get("columns", [])]
            browser._columns = columns
            out = []
            for item in event.get("rows", []):
                row = BrowserRow()
                row.ParseFromString(base64.b64decode(item["row"]))
                _run(
                    gui_hooks.browser_did_fetch_row,
                    int(item["id"]),
                    notes_mode,
                    row,
                    columns,
                )
                out.append(base64.b64encode(row.SerializeToString()).decode())
            return out
    finally:
        browser._notes_mode = None
        browser._columns = None
    raise Exception(f"unknown browser event {name}")
