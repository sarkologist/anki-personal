# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Collection access for proposals that create notes.

Card previews, the duplicate check, and the apply op. Everything here takes
``col`` and runs on the main thread (or inside a CollectionOp); the HTML is
assembled by surface.py.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Any

from .patches import HTML_TAG_RE, NoteCreate, PatchValidationError
from .surface import MAX_DUPLICATES_SHOWN, CardPreview, CreatePreview, DuplicateHit
from .thread_view import reviewer_card_html

MODEL_CLOZE = 1
UNDO_NAME = "Apply agent proposal"
DUPLICATE_EXCERPT_CHARS = 80


def new_note_for(col: Any, create: NoteCreate) -> Any:
    """An unsaved note of the proposed note type with the proposed content."""
    notetype = col.models.get(create.notetype_id)
    if not notetype:
        raise PatchValidationError(f"Note type {create.notetype_id} no longer exists.")
    note = col.new_note(notetype)
    for field in create.fields:
        note[field.name] = field.html
    note.tags = list(create.tags)
    return note


def duplicate_query(notetype_id: int, first_field_html: str) -> str:
    """Anki's dupe search, which compares the first field without HTML."""
    text = first_field_html.replace("\\", "\\\\").replace('"', '\\"')
    return f'"dupe:{notetype_id},{text}"'


def field_excerpt(value: str) -> str:
    text = " ".join(html.unescape(HTML_TAG_RE.sub(" ", value)).split())
    if len(text) > DUPLICATE_EXCERPT_CHARS:
        return text[: DUPLICATE_EXCERPT_CHARS - 1] + "…"
    return text


def find_duplicates(col: Any, create: NoteCreate) -> tuple[list[DuplicateHit], int]:
    """The first few notes whose first field matches, and how many there are."""
    if not create.fields:
        return [], 0
    note_ids = list(
        col.find_notes(duplicate_query(create.notetype_id, create.fields[0].html))
    )
    hits = []
    for note_id in note_ids[:MAX_DUPLICATES_SHOWN]:
        try:
            first = str(col.get_note(note_id).fields[0])
        except Exception:
            first = ""
        hits.append(DuplicateHit(int(note_id), field_excerpt(first)))
    return hits, len(note_ids)


def _card_previews(note: Any, notetype: dict[str, Any]) -> list[CardPreview]:
    templates = list(notetype.get("tmpls") or ())
    if notetype.get("type") == MODEL_CLOZE:
        name = str(templates[0].get("name", "Cloze")) if templates else "Cloze"
        numbers = list(note.cloze_numbers_in_fields()) or [1]
        cards = [(f"{name} {number}", number - 1) for number in numbers]
    else:
        cards = [
            (str(template.get("name", "")), int(template.get("ord", index)))
            for index, template in enumerate(templates)
        ]
    previews = []
    for name, ord in cards:
        card = note.ephemeral_card(ord)
        previews.append(
            CardPreview(
                name=name,
                question_html=reviewer_card_html(card.question),
                answer_html=reviewer_card_html(card.answer),
            )
        )
    return previews


def create_preview(col: Any, create: NoteCreate) -> CreatePreview:
    """Names, rendered cards and duplicates for a proposed note.

    Each part falls back on its own: a template that fails to render still
    leaves the field grid, the names and the duplicate warning.
    """
    notetype: dict[str, Any] = {}
    try:
        notetype = dict(col.models.get(create.notetype_id) or {})
    except Exception:
        pass
    notetype_name = str(notetype.get("name") or f"note type {create.notetype_id}")
    try:
        deck_name = str(col.decks.name(create.deck_id))
    except Exception:
        deck_name = f"deck {create.deck_id}"
    cards: tuple[CardPreview, ...] = ()
    try:
        if notetype:
            cards = tuple(_card_previews(new_note_for(col, create), notetype))
    except Exception:
        cards = ()
    try:
        duplicates, total = find_duplicates(col, create)
    except Exception:
        duplicates, total = [], 0
    return CreatePreview(
        notetype_name=notetype_name,
        deck_name=deck_name,
        first_field_name=create.fields[0].name if create.fields else "",
        cards=cards,
        duplicates=tuple(duplicates),
        duplicate_total=total,
    )


def apply_proposal_changes(
    col: Any,
    *,
    updates: Sequence[Any],
    creates: Sequence[NoteCreate],
    created_ids: list[int],
) -> Any:
    """Save updated notes and add new ones as a single undo step.

    The ids of the added notes are appended to ``created_ids``.
    """
    target = col.add_custom_undo_entry(UNDO_NAME)
    if updates:
        col.update_notes(list(updates))
    for create in creates:
        if create.deck_id is None:
            raise ValueError("A new note has no deck to be added to.")
    for create in creates:
        note = new_note_for(col, create)
        col.add_note(note, create.deck_id)
        created_ids.append(int(note.id))
    return col.merge_undo_entries(target)
