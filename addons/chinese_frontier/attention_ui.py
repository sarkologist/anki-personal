"""Main-screen notice and dialog for cards that need the learner; see ATTENTION.md."""

from __future__ import annotations

from typing import Callable

import aqt
from anki.collection import Collection, OpChanges
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp
from aqt.qt import (
    QAbstractItemView,
    QAction,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    Qt,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import tooltip

from .attention import ACTIONS, Attention, banner, browser_search


class AttentionNotice:
    def __init__(
        self,
        language: str,
        compute: Callable[[], list[Attention]],
        resolve: Callable[[Collection, Attention], OpChanges],
    ) -> None:
        assert mw
        self.language = language
        self.command = f"{language.lower()}_frontier_attention"
        self.compute = compute
        self.resolve = resolve
        self.notified: set[tuple[str, int]] = set()
        self.action = QAction(f"{language} Frontier: needs attention…", mw)
        qconnect(self.action.triggered, self.open)
        mw.form.menuTools.addAction(self.action)
        gui_hooks.deck_browser_will_render_content.append(self.render)
        gui_hooks.webview_did_receive_js_message.append(self.message)

    def items(self) -> list[Attention]:
        return self.compute() if mw and mw.col else []

    def reset(self) -> None:
        self.notified.clear()

    def refresh(self) -> None:
        """Tell the learner once about items not seen before; redraw the notice."""
        if not mw or not mw.col:
            return
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
        if mw.state == "deckBrowser":
            mw.deckBrowser.refresh()

    def render(self, _deck_browser: object, content: object) -> None:
        if items := self.items():
            content.stats += banner(self.language, len(items), self.command)  # type: ignore[attr-defined]

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
        dialog = QDialog(mw)
        dialog.setWindowTitle(f"{self.language} Frontier: needs attention")
        dialog.resize(640, 360)
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "Automation cannot finish these cards. Open them in the Browser to edit "
            "them, or act on the selected ones."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        listing = QListWidget()
        listing.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        listing.setWordWrap(True)
        layout.addWidget(listing)
        buttons = QHBoxLayout()
        browse = QPushButton("Open in Browser")
        act = QPushButton()
        close = QPushButton("Close")
        for button in (browse, act, close):
            buttons.addWidget(button)
        layout.addLayout(buttons)

        def fill() -> None:
            listing.clear()
            for item in self.items():
                text = f"{item.word}: {item.reason}"
                entry = QListWidgetItem(
                    text if len(text) <= 240 else text[:239] + "…", listing
                )
                entry.setToolTip(text)
                entry.setData(Qt.ItemDataRole.UserRole, item)
            if not listing.count():
                QListWidgetItem("Nothing needs your attention.", listing)
            update()

        def selected() -> list[Attention]:
            return [
                item
                for entry in listing.selectedItems()
                if isinstance(item := entry.data(Qt.ItemDataRole.UserRole), Attention)
            ]

        def everything() -> list[Attention]:
            return [
                item
                for row in range(listing.count())
                if isinstance(
                    item := listing.item(row).data(Qt.ItemDataRole.UserRole), Attention
                )
            ]

        def update() -> None:
            labels = {ACTIONS[item.kind] for item in selected()}
            act.setVisible(len(labels) == 1)
            if len(labels) == 1:
                act.setText(labels.pop())
            browse.setEnabled(bool(everything()))

        def open_browser() -> None:
            if items := selected() or everything():
                aqt.dialogs.open("Browser", mw, search=(browser_search(items),))

        def act_on() -> None:
            items = selected()

            def op(col: Collection) -> OpChanges:
                changes = OpChanges()
                for item in items:
                    changes.MergeFrom(self.resolve(col, item))
                return changes

            def done(_changes: OpChanges) -> None:
                fill()
                self.refresh()

            CollectionOp(parent=dialog, op=op).success(done).run_in_background()

        qconnect(listing.itemSelectionChanged, update)
        qconnect(listing.itemDoubleClicked, lambda _entry: open_browser())
        qconnect(browse.clicked, open_browser)
        qconnect(act.clicked, act_on)
        qconnect(close.clicked, dialog.accept)
        fill()
        dialog.show()
