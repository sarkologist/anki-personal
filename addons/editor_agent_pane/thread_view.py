# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pure helpers for showing a queue thread in the pane (no Qt, no collection).

The pane renders a thread from the store: its transcript, a banner while it is
queued or running, and its current proposal. These helpers turn store rows
into surface HTML fragments and decide which thread a note shows.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from .model_options import PROVIDER_CLAUDE, PROVIDER_CODEX, PROVIDER_OLLAMA
from .patches import EditorSnapshot, MultiCardSnapshot, SelectedTextSnapshot
from .queue_runner import note_state_hash
from .queue_store import (
    ANSWERED,
    APPLIED,
    DISCARDED,
    FAILED,
    QUEUED,
    READY,
    RUNNING,
    STALE,
    Message,
    Proposal,
    Thread,
)
from .sanitize import sanitize_html
from .snapshots import Snapshot, snapshot_to_dict
from .surface import (
    render_activity_start,
    render_activity_summary,
    render_assistant_message,
    render_error_message,
    render_user_message,
)

ACTIVE_STATUSES = (QUEUED, RUNNING)
REPLYABLE_STATUSES = (READY, ANSWERED, FAILED)
# A failed run leaves the previous turn's proposal current, so it can still be
# applied.
APPLICABLE_STATUSES = (READY, FAILED)
DISCARDABLE_STATUSES = (QUEUED, READY, ANSWERED, FAILED, STALE)
LIVE_ACTIVITY_ID = "agent-thread-live"
STATUS_ID = "agent-thread-status"

PROVIDER_LABELS = {
    PROVIDER_CODEX: "Codex",
    PROVIDER_OLLAMA: "Ollama",
    PROVIDER_CLAUDE: "Claude",
}
RUNNING_STATUS = {
    PROVIDER_CODEX: "[Live Codex activity]",
    PROVIDER_OLLAMA: "[Running local Ollama model]",
    PROVIDER_CLAUDE: "[Live Claude activity]",
}
STATUS_TEXT = {
    QUEUED: "queued",
    RUNNING: "running",
    READY: "proposal ready",
    ANSWERED: "answered",
    FAILED: "failed",
    APPLIED: "applied",
    DISCARDED: "discarded",
    STALE: "stale",
}

STALE_WARNING = (
    "The note changed since the agent made this proposal, so it was not "
    "applied. Discard it, or reply to have the agent re-propose against the "
    "current note."
)
MISSING_NOTE_WARNING = (
    "The note this proposal was made for no longer exists. Discard the proposal."
)

# context_json layout (see agent-request-queue-plan.md, "Captured context").
SOURCE_EDIT_CURRENT = "edit_current"
SOURCE_ADD_CARDS = "add_cards"
SOURCE_BROWSER = "browser"
SOURCE_BROWSER_MULTI = "browser_multi"


def thread_context(
    source: str,
    snapshot: Snapshot,
    *,
    reviewer: dict[str, Any] | None = None,
    note_mod: int | None = None,
    deck_id: int | None = None,
) -> dict[str, Any]:
    """``deck_id`` is the default deck for new notes: the source card's home
    deck, or the Add Cards target deck."""
    return {
        "source": source,
        "snapshot": snapshot_to_dict(snapshot),
        "note_mod": note_mod,
        "reviewer": reviewer,
        "deck_id": deck_id,
    }


REVIEWER_SELECTION_JS = "window.getSelection().toString()"
_STYLE_BLOCK_RE = re.compile(r"(?is)<style\b.*?</style\s*>")


def reviewer_card_html(render: Callable[[], str]) -> str:
    """A rendered reviewer side, sanitized and without its card CSS."""
    try:
        return sanitize_html(_STYLE_BLOCK_RE.sub("", str(render()))).strip()
    except Exception:
        return ""


def provider_label(thread: Thread) -> str:
    return PROVIDER_LABELS.get(str(thread.settings.get("provider")), "Codex")


def pick_thread(threads: Sequence[Thread]) -> Thread | None:
    """The thread a note should show: newest active, else newest with a result."""
    newest_first = sorted(threads, key=lambda t: t.created_at, reverse=True)
    for statuses in (ACTIVE_STATUSES, (READY, ANSWERED), (FAILED,)):
        for thread in newest_first:
            if thread.status in statuses:
                return thread
    return None


def thread_label(thread: Thread) -> str:
    title = thread.title or "(untitled)"
    return f"{title} - {STATUS_TEXT.get(thread.status, thread.status)}"


