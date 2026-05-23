# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
