# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any

TAG_RE = re.compile(r"^[^\s]+$")
DOCUMENT_HTML_RE = re.compile(
    r"<\s*!doctype\b|<\s*/?\s*(?:html|head|body)\b",
    re.IGNORECASE,
)
CLIPBOARD_FRAGMENT_MARKERS = ("<!--StartFragment-->", "<!--EndFragment-->")
CLIPBOARD_FRAGMENT_RE = re.compile(
    r"<!--\s*(?:StartFragment|EndFragment)\b",
    re.IGNORECASE,
)


class PatchValidationError(ValueError):
    """Raised when an agent-proposed patch is not safe for the current note."""


@dataclass(frozen=True)
class FieldSnapshot:
    name: str
    html: str


@dataclass(frozen=True)
class NoteImageSnapshot:
    attachment_index: int
    filename: str
    fields: tuple[str, ...]
    path: str

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "attachment_index": self.attachment_index,
            "filename": self.filename,
            "fields": list(self.fields),
        }


@dataclass(frozen=True)
class SelectedTextSnapshot:
    field_name: str
    field_index: int
    input_kind: str
    text: str
    html: str | None = None

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "field_index": self.field_index,
            "input_kind": self.input_kind,
            "text": self.text,
            "html": self.html,
        }


@dataclass(frozen=True)
class EditorSnapshot:
    mode: str
    note_id: int | None
    notetype_id: int
    notetype_name: str
    fields: tuple[FieldSnapshot, ...]
    tags: tuple[str, ...]
    current_field: str | None = None
    card_id: int | None = None
    images: tuple[NoteImageSnapshot, ...] = ()
    selected_text: SelectedTextSnapshot | None = None

    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    def field_html(self, name: str) -> str:
        for field_snapshot in self.fields:
            if field_snapshot.name == name:
                return field_snapshot.html
        raise KeyError(name)

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "note_id": self.note_id,
            "notetype_id": self.notetype_id,
            "notetype_name": self.notetype_name,
            "fields": [
                {"name": field_snapshot.name, "html": field_snapshot.html}
                for field_snapshot in self.fields
            ],
            "tags": list(self.tags),
            "current_field": self.current_field,
            "card_id": self.card_id,
            "images": [
                image_snapshot.as_tool_result() for image_snapshot in self.images
            ],
            "selected_text": (
                self.selected_text.as_tool_result() if self.selected_text else None
            ),
        }

    def image_paths(self) -> tuple[str, ...]:
        return tuple(image_snapshot.path for image_snapshot in self.images)


@dataclass(frozen=True)
class SelectedCardSnapshot:
    card_id: int
    note_id: int
    notetype_id: int
    notetype_name: str
    ord: int
    template_name: str
    deck_id: int | None = None
    deck_name: str | None = None

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "note_id": self.note_id,
            "notetype_id": self.notetype_id,
            "notetype_name": self.notetype_name,
            "ord": self.ord,
            "template_name": self.template_name,
            "deck_id": self.deck_id,
            "deck_name": self.deck_name,
        }


@dataclass(frozen=True)
class SelectedNoteSnapshot:
    note_id: int
    notetype_id: int
    notetype_name: str
    fields: tuple[FieldSnapshot, ...]
    tags: tuple[str, ...]

    def field_names(self) -> tuple[str, ...]:
        return tuple(field.name for field in self.fields)

    def field_html(self, name: str) -> str:
        for field_snapshot in self.fields:
            if field_snapshot.name == name:
                return field_snapshot.html
        raise KeyError(name)

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "note_id": self.note_id,
            "notetype_id": self.notetype_id,
            "notetype_name": self.notetype_name,
            "fields": [
                {"name": field_snapshot.name, "html": field_snapshot.html}
                for field_snapshot in self.fields
            ],
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class MultiCardSnapshot:
    cards: tuple[SelectedCardSnapshot, ...]
    notes: tuple[SelectedNoteSnapshot, ...]
    mode: str = "browse_multi"

    def note_by_id(self, note_id: int) -> SelectedNoteSnapshot:
        for note in self.notes:
            if note.note_id == note_id:
                return note
        raise KeyError(note_id)

    def card_by_id(self, card_id: int) -> SelectedCardSnapshot:
        for card in self.cards:
            if card.card_id == card_id:
                return card
        raise KeyError(card_id)

    def cards_for_note(self, note_id: int) -> tuple[SelectedCardSnapshot, ...]:
        return tuple(card for card in self.cards if card.note_id == note_id)

    def as_tool_result(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "cards": [card.as_tool_result() for card in self.cards],
            "notes": [note.as_tool_result() for note in self.notes],
        }

    def image_paths(self) -> tuple[str, ...]:
        return ()


