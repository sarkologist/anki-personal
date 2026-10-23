# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Pure helpers for showing a queue thread in the pane (no Qt, no collection).

The pane renders a thread from the store: its transcript, a banner while it is
queued or running, and its current proposal. These helpers turn store rows
into surface HTML fragments and decide which thread a note shows.
"""

from __future__ import annotations

import datetime
import html
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .effort_options import effort_options_for, effort_supported_by_options
from .model_options import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_OLLAMA,
    provider_value,
)
from .patches import (
    HTML_TAG_RE,
    EditorSnapshot,
    MultiCardSnapshot,
    SelectedTextSnapshot,
)
from .queue_runner import REPLAYED_MESSAGE, RESUMED_MESSAGE, note_state_hash
from .queue_store import (
    ANSWERED,
    APPLIED,
    DISCARDED,
    FAILED,
    QUEUED,
    READY,
    RERUN_MESSAGE,
    RUNNING,
    STALE,
    Message,
    Proposal,
    Thread,
)
from .sanitize import sanitize_html
from .snapshots import MULTI_KIND, Snapshot, snapshot_to_dict
from .surface import (
    render_activity_note,
    render_activity_start,
    render_activity_summary,
    render_assistant_message,
    render_error_message,
    render_notice_message,
    render_user_message,
)

ACTIVE_STATUSES = (QUEUED, RUNNING)
REPLYABLE_STATUSES = (READY, ANSWERED, FAILED)
# A failed run leaves the previous turn's proposal current, so it can still be
# applied. A stale proposal can be applied only when forced ("Apply anyway"):
# the base check still refuses it otherwise.
APPLICABLE_STATUSES = (READY, FAILED, STALE)
# Retry: a failed run is queued again without a new message.
RETRYABLE_STATUSES = (FAILED,)
# Re-run: the last request again, against the current note.
RERUNNABLE_STATUSES = (STALE, READY, FAILED)
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
    "applied. Re-run it to have the agent re-propose against the current "
    "note, apply it anyway, or discard it."
)
MISSING_NOTE_WARNING = (
    "The note this proposal was made for no longer exists. Discard the proposal."
)
# Why a proposal went stale (stored by mark_stale as a system message): shown
# as a notice, not as an error.
STALE_REASONS = (STALE_WARNING, MISSING_NOTE_WARNING)
STALE_NOTICE_LABEL = "Stale"
# System messages that record what a run did, shown as one activity line.
ACTIVITY_LINE_PREFIXES = (
    RERUN_MESSAGE,
    RESUMED_MESSAGE.split("{", 1)[0],
    REPLAYED_MESSAGE.split("{", 1)[0],
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
    for statuses in (ACTIVE_STATUSES, (READY, STALE, ANSWERED), (FAILED,)):
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
        elif message.text.strip() in STALE_REASONS:
            fragments.append(
                render_notice_message(STALE_NOTICE_LABEL, message.text.strip())
            )
        elif message.text.strip().startswith(ACTIVITY_LINE_PREFIXES):
            fragments.append(render_activity_note(message.text, role=role))
        elif _is_activity_summary(message.text):
            fragments.append(
                render_activity_summary(
                    f"agent-thread-activity-{message.seq}", message.text, role=role
                )
            )
        else:
            fragments.append(render_error_message(message.text))
    return fragments


PANE_CLOSED_HINT = "Send starts a new request."


def status_fragment(
    thread: Thread, *, closed_hint: str = PANE_CLOSED_HINT
) -> str | None:
    """A banner for a thread that is not waiting on the user. ``closed_hint``
    follows "Applied." / "Discarded." (the pane says how to start over)."""
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
            status=" ".join(
                part
                for part in (
                    f"[{STATUS_TEXT[thread.status].capitalize()}.",
                    closed_hint,
                )
                if part
            )
            + "]",
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


# The queue window's thread list.

QUEUE_STATUS_ORDER = (
    READY,
    STALE,
    ANSWERED,
    FAILED,
    RUNNING,
    QUEUED,
    APPLIED,
    DISCARDED,
)
CLOSED_STATUSES = (APPLIED, DISCARDED)
STATUS_GLYPHS = {
    READY: "●",
    ANSWERED: "✉",
    FAILED: "✗",
    RUNNING: "⟳",
    QUEUED: "…",
    STALE: "!",
    APPLIED: "✓",
    DISCARDED: "–",
}
EXCERPT_CHARS = 60


def thread_note_ids(thread: Thread) -> list[int]:
    """The existing notes a thread is about: its note, or the selected notes."""
    if thread.note_id is not None:
        return [int(thread.note_id)]
    snapshot = thread.context.get("snapshot") or {}
    if snapshot.get("kind") == MULTI_KIND:
        return [int(note["note_id"]) for note in snapshot.get("notes") or ()]
    return []


def queue_threads(
    threads: Iterable[Thread],
    *,
    include_closed: bool = False,
    note_id: int | None = None,
) -> list[Thread]:
    """The queue window's rows: waiting on the user first, then newest first."""
    rank = {status: index for index, status in enumerate(QUEUE_STATUS_ORDER)}
    shown = [
        thread
        for thread in threads
        if (include_closed or thread.status not in CLOSED_STATUSES)
        and (note_id is None or note_id in thread_note_ids(thread))
    ]
    return sorted(
        shown,
        key=lambda thread: (
            rank.get(thread.status, len(rank)),
            -thread.updated_at,
            -thread.created_at,
        ),
    )


