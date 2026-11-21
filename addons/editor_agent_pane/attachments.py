# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Selections the user attaches to a message, each with an optional comment.

An attachment comes from a field of the editor's note or from the transcript.
It is stored with its message and given to the model as a numbered block
before the message's text, so history replay carries it too.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

FIELD = "field"
TRANSCRIPT = "transcript"
TRANSCRIPT_ROLES = ("assistant", "user")


@dataclass(frozen=True)
class Attachment:
    source: str
    text: str
    comment: str = ""
    # source "field": which field of the note
    field_name: str | None = None
    field_index: int | None = None
    # source "transcript": whose message, or None when it spans messages
    role: str | None = None

    @classmethod
    def field(
        cls, field_name: str, field_index: int, text: str, comment: str = ""
    ) -> Attachment:
        return cls(FIELD, text, comment, field_name=field_name, field_index=field_index)

    @classmethod
    def transcript(
        cls, text: str, role: str | None = None, comment: str = ""
    ) -> Attachment:
        return cls(TRANSCRIPT, text, comment, role=role)

    def with_comment(self, comment: str) -> Attachment:
        return Attachment(
            self.source,
            self.text,
            comment,
            self.field_name,
            self.field_index,
            self.role,
        )

    def same_selection(self, other: Attachment) -> bool:
        """The same text from the same place, whatever the comments."""
        return self.with_comment("") == other.with_comment("")

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"source": self.source, "text": self.text}
        if self.comment:
            data["comment"] = self.comment
        if self.source == FIELD:
            data["field_name"] = self.field_name
            data["field_index"] = self.field_index
        elif self.role is not None:
            data["role"] = self.role
        return data

    @classmethod
    def from_dict(cls, raw: Any) -> Attachment | None:
        """An attachment from stored or page data; None if it is malformed."""
        if not isinstance(raw, dict):
            return None
        text = raw.get("text")
        comment = raw.get("comment", "")
        if not isinstance(text, str) or not text.strip():
            return None
        if not isinstance(comment, str):
            return None
        source = raw.get("source")
        if source == FIELD:
            name = raw.get("field_name")
            index = raw.get("field_index")
            if not isinstance(name, str) or type(index) is not int or index < 0:
                return None
            return cls.field(name, index, text, comment)
        if source == TRANSCRIPT:
            role = raw.get("role")
            if role is not None and role not in TRANSCRIPT_ROLES:
                return None
            return cls.transcript(text, role, comment)
        return None


def attachments_from_dicts(raw: Any) -> tuple[Attachment, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(a for item in raw if (a := Attachment.from_dict(item)) is not None)


def attachments_to_dicts(attachments: Iterable[Attachment]) -> list[dict[str, Any]]:
    return [attachment.to_dict() for attachment in attachments]


def model_label(attachment: Attachment) -> str:
    """Where the selection is from, as the model is told."""
    if attachment.source == FIELD:
        return f'From the note\'s "{attachment.field_name}" field'
    if attachment.role == "assistant":
        return "From your earlier reply"
    if attachment.role == "user":
        return "From my earlier message"
    return "From our conversation"


def user_label(attachment: Attachment) -> str:
    """Where the selection is from, as the user is shown."""
    if attachment.source == FIELD:
        return str(attachment.field_name)
    if attachment.role == "assistant":
        return "Reply"
    if attachment.role == "user":
        return "Your message"
    return "Conversation"


def attachments_prompt(text: str, attachments: Iterable[Attachment]) -> str:
    """A user turn as the model gets it: the attachments, numbered, then the
    text."""
    items = list(attachments)
    if not items:
        return text
    blocks = []
    for number, attachment in enumerate(items, 1):
        lines = [f"[{number}] {model_label(attachment)}:"]
        lines += [f"> {line}".rstrip() for line in attachment.text.strip().split("\n")]
        if attachment.comment.strip():
            lines.append(f"Comment: {attachment.comment.strip()}")
        blocks.append("\n".join(lines))
    block = "Attached selections:\n" + "\n\n".join(blocks)
    return f"{block}\n\n{text}" if text.strip() else block


class AttachmentTray:
    """The selections waiting to go with the next message.

    ``ContextTray`` (thread_panel.py) shows one and has the same methods.
    """

    def __init__(self) -> None:
        self._items: list[Attachment] = []

    @property
    def items(self) -> tuple[Attachment, ...]:
        return tuple(self._items)

    def add(self, attachment: Attachment) -> int | None:
        """Add at the end; None (and no change) if it is already there."""
        if any(item.same_selection(attachment) for item in self._items):
            return None
        self._items.append(attachment)
        return len(self._items) - 1

    def remove(self, index: int) -> None:
        if 0 <= index < len(self._items):
            del self._items[index]

    def set_comment(self, index: int, comment: str) -> None:
        if 0 <= index < len(self._items):
            self._items[index] = self._items[index].with_comment(comment)

    def drop_fields(self) -> None:
        """Forget the editor's selections (the note changed)."""
        self._items = [item for item in self._items if item.source != FIELD]

    def take(self) -> tuple[Attachment, ...]:
        """The items, with comments trimmed, leaving the tray empty."""
        items = tuple(item.with_comment(item.comment.strip()) for item in self._items)
        self._items = []
        return items

    def restore(self, attachments: Iterable[Attachment]) -> None:
        """Put back items whose message was not sent, before any added since."""
        added = self._items
        self._items = []
        for item in (*attachments, *added):
            self.add(item)
