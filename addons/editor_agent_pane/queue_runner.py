# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Background worker that runs queued agent threads.

No Qt: everything that touches the collection or the GUI is injected, so the
add-on wires in ``taskman.run_on_main`` and tests pass synchronous stand-ins.
Each run either resumes the thread's CLI session with only the new turn, or
replays the stored transcript (see agent-request-queue-plan.md, "Replies").
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from .activity import ClaudeActivityRenderer, CodexActivityRenderer
from .agent_log import AgentRunLogger
from .card_access import CardReadScope, normalize_card_access, send_with_card_reads
from .codex_client import (
    AgentResult,
    AgentStopped,
    normalize_project_folder_access,
)
from .effort_options import (
    claude_effort_options,
    codex_effort_options,
    effort_supported_by_options,
    effort_value,
)
from .model_options import (
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    PROVIDER_OLLAMA,
    provider_value,
)
from .patches import (
    CreateDefaults,
    FieldSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    NotePatch,
    NotetypeInfo,
    PatchValidationError,
    validate_patch,
)
from .queue_store import (
    ANSWERED,
    FAILED,
    QUEUED,
    READY,
    RUNNING,
    QueueStore,
    QueueTransitionError,
    Thread,
)
from .snapshots import Patch, Snapshot, patch_to_dict, snapshot_from_dict

DEFAULT_TIMEOUT_SECONDS = 300
PATH_KEYS = ("codex_path", "claude_path", "ollama_path", "ollama_host")
CANCELLED = "cancelled"
OLLAMA_ACTIVITY_SUMMARY = "[Ollama activity: local model run]"
IDLE_POLL_SECONDS = 1.0

RunOnMain = Callable[[Callable[[], Any]], Any]
ReadCards = Callable[[CardReadScope, Any], dict[str, Any]]
# note_id -> {"mod": int, "fields": [html, ...], "tags": [str, ...]}, or None
# when the note no longer exists. Raises CollectionUnavailable (as ReadCards
# may) when the collection is closed; the run is then re-queued.
ReadNoteState = Callable[[list[int]], dict[int, dict[str, Any] | None]]
# {"notetypes": [{"id", "name", "fields": [str]}], "decks": [{"id", "name"}]}
# for the whole collection; raises CollectionUnavailable like ReadNoteState.
ReadCatalog = Callable[[], dict[str, Any]]
MakeAgent = Callable[[dict[str, Any]], Any]
Notify = Callable[[str], None]
LoggerFactory = Callable[[Thread], tuple[AgentRunLogger | None, str | None]]
# (thread_id, activity line), called on the worker thread while a run streams.
StreamEvent = Callable[[str, str], None]
CARD_READ_ACTIVITY_LINE = "[cards] Reading permitted cards"
NO_PENDING_REQUEST = (
    "Nothing to run: the thread has no new request since the agent's last answer."
)


class CollectionUnavailable(RuntimeError):
    """The collection is closed for now (sync, profile switch); retry later."""


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """Clean a thread's settings snapshot, as runtime.py cleans the config."""
    provider = provider_value(raw.get("provider"))
    model = str(raw.get("model") or "").strip()
    effort = effort_value(raw.get("reasoning_effort") or "")
    if provider == PROVIDER_OLLAMA:
        effort = ""
    else:
        options = (
            claude_effort_options(model)
            if provider == PROVIDER_CLAUDE
            else codex_effort_options(model)
        )
        effort = effort_supported_by_options(effort, options)
    deck_ids: list[int] = []
    for deck_id in raw.get("deck_ids") or ():
        try:
            deck_ids.append(int(deck_id))
        except (TypeError, ValueError):
            continue
    timeout = raw.get("timeout_seconds")
    try:
        timeout_seconds = int(timeout)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    if timeout_seconds < 1:
        timeout_seconds = DEFAULT_TIMEOUT_SECONDS
    raw_paths = raw.get("paths") if isinstance(raw.get("paths"), dict) else {}
    assert isinstance(raw_paths, dict)
    is_codex = provider == PROVIDER_CODEX
    return {
        "provider": provider,
        "model": model,
        "reasoning_effort": effort,
        "card_access": normalize_card_access(raw.get("card_access")),
        "deck_ids": deck_ids,
        "project_folder": str(raw.get("project_folder") or "").strip(),
        "project_folder_access": normalize_project_folder_access(
            str(raw.get("project_folder_access") or "")
        ),
        "custom_instructions": str(raw.get("custom_instructions") or ""),
        "fast_mode": is_codex and _bool(raw.get("fast_mode"), False),
        "stream_reasoning_summaries": is_codex
        and _bool(raw.get("stream_reasoning_summaries"), True),
        "timeout_seconds": timeout_seconds,
        "paths": {
            key: str(raw_paths.get(key) or "").strip()
            for key in PATH_KEYS
            if key in raw_paths
        },
    }