def validate_selected_text_snapshot(
    raw: Any,
    fields: tuple[FieldSnapshot, ...],
) -> SelectedTextSnapshot | None:
    if isinstance(raw, SelectedTextSnapshot):
        raw = raw.as_tool_result()
    if not isinstance(raw, dict):
        return None

    field_index = raw.get("field_index")
    if type(field_index) is not int or field_index < 0 or field_index >= len(fields):
        return None

    field_name = raw.get("field_name")
    if not isinstance(field_name, str) or field_name != fields[field_index].name:
        return None

    input_kind = raw.get("input_kind")
    if input_kind not in ("rich_text", "plain_text"):
        return None

    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        return None

    selected_html = raw.get("html")
    if selected_html is not None and not isinstance(selected_html, str):
        return None

    return SelectedTextSnapshot(
        field_name=field_name,
        field_index=field_index,
        input_kind=input_kind,
        text=text,
        html=selected_html,
    )


@dataclass(frozen=True)
class TagPatch:
    replace: tuple[str, ...] | None = None
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()

    def apply(self, current_tags: tuple[str, ...]) -> tuple[str, ...]:
        tags = list(self.replace if self.replace is not None else current_tags)
        for tag in self.remove:
            tags = [existing for existing in tags if existing != tag]
        for tag in self.add:
            if tag not in tags:
                tags.append(tag)
        return tuple(tags)

    def has_changes(self) -> bool:
        return self.replace is not None or bool(self.add) or bool(self.remove)


@dataclass(frozen=True)
class NotePatch:
    summary: str
    note_id: int | None
    notetype_id: int
    field_updates: dict[str, str] = field(default_factory=dict)
    tag_patch: TagPatch = field(default_factory=TagPatch)

    def has_changes(self) -> bool:
        return bool(self.field_updates) or self.tag_patch.has_changes()


@dataclass(frozen=True)
class MultiNoteUpdate:
    note_id: int
    notetype_id: int
    field_updates: dict[str, str] = field(default_factory=dict)
    tag_patch: TagPatch = field(default_factory=TagPatch)

    def has_changes(self) -> bool:
        return bool(self.field_updates) or self.tag_patch.has_changes()


@dataclass(frozen=True)
class MultiNotePatch:
    summary: str
    note_updates: tuple[MultiNoteUpdate, ...]

    def has_changes(self) -> bool:
        return any(update.has_changes() for update in self.note_updates)

    def update_for_note(self, note_id: int) -> MultiNoteUpdate | None:
        for update in self.note_updates:
            if update.note_id == note_id:
                return update
        return None

    def affected_note_ids(self) -> tuple[int, ...]:
        return tuple(update.note_id for update in self.note_updates)


