# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""aqt.operations.note: the note ops the supported add-ons use."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from anki.collection import OpChanges, OpChangesWithCount
from anki.decks import DeckId
from anki.notes import Note

from . import CollectionOp


def add_note(
    *, parent: Any, note: Note, target_deck_id: DeckId
) -> CollectionOp[OpChangesWithCount]:
    return CollectionOp(parent, lambda col: col.add_note(note, target_deck_id))


def update_note(*, parent: Any, note: Note) -> CollectionOp[OpChanges]:
    return CollectionOp(parent, lambda col: col.update_note(note))


def update_notes(*, parent: Any, notes: Sequence[Note]) -> CollectionOp[OpChanges]:
    return CollectionOp(parent, lambda col: col.update_notes(notes))
