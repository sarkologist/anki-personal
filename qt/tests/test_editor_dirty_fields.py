# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aqt import editor as editor_module
from aqt.editor import Editor


@dataclass
class _Note:
    fields: list[str]


class _Editor:
    def __init__(self, fields: list[str], sticky: list[bool]) -> None:
        self.note = _Note(fields)
        self._sticky = sticky

    def note_type(self) -> dict[str, Any]:
        return {"flds": [{"sticky": sticky} for sticky in self._sticky]}


def test_fields_are_blank_ignores_sticky_fields() -> None:
    editor = _Editor(fields=["", "changed sticky"], sticky=[False, True])

    assert Editor.fieldsAreBlank(editor)


def test_fields_are_blank_detects_non_sticky_input() -> None:
    editor = _Editor(fields=["front", "changed sticky"], sticky=[False, True])

    assert not Editor.fieldsAreBlank(editor)


@dataclass
class _BatchNote:
    id: int
    fields: list[str]


class _BatchEditor:
    def __init__(self, note_id: int, fields: list[str], add_mode: bool = False) -> None:
        self.note = _BatchNote(note_id, fields)
        self.addMode = add_mode
        self.saved = 0
        self.checked = 0

    def mungeHTML(self, html: str) -> str:
        return f"munged:{html}"

    def _save_current_note(self) -> None:
        self.saved += 1

    def _check_and_update_duplicate_display_async(self) -> None:
        self.checked += 1


def test_batch_fields_are_saved_once(monkeypatch) -> None:
    editor = _BatchEditor(123, ["old front", "old back"])
    typed: list[_BatchNote] = []
    monkeypatch.setattr(
        editor_module.gui_hooks,
        "editor_did_fire_typing_timer",
        typed.append,
    )

    assert Editor._save_fields(editor, 123, ["new front", "new back"])
    assert editor.note.fields == ["munged:new front", "munged:new back"]
    assert editor.saved == 1
    assert editor.checked == 1
    assert typed == [editor.note]


def test_batch_fields_only_update_add_mode_memory(monkeypatch) -> None:
    editor = _BatchEditor(0, ["old"], add_mode=True)
    monkeypatch.setattr(
        editor_module.gui_hooks,
        "editor_did_fire_typing_timer",
        lambda note: None,
    )

    assert Editor._save_fields(editor, 0, ["new"])
    assert editor.note.fields == ["munged:new"]
    assert editor.saved == 0


def test_batch_fields_reject_stale_note_or_wrong_field_count(monkeypatch) -> None:
    editor = _BatchEditor(123, ["old front", "old back"])
    typed: list[_BatchNote] = []
    monkeypatch.setattr(
        editor_module.gui_hooks,
        "editor_did_fire_typing_timer",
        typed.append,
    )

    assert not Editor._save_fields(editor, 999, ["new front", "new back"])
    assert not Editor._save_fields(editor, 123, ["only one"])
    assert editor.note.fields == ["old front", "old back"]
    assert editor.saved == 0
    assert editor.checked == 0
    assert typed == []