def neighbour_id(thread_ids: Sequence[str], thread_id: str) -> str | None:
    """The row after ``thread_id`` (or before it, at the end of the list)."""
    if thread_id not in thread_ids:
        return None
    index = list(thread_ids).index(thread_id)
    if index + 1 < len(thread_ids):
        return thread_ids[index + 1]
    return thread_ids[index - 1] if index > 0 else None


def plain_excerpt(html_text: str, max_chars: int = EXCERPT_CHARS) -> str:
    text = " ".join(html.unescape(HTML_TAG_RE.sub(" ", html_text)).split())
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def card_excerpt(thread: Thread) -> str:
    """The first field of the note the request was made on, as captured."""
    snapshot = thread.context.get("snapshot") or {}
    if snapshot.get("kind") == MULTI_KIND:
        notes = list(snapshot.get("notes") or ())
        if not notes:
            return ""
        first = _first_field(notes[0])
        return first if len(notes) == 1 else f"{first} (+{len(notes) - 1} notes)"
    return _first_field(snapshot)


def _first_field(note: dict[str, Any]) -> str:
    fields = list(note.get("fields") or ())
    return plain_excerpt(str(fields[0].get("html", ""))) if fields else ""


def age_text(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return "now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return "now"


DEFERRED_GLYPH = "⏸"


def is_deferred(thread: Thread, now: float) -> bool:
    return thread.deferred_until is not None and thread.deferred_until > now


def until_text(until: float, now: float) -> str:
    """ "until 08:00" today, "until Thu 08:00" within a week, else a date."""
    when = datetime.datetime.fromtimestamp(until)
    today = datetime.datetime.fromtimestamp(now).date()
    if when.date() == today:
        return when.strftime("until %H:%M")
    if (when.date() - today).days < 7:
        return when.strftime("until %a %H:%M")
    return when.strftime("until %d %b %H:%M")


def queue_row(
    thread: Thread, *, deck: str, version: int | None, now: float
) -> tuple[str, str, str, str, str, str]:
    """Cells: status glyph, title, card excerpt, deck, age, proposal version.

    A deferred thread gets DEFERRED_GLYPH and the time it comes back as its age.
    """
    deferred = is_deferred(thread, now)
    glyph = STATUS_GLYPHS.get(thread.status, "?")
    return (
        glyph + DEFERRED_GLYPH if deferred else glyph,
        thread.title or "(untitled)",
        card_excerpt(thread),
        deck,
        (
            until_text(float(thread.deferred_until or 0), now)
            if deferred
            else age_text(now - thread.updated_at)
        ),
        f"v{version}" if version else "",
    )


DEFER_HOUR = "hour"
DEFER_TOMORROW = "tomorrow"
DEFER_NEXT_WEEK = "next_week"
DEFER_MORNING_HOUR = 8
DEFER_CHOICES = (
    ("For an hour", DEFER_HOUR),
    ("Until tomorrow 08:00", DEFER_TOMORROW),
    ("Until next week (Monday 08:00)", DEFER_NEXT_WEEK),
)


def defer_choice_time(choice: str, now: float) -> float:
    """When a Defer menu choice ends, in local time."""
    if choice == DEFER_HOUR:
        return now + 3600
    today = datetime.datetime.fromtimestamp(now).date()
    if choice == DEFER_TOMORROW:
        day = today + datetime.timedelta(days=1)
    elif choice == DEFER_NEXT_WEEK:
        day = today + datetime.timedelta(days=7 - today.weekday())
    else:
        raise ValueError(f"Unknown defer choice: {choice!r}.")
    return datetime.datetime.combine(day, datetime.time(DEFER_MORNING_HOUR)).timestamp()


def thread_deck_ids(thread: Thread) -> tuple[int, ...]:
    """Decks "Other cards in deck" reads for a thread: its captured default
    deck, or the selected cards' decks."""
    snapshot = thread.context.get("snapshot") or {}
    if snapshot.get("kind") == MULTI_KIND:
        return tuple(
            sorted(
                {
                    int(card["deck_id"])
                    for card in snapshot.get("cards") or ()
                    if card.get("deck_id") is not None
                }
            )
        )
    deck_id = thread.context.get("deck_id")
    return (int(deck_id),) if deck_id is not None else ()


def merge_thread_settings(
    settings: dict[str, Any],
    *,
    provider: str,
    model: str,
    reasoning_effort: str,
    card_access: str,
    deck_ids: Sequence[int] = (),
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """``settings`` with a new provider/model/effort/card access for the next
    run; paths, project folder, timeouts and the rest are kept. An effort the
    model does not take falls back to the default; "deck" access without
    stored decks takes ``deck_ids``. A provider change resets the
    provider-specific flags as the pane sets them: Codex's fast mode and
    reasoning summaries from ``config`` (the add-on config), off for the
    others, and no project folder for Ollama."""
    provider = provider_value(provider)
    model = str(model).strip()
    merged = dict(settings)
    if provider != provider_value(settings.get("provider")):
        config = config or {}
        is_codex = provider == PROVIDER_CODEX
        fast_mode = config.get("fast_mode", False)
        summaries = config.get("stream_reasoning_summaries", True)
        merged["fast_mode"] = is_codex and fast_mode is True
        merged["stream_reasoning_summaries"] = is_codex and summaries is not False
        if provider == PROVIDER_OLLAMA:
            merged["project_folder"] = ""
    merged.update(
        provider=provider,
        model=model,
        reasoning_effort=effort_supported_by_options(
            reasoning_effort, effort_options_for(provider, model)
        ),
        card_access=card_access,
    )
    if card_access == "deck" and not merged.get("deck_ids"):
        merged["deck_ids"] = list(deck_ids)
    return merged


SOURCE_LABELS = {
    SOURCE_EDIT_CURRENT: "Edit Current",
    SOURCE_ADD_CARDS: "Add Cards",
    SOURCE_BROWSER: "Browser",
    SOURCE_BROWSER_MULTI: "Browser, several cards",
}


def captured_context_fragment(thread: Thread) -> str:
    """What the request was captured with, collapsed above the transcript."""
    context = thread.context
    source = SOURCE_LABELS.get(str(context.get("source")), "unknown")
    status = STATUS_TEXT.get(thread.status, thread.status)
    rows = [("From", source)]
    if note_ids := thread_note_ids(thread):
        rows.append(
            ("Notes" if len(note_ids) > 1 else "Note", ", ".join(map(str, note_ids)))
        )
    if excerpt := card_excerpt(thread):
        rows.append(("Card", excerpt))
    reviewer = context.get("reviewer") or {}
    if side := reviewer.get("side"):
        rows.append(("Side shown", str(side)))
    if selection := str(reviewer.get("selection") or "").strip():
        rows.append(("Selected in reviewer", plain_excerpt(selection, 200)))
    snapshot = context.get("snapshot") or {}
    selected = snapshot.get("selected_text") or {}
    if text := str(selected.get("text") or "").strip():
        field = str(selected.get("field_name") or "")
        rows.append(
            (f"Selected in {field}" if field else "Selected", plain_excerpt(text, 200))
        )
    body = "".join(
        f"<div><b>{html.escape(label)}:</b> {html.escape(value)}</div>"
        for label, value in rows
    )
    summary = f"Captured context ({source}, {status})"
    return (
        '<section class="agent-activity agent-activity-compact">'
        '<details class="agent-activity-details">'
        f"<summary>{html.escape(summary)}</summary>"
        f'<div class="agent-body">{body}</div></details></section>'
    )
