"""Cards automation cannot finish without the learner; see ATTENTION.md."""

from __future__ import annotations

import html
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .preparation import read_lesson

EDIT = "edit"
STUCK = "stuck"
REJECTED = "rejected"
STALLED = "stalled fix"
# A held repair rejected this many times in a row is shown to the learner.
REJECTIONS = 3
ACTIONS = {
    EDIT: "Dismiss",
    STUCK: "Release",
    REJECTED: "Release",
    STALLED: "Cancel fix",
}


@dataclass(frozen=True)
class Attention:
    kind: str
    card_id: int
    note_id: int
    word: str
    reason: str


def rejections(deferred: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """Consecutive rejections and the last reason of each deferred target."""
    return {
        key: (
            int(item.get("attempts", 0)),
            str(read_lesson(item.get("reason", "")).get("reason", "")),
        )
        for key, item in deferred.items()
        if isinstance(item, dict)
    }


def browser_search(items: Iterable[Attention]) -> str:
    return "cid:" + ",".join(str(item.card_id) for item in items)


def banner(language: str, count: int, command: str) -> str:
    cards = "card needs" if count == 1 else "cards need"
    return (
        '<div style="margin:1em auto;text-align:center">'
        f"{html.escape(language)} Frontier: {count} {cards} your attention. "
        f"<a href=# onclick=\"return pycmd('{command}')\">Review</a></div>"
    )