def _normalize_tags(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PatchValidationError(f"{key} must be a list of tags.")
    normalized: list[str] = []
    for raw_tag in value:
        if not isinstance(raw_tag, str):
            raise PatchValidationError(f"{key} must contain strings.")
        tag = raw_tag.strip()
        if not tag:
            continue
        if not TAG_RE.match(tag):
            raise PatchValidationError(f"Tag contains whitespace: {tag!r}.")
        if tag not in normalized:
            normalized.append(tag)
    return tuple(normalized)


def validate_note_patch(raw: dict[str, Any], snapshot: EditorSnapshot) -> NotePatch:
    if not isinstance(raw, dict):
        raise PatchValidationError("Patch must be a JSON object.")

    raw_note_id = raw.get("note_id")
    note_id = int(raw_note_id) if raw_note_id not in (None, "") else None
    if snapshot.note_id not in (None, 0) and note_id not in (None, snapshot.note_id):
        raise PatchValidationError("Patch targets a different note.")

    raw_notetype_id = raw.get("notetype_id", snapshot.notetype_id)
    try:
        notetype_id = int(raw_notetype_id)
    except (TypeError, ValueError) as exc:
        raise PatchValidationError("Patch notetype_id must be an integer.") from exc
    if notetype_id != snapshot.notetype_id:
        raise PatchValidationError("Patch targets a different note type.")

    field_updates = _validate_field_updates(
        raw.get("field_updates", []),
        set(snapshot.field_names()),
    )

    raw_tags = raw.get("tags", {})
    if raw_tags is None:
        raw_tags = {}
    if not isinstance(raw_tags, dict):
        raise PatchValidationError("tags must be an object.")

    raw_replace = raw_tags.get("replace")
    replace = (
        None if raw_replace is None else _normalize_tags(raw_replace, "tags.replace")
    )
    tag_patch = TagPatch(
        replace=replace,
        add=_normalize_tags(raw_tags.get("add"), "tags.add"),
        remove=_normalize_tags(raw_tags.get("remove"), "tags.remove"),
    )

    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise PatchValidationError("summary must be a string.")
    patch = NotePatch(
        summary=summary.strip() or "Proposed note update",
        note_id=note_id,
        notetype_id=notetype_id,
        field_updates=field_updates,
        tag_patch=tag_patch,
    )
    if not patch.has_changes():
        raise PatchValidationError("Patch does not contain any changes.")
    return patch


def _validate_field_updates(
    raw_updates: Any,
    known_fields: set[str],
) -> dict[str, str]:
    if raw_updates is None:
        raw_updates = []
    if not isinstance(raw_updates, list):
        raise PatchValidationError("field_updates must be a list.")
    field_updates: dict[str, str] = {}
    for update in raw_updates:
        if not isinstance(update, dict):
            raise PatchValidationError("Each field update must be an object.")
        name = update.get("name")
        html = update.get("html")
        if not isinstance(name, str) or not name:
            raise PatchValidationError("Field update name must be a string.")
        if name not in known_fields:
            raise PatchValidationError(f"Unknown field: {name}.")
        if name in field_updates:
            raise PatchValidationError(f"Duplicate field update: {name}.")
        if not isinstance(html, str):
            raise PatchValidationError(f"Field update for {name} must contain html.")
        field_updates[name] = _normalize_field_html(name, html)
    return field_updates


def _normalize_field_html(field_name: str, value: str) -> str:
    normalized = value
    for marker in CLIPBOARD_FRAGMENT_MARKERS:
        normalized = normalized.replace(marker, "")

    if CLIPBOARD_FRAGMENT_RE.search(normalized):
        raise PatchValidationError(
            f"Field update for {field_name} contains a malformed clipboard fragment comment."
        )
    if DOCUMENT_HTML_RE.search(normalized):
        raise PatchValidationError(
            f"Field update for {field_name} contains document wrapper HTML."
        )
    _validate_html_comments(field_name, normalized)
    return normalized


def _validate_html_comments(field_name: str, value: str) -> None:
    position = 0
    while True:
        start = value.find("<!--", position)
        end = value.find("-->", position)
        if end != -1 and (start == -1 or end < start):
            raise PatchValidationError(
                f"Field update for {field_name} contains a stray HTML comment closer."
            )
        if start == -1:
            return
        end = value.find("-->", start + 4)
        if end == -1:
            raise PatchValidationError(
                f"Field update for {field_name} contains an unterminated HTML comment."
            )
        position = end + 3


def _validate_tag_patch(raw_tags: Any) -> TagPatch:
    if raw_tags is None:
        raw_tags = {}
    if not isinstance(raw_tags, dict):
        raise PatchValidationError("tags must be an object.")

    raw_replace = raw_tags.get("replace")
    replace = (
        None if raw_replace is None else _normalize_tags(raw_replace, "tags.replace")
    )
    return TagPatch(
        replace=replace,
        add=_normalize_tags(raw_tags.get("add"), "tags.add"),
        remove=_normalize_tags(raw_tags.get("remove"), "tags.remove"),
    )


def validate_multi_note_patch(
    raw: dict[str, Any],
    snapshot: MultiCardSnapshot,
) -> MultiNotePatch:
    if not isinstance(raw, dict):
        raise PatchValidationError("Patch must be a JSON object.")

    raw_updates = raw.get("note_updates")
    if not isinstance(raw_updates, list):
        raise PatchValidationError("note_updates must be a list.")

    selected_notes = {note.note_id: note for note in snapshot.notes}
    updates: list[MultiNoteUpdate] = []
    seen_note_ids: set[int] = set()
    for raw_update in raw_updates:
        if not isinstance(raw_update, dict):
            raise PatchValidationError("Each note update must be an object.")
        raw_note_id = raw_update.get("note_id")
        try:
            note_id = int(raw_note_id)
        except (TypeError, ValueError) as exc:
            raise PatchValidationError("note_id must be an integer.") from exc
        if note_id in seen_note_ids:
            raise PatchValidationError(f"Duplicate note update: {note_id}.")
        if note_id not in selected_notes:
            raise PatchValidationError(
                f"Patch targets note that was not selected: {note_id}."
            )
        seen_note_ids.add(note_id)

        selected_note = selected_notes[note_id]
        raw_notetype_id = raw_update.get("notetype_id", selected_note.notetype_id)
        try:
            notetype_id = int(raw_notetype_id)
        except (TypeError, ValueError) as exc:
            raise PatchValidationError("notetype_id must be an integer.") from exc
        if notetype_id != selected_note.notetype_id:
            raise PatchValidationError("Patch targets a different note type.")

        update = MultiNoteUpdate(
            note_id=note_id,
            notetype_id=notetype_id,
            field_updates=_validate_field_updates(
                raw_update.get("field_updates", []),
                set(selected_note.field_names()),
            ),
            tag_patch=_validate_tag_patch(raw_update.get("tags", {})),
        )
        if update.has_changes():
            updates.append(update)

    summary = raw.get("summary", "")
    if not isinstance(summary, str):
        raise PatchValidationError("summary must be a string.")
    patch = MultiNotePatch(
        summary=summary.strip() or "Proposed selected-card updates",
        note_updates=tuple(updates),
    )
    if not patch.has_changes():
        raise PatchValidationError("Patch does not contain any changes.")
    return patch


def render_patch_diff(snapshot: EditorSnapshot, patch: NotePatch) -> str:
    lines = [patch.summary, ""]
    for field_name, new_html in patch.field_updates.items():
        old_html = snapshot.field_html(field_name)
        lines.append(f"Field: {field_name}")
        lines.extend(
            difflib.unified_diff(
                old_html.splitlines(),
                new_html.splitlines(),
                fromfile="current",
                tofile="proposed",
                lineterm="",
            )
        )
        lines.append("")

    if patch.tag_patch.has_changes():
        old_tags = tuple(snapshot.tags)
        new_tags = patch.tag_patch.apply(old_tags)
        lines.append("Tags")
        lines.extend(
            difflib.unified_diff(
                [" ".join(old_tags)],
                [" ".join(new_tags)],
                fromfile="current",
                tofile="proposed",
                lineterm="",
            )
        )
        lines.append("")

    return "\n".join(lines).strip()
