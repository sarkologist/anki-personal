# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from collections.abc import Callable

from anki.cards import Card
from aqt import AnkiQt
from aqt.browser.previewer import MultiCardPreviewer

from .patches import CardActionPatch, EditorSnapshot, NotePatch, PatchValidationError
from .proposal_cards import proposal_preview_cards


class ProposalPreviewer(MultiCardPreviewer):
    def __init__(
        self,
        *,
        mw: AnkiQt,
        snapshot: EditorSnapshot,
        patch: NotePatch | CardActionPatch,
        on_close: Callable[[], None],
    ) -> None:
        self._cards = proposal_preview_cards(mw.col, snapshot, patch)
        if not self._cards:
            raise PatchValidationError("The proposal has no cards to preview.")
        self._index = 0
        self._last_index = -1
        super().__init__(parent=None, mw=mw, on_close=on_close)

    def card(self) -> Card:
        return self._cards[self._index]

    def card_changed(self) -> bool:
        changed = self._index != self._last_index
        self._last_index = self._index
        return changed

    def _state_and_mod(self) -> tuple[str, int, int]:
        # Unsaved cards all have ID zero, and their notes must not be loaded
        # from the collection. The preview index identifies each rendered card.
        return (self._state, self._index, 0)

    def _on_prev_card(self) -> None:
        if self._index > 0:
            self._index -= 1
            self.render_card()

    def _on_next_card(self) -> None:
        if self._index < len(self._cards) - 1:
            self._index += 1
            self.render_card()

    def _should_enable_prev(self) -> bool:
        return super()._should_enable_prev() or self._index > 0

    def _should_enable_next(self) -> bool:
        return super()._should_enable_next() or self._index < len(self._cards) - 1

    def _render_scheduled(self) -> None:
        super()._render_scheduled()
        self._updateButtons()
        if self._open:
            self.setWindowTitle(
                f"Agent proposal — Card {self._index + 1} of {len(self._cards)}"
            )