def transcript_fragments(
    thread: Thread,
    messages: Iterable[Message],
    selected_text: SelectedTextSnapshot | None = None,
) -> list[str]:
    """HTML fragments for the stored transcript, oldest first.

    ``selected_text`` is the editor selection captured with the thread; it is
    shown on the first user message, which is the one it was sent with.
    """
    role = provider_label(thread)
    fragments: list[str] = []
    first_user = True
    for message in messages:
        if message.role == "user":
            fragments.append(
                render_user_message(message.text, selected_text if first_user else None)
            )
            first_user = False
        elif message.role == "agent":
            if message.text or message.html:
                fragments.append(render_assistant_message(message.html, message.text))
        elif _is_activity_summary(message.text):
            fragments.append(
                render_activity_summary(
                    f"agent-thread-activity-{message.seq}", message.text, role=role
                )
            )
        else:
            fragments.append(render_error_message(message.text))
    return fragments


def status_fragment(thread: Thread) -> str | None:
    """A banner for a thread that is not waiting on the user."""
    role = provider_label(thread)
    if thread.status == QUEUED:
        return render_activity_start(
            LIVE_ACTIVITY_ID, role=role, status="[Queued - waiting for the agent]"
        )
    if thread.status == RUNNING:
        return render_activity_start(
            LIVE_ACTIVITY_ID,
            role=role,
            status=RUNNING_STATUS.get(
                str(thread.settings.get("provider")), "[Running]"
            ),
        )
    if thread.status in (APPLIED, DISCARDED):
        return render_activity_start(
            STATUS_ID,
            role="Thread",
            status=f"[{STATUS_TEXT[thread.status].capitalize()}. "
            "Send starts a new request.]",
        )
    return None


OPEN_NOTE_COMMAND = "agentOpenNote"


def created_note_ids(proposals: Iterable[Proposal]) -> list[int]:
    """Notes added by applying the thread's proposals, oldest first."""
    ids: list[int] = []
    for proposal in proposals:
        for note_id in (proposal.result or {}).get("created_note_ids") or ():
            ids.append(int(note_id))
    return ids


def created_notes_fragment(note_ids: Sequence[int]) -> str | None:
    """Transcript links to notes an apply created (opened in the Browser)."""
    if not note_ids:
        return None
    links = ", ".join(
        f'<a href="#" onclick="pycmd(\'{OPEN_NOTE_COMMAND}:{int(note_id)}\');'
        f'return false;">nid:{int(note_id)}</a>'
        for note_id in note_ids
    )
    label = "Created note" if len(note_ids) == 1 else "Created notes"
    return (
        '<section class="agent-message assistant">'
        f'<div class="agent-role">Applied</div><div class="agent-body">{label}: '
        f"{links}</div></section>"
    )


def parse_open_note_command(cmd: str) -> int | None:
    prefix = OPEN_NOTE_COMMAND + ":"
    if not cmd.startswith(prefix):
        return None
    try:
        return int(cmd[len(prefix) :])
    except ValueError:
        return None


def _is_activity_summary(text: str) -> bool:
    clean = text.strip()
    return clean.startswith("[") and clean.endswith("]")


def snapshot_card_ids(snapshot: Snapshot) -> tuple[int, ...]:
    if isinstance(snapshot, MultiCardSnapshot):
        return tuple(card.card_id for card in snapshot.cards)
    return ()


def selected_text_of(snapshot: Snapshot) -> SelectedTextSnapshot | None:
    return snapshot.selected_text if isinstance(snapshot, EditorSnapshot) else None


def stale_reason(
    base: dict[str, Any],
    states: dict[int, dict[str, Any] | None],
) -> str | None:
    """Why a proposal no longer applies cleanly, or None if its base still holds.

    ``states`` maps note id to the note's current {mod, fields, tags}, or None
    when the note is gone; notes missing from ``states`` are not checked.
    """
    for entry in base.get("notes") or ():
        note_id = entry.get("note_id")
        if note_id not in states:
            continue
        state = states[note_id]
        if state is None or entry.get("mod") is None:
            return MISSING_NOTE_WARNING
        current_hash = note_state_hash(
            list(state.get("fields") or ()), list(state.get("tags") or ())
        )
        if state.get("mod") != entry.get("mod") or current_hash != entry.get("hash"):
            return STALE_WARNING
    return None


def unsaved_note_stale_reason(
    snapshot: Snapshot,
    notetype_id: int,
    fields: list[str],
    tags: list[str],
) -> str | None:
    """For a note with no id yet (Add Cards): has the editor moved on from the
    captured snapshot? There is no mod to compare, so compare content."""
    if not isinstance(snapshot, EditorSnapshot):
        return None
    captured = note_state_hash(
        [field.html for field in snapshot.fields], list(snapshot.tags)
    )
    if notetype_id != snapshot.notetype_id or note_state_hash(fields, tags) != captured:
        return STALE_WARNING
    return None
