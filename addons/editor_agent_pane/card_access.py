# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .codex_client import AgentResult

CARD_ACCESS_OPTIONS = (
    ("Current card only", "current"),
    ("Other cards in deck", "deck"),
    ("All other cards", "all"),
)
MAX_CARD_PAGE_SIZE = 20
MAX_CARD_REQUESTS = 12


def normalize_card_access(value: Any) -> str:
    return value if value in ("current", "deck", "all") else "current"


@dataclass(frozen=True)
class CardReadScope:
    access: str
    deck_ids: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "access": normalize_card_access(self.access),
            "deck_ids": list(self.deck_ids),
            "read_only": True,
        }


def read_cards(col: Any, scope: CardReadScope, request: Any) -> dict[str, Any]:
    """Called by Anki, never the model. Expose no collection mutation operation."""
    access = normalize_card_access(scope.access)
    if access == "current":
        return {"error": "Only the current editor context may be read."}
    if access == "deck" and not scope.deck_ids:
        return {"error": "No current deck is available. Select a card or target deck."}
    if not isinstance(request, dict) or set(request) != {"query", "offset", "limit"}:
        return {"error": "Use query, offset, and limit only."}
    query, offset, limit = (request[key] for key in ("query", "offset", "limit"))
    if (
        not isinstance(query, str)
        or len(query) > 2000
        or type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= MAX_CARD_PAGE_SIZE
    ):
        return {"error": "Expected an Anki query, offset >= 0, and limit from 1 to 20."}

    ids = set(col.find_cards(query))
    if access == "deck":
        # Intersect independently of the model's query so OR/negation cannot
        # escape the scope. odid preserves the home deck for filtered cards,
        # matching Card.current_deck_id(). Do not include subdecks implicitly.
        placeholders = ",".join("?" for _ in scope.deck_ids)
        allowed = col.db.list(
            f"select id from cards where (case when odid != 0 then odid else did end) "
            f"in ({placeholders})",
            *scope.deck_ids,
        )
        ids.intersection_update(allowed)
    ordered = sorted(ids)
    cards = []
    for card_id in ordered[offset : offset + limit]:
        card = col.get_card(card_id)
        note = card.note()
        notetype = card.note_type()
        deck_id = int(card.current_deck_id())
        cards.append(
            {
                "card_id": int(card.id),
                "note_id": int(note.id),
                "deck_id": deck_id,
                "deck_name": col.decks.name(deck_id),
                "notetype_id": int(note.mid),
                "notetype_name": str(notetype.get("name", "")),
                "ord": int(card.ord),
                "template_name": str(card.template().get("name", "")),
                "fields": [{"name": name, "html": html} for name, html in note.items()],
                "tags": list(note.tags),
            }
        )
    next_offset = offset + len(cards)
    return {
        "cards": cards,
        "total": len(ordered),
        "next_offset": next_offset if next_offset < len(ordered) else None,
        "read_only": True,
    }


def send_with_card_reads(
    send: Callable[..., AgentResult],
    *,
    prompt: str,
    scope: CardReadScope,
    read: Callable[[Any], dict[str, Any]],
    stop_requested: Callable[[], bool],
    **kwargs: Any,
) -> AgentResult:
    from .codex_client import AgentStopped

    results: list[dict[str, Any]] = []
    for round_index in range(MAX_CARD_REQUESTS + 1):
        if stop_requested():
            raise AgentStopped("Agent run stopped.")
        context = {
            **scope.as_dict(),
            "requests_remaining": MAX_CARD_REQUESTS - round_index,
            "results": results,
        }
        result = send(
            prompt=prompt
            + "\n\nAnki read-only card access:\n"
            + json.dumps(context, ensure_ascii=False),
            stop_requested=stop_requested,
            **kwargs,
        )
        if stop_requested():
            raise AgentStopped("Agent run stopped.")
        if result.card_request is None:
            return result
        if round_index == MAX_CARD_REQUESTS:
            raise RuntimeError(
                "Agent reached the card lookup limit. Try a narrower search."
            )
        if result.proposals:
            raise RuntimeError("A card lookup cannot also propose edits.")
        # Enforce current-only even if a provider ignores the prompt.
        response = (
            {"error": "Only the current editor context may be read."}
            if normalize_card_access(scope.access) == "current"
            else read(result.card_request)
        )
        results.append({"request": result.card_request, "response": response})
    raise AssertionError("unreachable")