def default_make_agent(settings: dict[str, Any]) -> Any:
    """Build the provider client for normalised ``settings``; the runner adds
    ``persist_session`` (keep CLI sessions on disk so they can be resumed)."""
    paths = settings.get("paths", {})
    provider = settings["provider"]
    if provider == PROVIDER_OLLAMA:
        from .ollama_client import DEFAULT_OLLAMA_HOST, OllamaCliAgent

        return OllamaCliAgent(
            ollama_path=paths.get("ollama_path", ""),
            ollama_host=paths.get("ollama_host") or DEFAULT_OLLAMA_HOST,
            model=settings["model"],
            timeout_seconds=settings["timeout_seconds"],
            custom_instructions=settings["custom_instructions"],
        )
    if provider == PROVIDER_CLAUDE:
        from .claude_client import ClaudeCliAgent

        return ClaudeCliAgent(
            claude_path=paths.get("claude_path", ""),
            model=settings["model"],
            timeout_seconds=settings["timeout_seconds"],
            project_folder_access=settings["project_folder_access"],
            custom_instructions=settings["custom_instructions"],
            reasoning_effort=settings["reasoning_effort"],
            persist_session=bool(settings.get("persist_session")),
        )
    from .codex_client import CodexCliAgent

    return CodexCliAgent(
        codex_path=paths.get("codex_path", ""),
        model=settings["model"],
        timeout_seconds=settings["timeout_seconds"],
        project_folder_access=settings["project_folder_access"],
        custom_instructions=settings["custom_instructions"],
        fast_mode=settings["fast_mode"],
        reasoning_effort=settings["reasoning_effort"],
        stream_reasoning_summaries=settings["stream_reasoning_summaries"],
        persist_session=bool(settings.get("persist_session")),
    )


