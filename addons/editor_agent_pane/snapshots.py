# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""JSON round-tripping for editor snapshots and patches.

The queue store persists the captured context and each proposal as JSON. These
dicts are trusted (written by us), so they are rebuilt without re-validation;
patches are validated against a snapshot when the agent produces them.
"""

from __future__ import annotations

from typing import Any

from .patches import (
    EditorSnapshot,
    FieldSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    MultiNoteUpdate,
    NoteCreate,
    NoteImageSnapshot,
    NotePatch,
    SelectedCardSnapshot,
    SelectedNoteSnapshot,
    SelectedTextSnapshot,
    TagPatch,
)

Snapshot = EditorSnapshot | MultiCardSnapshot
Patch = NotePatch | MultiNotePatch

EDITOR_KIND = "editor"
MULTI_KIND = "multi"


def snapshot_to_dict(snapshot: Snapshot) -> dict[str, Any]:
    data = snapshot.as_tool_result()
    if isinstance(snapshot, MultiCardSnapshot):
        return {"kind": MULTI_KIND, **data}
    # as_tool_result() hides local image paths from the agent; keep them here.
    data["images"] = [
        {**image.as_tool_result(), "path": image.path} for image in snapshot.images
    ]
    return {"kind": EDITOR_KIND, **data}


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    kind = data.get("kind")
    if kind == EDITOR_KIND:
        return _editor_snapshot_from_dict(data)
    if kind == MULTI_KIND:
        return MultiCardSnapshot(
            cards=tuple(SelectedCardSnapshot(**card) for card in data["cards"]),
            notes=tuple(_selected_note_from_dict(note) for note in data["notes"]),
            mode=str(data.get("mode", "browse_multi")),
        )
    raise ValueError(f"Unknown snapshot kind: {kind!r}.")


def _fields(raw: list[dict[str, Any]]) -> tuple[FieldSnapshot, ...]:
    return tuple(FieldSnapshot(str(f["name"]), str(f["html"])) for f in raw)


def _editor_snapshot_from_dict(data: dict[str, Any]) -> EditorSnapshot:
    selected = data.get("selected_text")
    return EditorSnapshot(
        mode=str(data["mode"]),
        note_id=data.get("note_id"),
        notetype_id=int(data["notetype_id"]),
        notetype_name=str(data["notetype_name"]),
        fields=_fields(data["fields"]),
        tags=tuple(data.get("tags", ())),
        current_field=data.get("current_field"),
        card_id=data.get("card_id"),
        images=tuple(
            NoteImageSnapshot(
                attachment_index=int(image["attachment_index"]),
                filename=str(image["filename"]),
                fields=tuple(image.get("fields", ())),
                path=str(image["path"]),
            )
            for image in data.get("images", ())
        ),
        selected_text=SelectedTextSnapshot(**selected) if selected else None,
    )


def _selected_note_from_dict(data: dict[str, Any]) -> SelectedNoteSnapshot:
    return SelectedNoteSnapshot(
        note_id=int(data["note_id"]),
        notetype_id=int(data["notetype_id"]),
        notetype_name=str(data["notetype_name"]),
        fields=_fields(data["fields"]),
        tags=tuple(data.get("tags", ())),
    )


# Patches use the agent response schema's unified shape (note_updates with
# field_updates as a list of {name, html} and tags as {replace, add, remove},
# note_creates, assumptions, questions), so a stored proposal can be shown back
# to the agent verbatim when a reply amends it. Whether it is a single-note or
# a multi-note patch follows from the thread's snapshot.


def patch_to_dict(patch: Patch) -> dict[str, Any]:
    if isinstance(patch, MultiNotePatch):
        updates = [
            _update_to_dict(
                update.note_id,
                update.notetype_id,
                update.field_updates,
                update.tag_patch,
            )
            for update in patch.note_updates
        ]
    else:
        updates = (
            [
                _update_to_dict(
                    patch.note_id,
                    patch.notetype_id,
                    patch.field_updates,
                    patch.tag_patch,
                )
            ]
            if patch.has_note_update()
            else []
        )
    return {
        "summary": patch.summary,
        "note_updates": updates,
        "note_creates": [_create_to_dict(create) for create in patch.note_creates],
        "assumptions": list(patch.assumptions),
        "questions": list(patch.questions),
    }


def patch_from_dict(data: dict[str, Any], snapshot: Snapshot) -> Patch:
    """Rebuild a stored patch; ``snapshot`` is the thread's captured context."""
    creates = tuple(_create_from_dict(raw) for raw in data.get("note_creates") or ())
    assumptions = tuple(str(text) for text in data.get("assumptions") or ())
    questions = tuple(str(text) for text in data.get("questions") or ())
    if "note_updates" not in data:
        # Phase 1 stored single-note patches with the update at the top level.
        note_id = data.get("note_id")
        return NotePatch(
            summary=str(data["summary"]),
            note_id=int(note_id) if note_id is not None else None,
            notetype_id=int(data["notetype_id"]),
            field_updates=_field_updates_from_list(data.get("field_updates") or []),
            tag_patch=_tags_from_dict(data.get("tags") or {}),
            note_creates=creates,
            assumptions=assumptions,
            questions=questions,
        )
    if isinstance(snapshot, MultiCardSnapshot):
        return MultiNotePatch(
            summary=str(data["summary"]),
            note_updates=tuple(
                MultiNoteUpdate(
                    note_id=int(update["note_id"]),
                    notetype_id=int(update["notetype_id"]),
                    field_updates=_field_updates_from_list(update["field_updates"]),
                    tag_patch=_tags_from_dict(update["tags"]),
                )
                for update in data["note_updates"]
            ),
            note_creates=creates,
            assumptions=assumptions,
            questions=questions,
        )
    updates = data["note_updates"]
    if updates:
        update = updates[0]
        note_id = update.get("note_id")
        return NotePatch(
            summary=str(data["summary"]),
            note_id=int(note_id) if note_id is not None else None,
            notetype_id=int(update["notetype_id"]),
            field_updates=_field_updates_from_list(update["field_updates"]),
            tag_patch=_tags_from_dict(update["tags"]),
            note_creates=creates,
            assumptions=assumptions,
            questions=questions,
        )
    return NotePatch(
        summary=str(data["summary"]),
        note_id=snapshot.note_id or None,
        notetype_id=snapshot.notetype_id,
        note_creates=creates,
        assumptions=assumptions,
        questions=questions,
    )


