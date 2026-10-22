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


# Patches use the agent response schema's shape (field_updates as a list of
# {name, html}, tags as {replace, add, remove}), so a stored proposal can be
# shown back to the agent verbatim when a reply amends it.


def patch_to_dict(patch: Patch) -> dict[str, Any]:
    if isinstance(patch, MultiNotePatch):
        return {
            "summary": patch.summary,
            "note_updates": [
                {
                    "note_id": update.note_id,
                    "notetype_id": update.notetype_id,
                    "field_updates": _field_updates_to_list(update.field_updates),
                    "tags": _tags_to_dict(update.tag_patch),
                }
                for update in patch.note_updates
            ],
        }
    return {
        "summary": patch.summary,
        "note_id": patch.note_id,
        "notetype_id": patch.notetype_id,
        "field_updates": _field_updates_to_list(patch.field_updates),
        "tags": _tags_to_dict(patch.tag_patch),
    }


def patch_from_dict(data: dict[str, Any]) -> Patch:
    if "note_updates" in data:
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
        )
    note_id = data.get("note_id")
    return NotePatch(
        summary=str(data["summary"]),
        note_id=int(note_id) if note_id is not None else None,
        notetype_id=int(data["notetype_id"]),
        field_updates=_field_updates_from_list(data["field_updates"]),
        tag_patch=_tags_from_dict(data["tags"]),
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