def note_state_hash(fields: list[str], tags: list[str]) -> str:
    payload = json.dumps(
        {"fields": list(fields), "tags": list(tags)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def snapshot_note_ids(snapshot: Snapshot) -> list[int]:
    if isinstance(snapshot, MultiCardSnapshot):
        return [note.note_id for note in snapshot.notes]
    return [snapshot.note_id] if snapshot.note_id else []


def patch_note_ids(patch: Patch, snapshot: Snapshot) -> list[int]:
    """The existing notes a patch changes (new notes have no base)."""
    if isinstance(patch, MultiNotePatch):
        return list(patch.affected_note_ids())
    if not patch.has_note_update():
        return []
    note_id = patch.note_id or getattr(snapshot, "note_id", None)
    return [note_id] if note_id else []


def creation_defaults(
    context: dict[str, Any], snapshot: Snapshot
) -> tuple[int | None, int | None]:
    """(notetype id, deck id) a new note defaults to: the source note's."""
    deck_id = context.get("deck_id")
    if isinstance(snapshot, MultiCardSnapshot):
        first = snapshot.cards[0] if snapshot.cards else None
        notetype_id = first.notetype_id if first is not None else None
        if deck_id is None and first is not None:
            deck_id = first.deck_id
    else:
        notetype_id = snapshot.notetype_id
    return notetype_id, int(deck_id) if isinstance(deck_id, int) else None


DEFAULT_CATALOG_LIMIT = 30
# Anki's own "Default" deck, listed beside the source deck's subtree.
ANKI_DEFAULT_DECK_ID = 1


def _defaults_first(
    entries: list[dict[str, Any]], default_id: int | None
) -> list[dict[str, Any]]:
    marked = [
        {**entry, "default": True} if entry.get("id") == default_id else dict(entry)
        for entry in entries
    ]
    return sorted(marked, key=lambda entry: not entry.get("default"))


def _deck_subtree(
    decks: list[dict[str, Any]], deck_id: int | None
) -> list[dict[str, Any]]:
    """The deck and its subdecks (by "::" name), the deck first."""
    root = next((deck for deck in decks if deck.get("id") == deck_id), None)
    if root is None:
        return []
    prefix = str(root.get("name", "")) + "::"
    children = sorted(
        (deck for deck in decks if str(deck.get("name", "")).startswith(prefix)),
        key=lambda deck: str(deck.get("name", "")),
    )
    return [root, *children]


def trim_catalog(
    catalog: dict[str, Any],
    *,
    notetype_id: int | None,
    deck_id: int | None,
    limit: int = DEFAULT_CATALOG_LIMIT,
) -> dict[str, Any]:
    """What the prompt lists: the source deck's subtree plus Anki's Default
    deck, and the note types used in those decks (most used first) plus the
    source note type; defaults first and marked, each list capped at
    ``limit``. Without a source deck or usage counts, every deck / note type
    up to the cap. Validation still uses the whole catalog."""
    limit = max(1, int(limit))
    decks = list(catalog.get("decks") or ())
    notetypes = list(catalog.get("notetypes") or ())
    subtree = _deck_subtree(decks, deck_id)
    if subtree:
        listed_decks = list(subtree)
        anki_default = next(
            (deck for deck in decks if deck.get("id") == ANKI_DEFAULT_DECK_ID), None
        )
        if anki_default is not None and anki_default not in listed_decks:
            listed_decks.append(anki_default)
    else:
        listed_decks = decks
    usage = catalog.get("usage")
    if subtree and isinstance(usage, list):
        subtree_ids = {deck.get("id") for deck in subtree}
        counts: dict[int, int] = {}
        for row in usage:
            try:
                row_deck, row_notetype, notes = (int(value) for value in row)
            except (TypeError, ValueError):
                continue
            if row_deck in subtree_ids:
                counts[row_notetype] = counts.get(row_notetype, 0) + notes
        listed_notetypes = sorted(
            (
                entry
                for entry in notetypes
                if entry.get("id") in counts or entry.get("id") == notetype_id
            ),
            key=lambda entry: (
                entry.get("id") != notetype_id,
                -counts.get(int(entry.get("id") or 0), 0),
                str(entry.get("name", "")),
            ),
        )
    else:
        listed_notetypes = notetypes
    shown_decks = _defaults_first(listed_decks, deck_id)[:limit]
    shown_notetypes = _defaults_first(listed_notetypes, notetype_id)[:limit]
    return {
        "notetypes": shown_notetypes,
        "decks": shown_decks,
        "omitted_notetypes": len(notetypes) - len(shown_notetypes),
        "omitted_decks": len(decks) - len(shown_decks),
    }


def catalog_prompt_section(
    catalog: dict[str, Any],
    *,
    notetype_id: int | None,
    deck_id: int | None,
    limit: int = DEFAULT_CATALOG_LIMIT,
) -> str:
    trimmed = trim_catalog(
        catalog, notetype_id=notetype_id, deck_id=deck_id, limit=limit
    )
    data = {"notetypes": trimmed["notetypes"], "decks": trimmed["decks"]}
    omitted = ""
    if trimmed["omitted_decks"] or trimmed["omitted_notetypes"]:
        omitted = (
            "; only those nearest the source note are listed "
            f"({trimmed['omitted_decks']} decks and "
            f"{trimmed['omitted_notetypes']} note types omitted), and any other "
            "existing deck or note type id is accepted too"
        )
    return (
        "Available note types and decks as JSON (use these ids in note_creates; "
        "entries marked default are the source note's note type and deck, used "
        f"unless the request implies otherwise{omitted}):\n"
        + json.dumps(data, ensure_ascii=False)
    )


def catalog_maps(
    catalog: dict[str, Any],
) -> tuple[dict[int, NotetypeInfo], set[int]]:
    notetypes = {
        int(entry["id"]): NotetypeInfo(
            name=str(entry.get("name", "")),
            field_names=tuple(str(name) for name in entry.get("fields") or ()),
        )
        for entry in catalog.get("notetypes") or ()
    }
    deck_ids = {int(entry["id"]) for entry in catalog.get("decks") or ()}
    return notetypes, deck_ids


def validate_against_catalog(
    patch: Patch,
    snapshot: Snapshot,
    catalog: dict[str, Any],
    defaults: CreateDefaults | None = None,
) -> Patch:
    """Re-validate a client-validated patch with the collection's note types
    and decks (the clients check structure only). A new note without a
    notetype_id/deck_id takes ``defaults`` (see creation_defaults)."""
    notetypes, deck_ids = catalog_maps(catalog)
    try:
        return validate_patch(
            patch_to_dict(patch),
            snapshot,
            notetypes=notetypes,
            deck_ids=deck_ids,
            defaults=defaults,
        )
    except PatchValidationError as exc:
        raise RuntimeError(f"The agent's proposal was rejected: {exc}") from exc


def _refreshed_fields(
    fields: tuple[FieldSnapshot, ...], state: dict[str, Any]
) -> tuple[FieldSnapshot, ...]:
    current = list(state.get("fields") or ())
    if len(current) != len(fields):
        # The notetype changed shape; keep the captured fields.
        return fields
    return tuple(
        FieldSnapshot(field.name, str(html)) for field, html in zip(fields, current)
    )


def refresh_snapshot(
    snapshot: Snapshot, states: dict[int, dict[str, Any] | None]
) -> Snapshot:
    """Replace captured fields/tags with the notes' current content."""
    if isinstance(snapshot, MultiCardSnapshot):
        notes = []
        for note in snapshot.notes:
            state = states.get(note.note_id)
            if state is None:
                notes.append(note)
                continue
            notes.append(
                replace(
                    note,
                    fields=_refreshed_fields(note.fields, state),
                    tags=tuple(state.get("tags") or ()),
                )
            )
        return replace(snapshot, notes=tuple(notes))
    state = states.get(snapshot.note_id) if snapshot.note_id else None
    if state is None:
        return snapshot
    return replace(
        snapshot,
        fields=_refreshed_fields(snapshot.fields, state),
        tags=tuple(state.get("tags") or ()),
    )


NOTE_DELETED = "The note no longer exists."
NOTES_DELETED = "The selected notes no longer exist."


def drop_deleted_notes(
    snapshot: Snapshot, states: dict[int, dict[str, Any] | None]
) -> tuple[Snapshot, list[int]]:
    """The snapshot without notes deleted since it was captured, and their ids.

    A note counts as deleted when ``states`` maps it to None. Raises when
    nothing is left to run against (the thread's note, or every selected note).
    """
    deleted = [
        note_id
        for note_id in snapshot_note_ids(snapshot)
        if note_id in states and states[note_id] is None
    ]
    if not deleted:
        return snapshot, []
    if not isinstance(snapshot, MultiCardSnapshot):
        raise RuntimeError(NOTE_DELETED)
    gone = set(deleted)
    notes = tuple(note for note in snapshot.notes if note.note_id not in gone)
    if not notes:
        raise RuntimeError(NOTES_DELETED)
    cards = tuple(card for card in snapshot.cards if card.note_id not in gone)
    return replace(snapshot, notes=notes, cards=cards), deleted


def dropped_notes_message(note_ids: list[int]) -> str:
    ids = ", ".join(str(note_id) for note_id in note_ids)
    return f"Left out notes that no longer exist: nid {ids}."


def base_for(
    note_ids: list[int], states: dict[int, dict[str, Any] | None]
) -> dict[str, Any]:
    notes: list[dict[str, Any]] = []
    for note_id in note_ids:
        state = states.get(note_id)
        if state is None:
            notes.append({"note_id": note_id, "mod": None, "hash": None})
            continue
        notes.append(
            {
                "note_id": note_id,
                "mod": state.get("mod"),
                "hash": note_state_hash(
                    list(state.get("fields") or ()), list(state.get("tags") or ())
                ),
            }
        )
    return {"notes": notes}


def replay_history(
    messages: list[Any],
) -> tuple[list[tuple[str, str]], str]:
    """Split a transcript into (user, agent) history pairs and the pending prompt.

    User messages that never got an answer (a failed run followed by a reply)
    are joined into one turn.
    """
    history: list[tuple[str, str]] = []
    pending: list[str] = []
    for message in messages:
        if message.role == "user":
            pending.append(message.text)
        elif message.role == "agent":
            history.append(("\n\n".join(pending), message.text))
            pending = []
    return history, "\n\n".join(pending)


def pending_request(messages: list[Any]) -> tuple[list[tuple[str, str]], str]:
    """``replay_history``, plus re-sending the last request on a retry/re-run.

    A thread queued again without a new user turn (``QueueStore.retry`` or
    ``rerun`` after its request was answered) ends with a system message (the
    failure, or RERUN_MESSAGE); it re-sends the last user turn. A thread whose
    last message is the agent's answer has nothing to run.
    """
    history, prompt = replay_history(messages)
    if prompt.strip() or not messages or messages[-1].role != "system":
        return history, prompt
    for message in reversed(messages):
        if message.role == "user":
            return history, message.text
    return history, ""


def note_mods(states: dict[int, dict[str, Any] | None]) -> dict[str, int | None]:
    """The notes' ``mod`` at run time, as stored in session_json."""
    return {
        str(note_id): (state.get("mod") if state is not None else None)
        for note_id, state in states.items()
    }


def notes_changed_since(
    session: dict[str, Any], states: dict[int, dict[str, Any] | None]
) -> bool:
    """Has any note changed since the session's last run saw it?"""
    seen = session.get("note_mods")
    if not isinstance(seen, dict):
        return bool(states)
    return any(seen.get(key, -1) != mod for key, mod in note_mods(states).items())


def base_changed(
    base: dict[str, Any], states: dict[int, dict[str, Any] | None]
) -> bool:
    """Does a proposal's base no longer match the notes (see stale_reason)?"""
    current = {
        entry["note_id"]: entry
        for entry in base_for(
            [
                entry.get("note_id")
                for entry in base.get("notes") or ()
                if entry.get("note_id") in states
            ],
            states,
        )["notes"]
    }
    for entry in base.get("notes") or ():
        now = current.get(entry.get("note_id"))
        if now is None:
            continue
        if now["mod"] is None or (now["mod"], now["hash"]) != (
            entry.get("mod"),
            entry.get("hash"),
        ):
            return True
    return False


def note_state_prompt_section(snapshot: Snapshot) -> str:
    return (
        "Current note state as JSON (the note changed since your last turn; "
        "this replaces what you saw before):\n"
        + json.dumps(snapshot.as_tool_result(), ensure_ascii=False)
    )


def stale_prompt_section(
    version: int, patch: dict[str, Any], snapshot: Snapshot
) -> str:
    return (
        f"The note changed since your proposal (version {version}): it was "
        "edited after you proposed, so the proposal no longer applies cleanly. "
        "Re-propose against the current note content below: return the complete "
        "patch for the current content (a null patch if nothing is needed any "
        "more).\nCurrent note content as JSON:\n"
        + json.dumps(snapshot.as_tool_result(), ensure_ascii=False)
        + "\nYour previous proposal as JSON:\n"
        + json.dumps(patch, ensure_ascii=False)
    )


def session_for(
    settings: dict[str, Any],
    session_id: str | None,
    states: dict[int, dict[str, Any] | None],
) -> dict[str, Any] | None:
    if not session_id:
        return None
    return {
        "provider": settings["provider"],
        "model": settings["model"],
        "session_id": session_id,
        "note_mods": note_mods(states),
    }


def resumable_session_id(
    session: dict[str, Any] | None, settings: dict[str, Any]
) -> str | None:
    """The session to resume, if it was made by the thread's current provider
    and model (Ollama has none)."""
    if not session or settings["provider"] == PROVIDER_OLLAMA:
        return None
    if session.get("provider") != settings["provider"]:
        return None
    if str(session.get("model") or "") != settings["model"]:
        return None
    session_id = session.get("session_id")
    return str(session_id) if session_id else None


RESUMED_MESSAGE = "[Resumed session {session_id}]"
REPLAYED_MESSAGE = "[Replayed transcript (resume failed: {error})]"
RESUME_FAILED_ACTIVITY_LINE = "[resume] Session resume failed; replaying the transcript"
RESUME_ERROR_CHARS = 200


def _one_line(text: str, limit: int = RESUME_ERROR_CHARS) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def proposal_prompt_section(version: int, patch: dict[str, Any]) -> str:
    return (
        f"Current proposal (version {version}) as JSON. The user has not applied "
        "it. Return the complete amended patch (repeat it unchanged to keep it "
        "as is). A null patch drops the proposal:\n"
        + json.dumps(patch, ensure_ascii=False)
    )


def reviewer_prompt_section(reviewer: dict[str, Any]) -> str:
    return (
        "Reviewer context as JSON: what the user saw in the reviewer when making "
        "this request (the side shown, the rendered question and answer HTML, and "
        "the text they had selected). Use it to resolve references such as "
        '"the sentence in blue" or "the example". It is rendered card output, '
        "not field content: do not copy it into fields; edit the fields in the "
        "editor context instead:\n" + json.dumps(reviewer, ensure_ascii=False)
    )


def check_patch_matches_snapshot(patch: Patch, snapshot: Snapshot) -> None:
    if isinstance(snapshot, MultiCardSnapshot):
        if not isinstance(patch, MultiNotePatch):
            raise RuntimeError(
                "The agent returned a single-note patch for a multi-card selection."
            )
    elif not isinstance(patch, NotePatch):
        raise RuntimeError("The agent returned a multi-note patch for a single note.")


def summary_with_elapsed(summary: str, elapsed_seconds: float) -> str:
    """Same format as runtime.py's ``_activity_summary_with_elapsed``."""
    seconds = max(0.0, elapsed_seconds)
    if seconds < 60:
        elapsed = f"{seconds:.1f}s"
    else:
        minutes, rest = divmod(int(round(seconds)), 60)
        elapsed = f"{minutes}m {rest:02d}s"
    clean = summary.strip()
    if clean.startswith("[") and " activity: " in clean and clean.endswith("]"):
        return f"{clean[:-1]}, took {elapsed}]"
    return f"{clean} (took {elapsed})"


class _ShuttingDown(Exception):
    pass


class QueueRunner:
    def __init__(
        self,
        store: QueueStore,
        *,
        run_on_main: RunOnMain,
        read_cards: ReadCards,
        read_note_state: ReadNoteState,
        make_agent: MakeAgent = default_make_agent,
        notify: Notify = lambda _thread_id: None,
        logger_factory: LoggerFactory | None = None,
        concurrency: int = 1,
        on_stream_event: StreamEvent | None = None,
        read_catalog: ReadCatalog | None = None,
        resume_sessions: bool | Callable[[], bool] = False,
        catalog_limit: int = DEFAULT_CATALOG_LIMIT,
    ) -> None:
        self.store = store
        self._run_on_main = run_on_main
        self._read_cards = read_cards
        self._read_note_state = read_note_state
        self._make_agent = make_agent
        self._notify = notify
        self._logger_factory = logger_factory
        self._concurrency = max(1, int(concurrency))
        self._on_stream_event = on_stream_event
        self._read_catalog = read_catalog
        self._resume_sessions = resume_sessions
        self._catalog_limit = catalog_limit
        self._lock = threading.Lock()
        self._stops: dict[str, threading.Event] = {}
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._workers: list[threading.Thread] = []
        # Set when the last run_once put its thread back in the queue, so the
        # worker waits before claiming it again.
        self._requeued = False

    # lifecycle

    def start(self) -> None:
        if self._workers:
            return
        self._stopping.clear()
        interrupted = [t.id for t in self.store.list_threads((RUNNING,))]
        self.store.reset_interrupted()
        for thread_id in interrupted:
            self._safe_notify(thread_id)
        for index in range(self._concurrency):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"agent-queue-{index}",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop the workers; interrupted runs go back to queued."""
        self.request_stop()
        self.join(timeout)

    def request_stop(self) -> None:
        """Tell running threads to stop; they will be re-queued, not failed.

        Call this before anything that makes a run raise ``AgentStopped`` on
        shutdown, so the run is recognised as interrupted rather than cancelled.
        """
        self._stopping.set()
        with self._lock:
            for event in self._stops.values():
                event.set()
        self._wake.set()

    def join(self, timeout: float = 10.0) -> None:
        for worker in self._workers:
            worker.join(timeout)
        self._workers = []

    def enqueue_kick(self) -> None:
        self._wake.set()

    def cancel(self, thread_id: str) -> bool:
        """Stop a running thread, or fail a queued one. True if anything changed."""
        with self._lock:
            event = self._stops.get(thread_id)
            if event is not None:
                event.set()
                return True
            try:
                self.store.transition(thread_id, QUEUED, FAILED)
            except QueueTransitionError:
                return False
        self.store.set_error(thread_id, CANCELLED)
        self._safe_notify(thread_id)
        return True

    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            self._wake.clear()
            try:
                ran = self.run_once()
            except Exception:
                ran = None
            if ran is None or self._requeued:
                self._wake.wait(IDLE_POLL_SECONDS)

    # one run

    def run_once(self) -> str | None:
        """Run the next queued thread to completion. Returns its id, or None."""
        with self._lock:
            if self._stopping.is_set():
                return None
            thread = self.store.claim_next()
            if thread is None:
                return None
            stop_event = threading.Event()
            self._stops[thread.id] = stop_event
        self._requeued = False
        self._safe_notify(thread.id)
        try:
            self._run(thread, stop_event)
        except (_ShuttingDown, CollectionUnavailable):
            self._requeue(thread.id)
        except AgentStopped:
            if self._stopping.is_set():
                # Interrupted by shutdown (e.g. while waiting on the main thread).
                self._requeue(thread.id)
            else:
                self._fail(thread.id, "Cancelled.", CANCELLED)
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self._fail(thread.id, message, message)
        finally:
            with self._lock:
                self._stops.pop(thread.id, None)
        self._safe_notify(thread.id)
        return thread.id

    def _resume_enabled(self) -> bool:
        option = self._resume_sessions
        try:
            return bool(option() if callable(option) else option)
        except Exception:
            return False

    def _run(self, thread: Thread, stop_event: threading.Event) -> None:
        settings = normalize_settings(thread.settings)
        snapshot = snapshot_from_dict(thread.context["snapshot"])
        history, prompt = pending_request(self.store.messages(thread.id))
        if not prompt.strip():
            raise RuntimeError(NO_PENDING_REQUEST)
        note_ids = snapshot_note_ids(snapshot)
        states: dict[int, dict[str, Any] | None] = (
            self._run_on_main(lambda: self._read_note_state(note_ids))
            if note_ids
            else {}
        )
        snapshot, dropped = drop_deleted_notes(snapshot, states)
        snapshot = refresh_snapshot(snapshot, states)
        read_catalog = self._read_catalog
        catalog: dict[str, Any] | None = (
            self._run_on_main(read_catalog) if read_catalog is not None else None
        )

        current = self.store.current_proposal(thread.id)
        stale = current is not None and base_changed(current.base, states)
        stale_section = (
            stale_prompt_section(current.version, current.patch, snapshot)
            if current is not None and stale
            else None
        )

        # Replay: the whole context, rebuilt from the store.
        replay_prompt = prompt
        reviewer = thread.context.get("reviewer")
        if isinstance(reviewer, dict) and reviewer:
            replay_prompt += "\n\n" + reviewer_prompt_section(reviewer)
        if catalog is not None:
            notetype_id, deck_id = creation_defaults(thread.context, snapshot)
            replay_prompt += "\n\n" + catalog_prompt_section(
                catalog,
                notetype_id=notetype_id,
                deck_id=deck_id,
                limit=self._catalog_limit,
            )
        if stale_section is not None:
            replay_prompt += "\n\n" + stale_section
        elif current is not None:
            replay_prompt += "\n\n" + proposal_prompt_section(
                current.version, current.patch
            )

        # Resume: the CLI session already holds the context, the reviewer
        # context (immutable), the catalog and its own proposal; send only
        # the new turn and what changed in the collection since.
        resume_enabled = self._resume_enabled()
        resume_id = (
            resumable_session_id(thread.session, settings) if resume_enabled else None
        )
        delta_prompt = prompt
        if stale_section is not None:
            delta_prompt += "\n\n" + stale_section
        elif thread.session is not None and notes_changed_since(thread.session, states):
            delta_prompt += "\n\n" + note_state_prompt_section(snapshot)

        logger, log_path = (
            self._logger_factory(thread) if self._logger_factory else (None, None)
        )
        if log_path:
            self.store.set_run_log_path(thread.id, log_path)

        provider = settings["provider"]

        def new_activity() -> ClaudeActivityRenderer | CodexActivityRenderer | None:
            if provider == PROVIDER_CLAUDE:
                return ClaudeActivityRenderer()
            if provider == PROVIDER_CODEX:
                return CodexActivityRenderer(
                    stream_reasoning_summaries=settings["stream_reasoning_summaries"]
                )
            return None

        activities = [new_activity()]
        scope = CardReadScope(settings["card_access"], tuple(settings["deck_ids"]))

        def stream(line: str) -> None:
            if self._on_stream_event is not None:
                on_stream_event = self._on_stream_event
                self._safe(lambda: on_stream_event(thread.id, line))

        def record(event: dict[str, Any]) -> None:
            activity = activities[-1]
            assert activity is not None
            line = activity.record(event)
            if line is not None:
                stream(line)

        def read(request: Any) -> dict[str, Any]:
            if stop_event.is_set():
                raise AgentStopped("Agent run stopped.")
            stream(CARD_READ_ACTIVITY_LINE)
            return self._run_on_main(lambda: self._read_cards(scope, request))

        agent = self._make_agent({**settings, "persist_session": resume_enabled})

        def send(text: str, turns: list[tuple[str, str]], **extra: Any) -> AgentResult:
            return send_with_card_reads(
                agent.send,
                prompt=text,
                scope=scope,
                read=read,
                stop_requested=stop_event.is_set,
                snapshot=snapshot,
                project_root=(
                    "" if provider == PROVIDER_OLLAMA else settings["project_folder"]
                ),
                history=turns,
                event_callback=record if activities[-1] is not None else None,
                run_logger=logger,
                **extra,
            )

        path_message: str | None = None
        started = time.monotonic()
        try:
            if resume_id is not None:
                try:
                    result = send(delta_prompt, [], resume_session_id=resume_id)
                    path_message = RESUMED_MESSAGE.format(session_id=resume_id)
                except (AgentStopped, CollectionUnavailable):
                    raise
                except Exception as exc:
                    # Any failure before a result: replay in the same run.
                    path_message = REPLAYED_MESSAGE.format(
                        error=_one_line(str(exc) or type(exc).__name__)
                    )
                    stream(RESUME_FAILED_ACTIVITY_LINE)
                    activities.append(new_activity())
                    result = send(replay_prompt, history)
            else:
                result = send(replay_prompt, history)
        except AgentStopped:
            if self._stopping.is_set():
                raise _ShuttingDown() from None
            raise
        elapsed = time.monotonic() - started

        activity = activities[-1]
        summary = summary_with_elapsed(
            activity.compact_summary()
            if activity is not None
            else OLLAMA_ACTIVITY_SUMMARY,
            elapsed,
        )

        patch: Patch | None = result.proposals[-1] if result.proposals else None
        if patch is not None:
            check_patch_matches_snapshot(patch, snapshot)
            if catalog is not None:
                notetype_id, deck_id = creation_defaults(thread.context, snapshot)
                patch = validate_against_catalog(
                    patch,
                    snapshot,
                    catalog,
                    CreateDefaults(notetype_id=notetype_id, deck_id=deck_id),
                )

        # One transaction: if it fails (e.g. the store closed during shutdown)
        # the thread stays running with its user turn pending, and is re-run.
        # A run without a patch drops the proposal it was shown (see
        # proposal_prompt_section). The session is replaced by this run's (or
        # cleared when the provider reported none).
        session = session_for(settings, result.session_id, states)
        self.store.complete_run(
            thread.id,
            to_status=READY if patch is not None else ANSWERED,
            messages=[
                *([("system", dropped_notes_message(dropped), "")] if dropped else []),
                *([("system", path_message, "")] if path_message else []),
                ("system", summary, ""),
                ("agent", result.text, result.html),
            ],
            proposal=patch_to_dict(patch) if patch is not None else None,
            base=(
                base_for(patch_note_ids(patch, snapshot), states)
                if patch is not None
                else None
            ),
            drop_proposal=patch is None,
            session=session,
            clear_session=session is None,
        )

    def _requeue(self, thread_id: str) -> None:
        self._requeued = True
        self._safe(lambda: self.store.complete_run(thread_id, to_status=QUEUED))

    def _fail(self, thread_id: str, message: str, error: str) -> None:
        """Fail the run; a proposal from an earlier turn stays current."""
        self._safe(
            lambda: self.store.complete_run(
                thread_id,
                to_status=FAILED,
                messages=[("system", message, "")],
                error=error,
                # The failed turn may have reached the CLI session; the next
                # run replays from the store instead of resuming it.
                clear_session=True,
            )
        )

    def _safe(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            pass

    def _safe_notify(self, thread_id: str) -> None:
        self._safe(lambda: self._notify(thread_id))
