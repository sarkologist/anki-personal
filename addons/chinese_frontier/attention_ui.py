# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Main-screen notice and dialog for cards that need the learner; see ATTENTION.md."""

from __future__ import annotations

import traceback
from typing import Any, Callable

import aqt
from anki.collection import Collection, OpChanges
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp
from aqt.qt import QAction, qconnect
from aqt.utils import tooltip

from . import panels
from .attention import ACTIONS, Attention, banner, browser_search
from .panels import field


class AttentionNotice:
    def __init__(
        self,
        language: str,
        compute: Callable[[], list[Attention]],
        resolve: Callable[[Collection, Attention], OpChanges],
        configured: Callable[[], bool],
    ) -> None:
        assert mw
        self.language = language
        # A language without a chosen deck stays off the main screen.
        self.configured = configured
        self.command = f"{language.lower()}_frontier_attention"
        self.compute = compute
        self.resolve = resolve
        self.notified: set[tuple[str, int]] = set()
        # Recomputed on refresh, not on every main-screen render.
        self.cached: list[Attention] = []
        self.action = QAction(f"{language} Frontier: needs attention…", mw)
        qconnect(self.action.triggered, self.open)
        mw.form.menuTools.addAction(self.action)
        gui_hooks.deck_browser_will_render_content.append(self.render)
        gui_hooks.webview_did_receive_js_message.append(self.message)

    def items(self) -> list[Attention]:
        """A failure here must not stop generation or the main screen."""
        if not mw or not mw.col:
            return []
        try:
            self.cached = self.compute()
        except Exception:
            traceback.print_exc()
            self.cached = []
        return self.cached

    def reset(self) -> None:
        self.notified.clear()
        self.cached = []

    def refresh(self) -> None:
        """Tell the learner once about items not seen before; redraw the notice."""
        if not mw or not mw.col:
            return
        before = len(self.cached)
        items = self.items()
        self.action.setText(
            f"{self.language} Frontier: needs attention ({len(items)})…"
        )
        new = {(item.kind, item.card_id) for item in items} - self.notified
        if new:
            cards = "card needs" if len(new) == 1 else "cards need"
            tooltip(
                f"{self.language} Frontier: {len(new)} {cards} your attention; "
                "see the main screen.",
                period=8000,
            )
        self.notified |= new
        if mw.state == "deckBrowser" and len(items) != before:
            mw.deckBrowser.refresh()

    def render(self, _deck_browser: object, content: object) -> None:
        if mw and mw.col and self.configured():
            content.stats += banner(self.language, len(self.cached), self.command)  # type: ignore[attr-defined]

    def message(
        self, handled: tuple[bool, object], message: str, _context: object
    ) -> tuple[bool, object]:
        if message != self.command:
            return handled
        self.open()
        return (True, None)

    def open(self) -> None:
        if not mw or not mw.col:
            return
        # The listed rows; the page's selection holds their indexes.
        rows: list[Attention] = []

        def listing() -> dict[str, dict[str, Any]]:
            rows[:] = self.items()
            items: list[dict[str, Any]] = []
            for index, item in enumerate(rows):
                text = f"{item.word}: {item.reason}"
                label = text if len(text) <= 240 else text[:239] + "…"
                items.append({"value": index, "label": label, "title": text})
            if not items:
                items.append(
                    {
                        "value": None,
                        "label": "Nothing needs your attention.",
                        "disabled": True,
                    }
                )
            return {"items": {"items": items}, **buttons([])}

        def selected(values: dict[str, Any]) -> list[Attention]:
            return [
                rows[index]
                for index in values.get("items") or []
                if isinstance(index, int) and 0 <= index < len(rows)
            ]

        def buttons(chosen: list[Attention]) -> dict[str, dict[str, Any]]:
            labels = {ACTIONS[item.kind] for item in chosen}
            return {
                "act": {
                    "hidden": len(labels) != 1,
                    "label": next(iter(labels)) if len(labels) == 1 else "",
                },
                "browse": {"disabled": not rows},
            }

        def act_on(chosen: list[Attention]) -> None:
            labels = {ACTIONS[item.kind] for item in chosen}
            if len(labels) != 1:
                return
            label = labels.pop()
            # Rows can go stale while the panel is open; act only on current ones.
            current = set(self.items())
            items = [item for item in chosen if item in current]

            def op(col: Collection) -> OpChanges:
                undo = col.add_custom_undo_entry(
                    f"{self.language} Frontier: {label.lower()}"
                )
                for item in items:
                    self.resolve(col, item)
                return col.merge_undo_entries(undo)

            def done(_changes: OpChanges) -> None:
                if mw and mw.col:
                    panel.send(panels.update(listing()))
                    self.refresh()

            CollectionOp(parent=mw, op=op).success(done).run_in_background()

        def message(message: dict[str, Any]) -> Any:
            chosen = selected(message.get("values", {}))
            name = message.get("name")
            if message.get("type") == "change":
                return panels.update(buttons(chosen))
            if name == "browse":
                if items := chosen or list(rows):
                    aqt.dialogs.open("Browser", mw, search=(browser_search(items),))
            elif name == "act":
                act_on(chosen)
            elif name == "close":
                panel.close()
            return None

        panel = panels.open_panel(
            f"{self.language} Frontier: needs attention",
            message,
            width=640,
            height=360,
        )
        shown = listing()
        panel.render(
            field(
                "text",
                text="Automation cannot finish these cards. Open them in the "
                "Browser to edit them, or act on the selected ones.",
            ),
            field(
                "list",
                "items",
                multiple=True,
                notify=True,
                activate="browse",
                height=220,
                **shown["items"],
            ),
            field(
                "row",
                align="end",
                body=[
                    field(
                        "button", "browse", label="Open in Browser", **shown["browse"]
                    ),
                    field("button", "act", **shown["act"]),
                    field("button", "close", label="Close"),
                ],
            ),
        )