def _update_to_dict(
    note_id: int | None,
    notetype_id: int,
    field_updates: dict[str, str],
    tag_patch: TagPatch,
) -> dict[str, Any]:
    return {
        "note_id": note_id,
        "notetype_id": notetype_id,
        "field_updates": _field_updates_to_list(field_updates),
        "tags": _tags_to_dict(tag_patch),
    }


def _create_to_dict(create: NoteCreate) -> dict[str, Any]:
    return {
        "notetype_id": create.notetype_id,
        "deck_id": create.deck_id,
        "fields": [{"name": f.name, "html": f.html} for f in create.fields],
        "tags": list(create.tags),
    }


def _create_from_dict(raw: dict[str, Any]) -> NoteCreate:
    return NoteCreate(
        notetype_id=int(raw["notetype_id"]),
        deck_id=None if raw.get("deck_id") is None else int(raw["deck_id"]),
        fields=_fields(raw["fields"]),
        tags=tuple(str(tag) for tag in raw.get("tags") or ()),
    )


def _field_updates_to_list(updates: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": name, "html": html} for name, html in updates.items()]


def _field_updates_from_list(raw: list[dict[str, Any]]) -> dict[str, str]:
    return {str(update["name"]): str(update["html"]) for update in raw}


def _tags_to_dict(tag_patch: TagPatch) -> dict[str, Any]:
    return {
        "replace": list(tag_patch.replace) if tag_patch.replace is not None else None,
        "add": list(tag_patch.add),
        "remove": list(tag_patch.remove),
    }


def _tags_from_dict(raw: dict[str, Any]) -> TagPatch:
    replace = raw.get("replace")
    return TagPatch(
        replace=tuple(replace) if replace is not None else None,
        add=tuple(raw.get("add", ())),
        remove=tuple(raw.get("remove", ())),
    )
