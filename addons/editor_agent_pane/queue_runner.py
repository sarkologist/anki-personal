# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Background worker that runs queued agent threads.

No Qt: everything that touches the collection or the GUI is injected, so the
add-on wires in ``taskman.run_on_main`` and tests pass synchronous stand-ins.
Phase 1 replays the stored transcript on every run (no CLI session resume).
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
    FieldSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    NotePatch,
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
    """Build the provider client for normalised ``settings``."""
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
    if isinstance(patch, MultiNotePatch):
        return list(patch.affected_note_ids())
    note_id = patch.note_id or getattr(snapshot, "note_id", None)
    return [note_id] if note_id else []


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
        '"the sentence in blue" or "the example":\n'
        + json.dumps(reviewer, ensure_ascii=False)
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

    def _run(self, thread: Thread, stop_event: threading.Event) -> None:
        settings = normalize_settings(thread.settings)
        snapshot = snapshot_from_dict(thread.context["snapshot"])
        history, prompt = replay_history(self.store.messages(thread.id))
        if not prompt.strip():
            raise RuntimeError(NO_PENDING_REQUEST)
        note_ids = snapshot_note_ids(snapshot)
        states: dict[int, dict[str, Any] | None] = (
            self._run_on_main(lambda: self._read_note_state(note_ids))
            if note_ids
            else {}
        )
        snapshot = refresh_snapshot(snapshot, states)

        current = self.store.current_proposal(thread.id)
        reviewer = thread.context.get("reviewer")
        if isinstance(reviewer, dict) and reviewer:
            prompt = prompt + "\n\n" + reviewer_prompt_section(reviewer)
        if current is not None:
            prompt = (
                prompt
                + "\n\n"
                + proposal_prompt_section(current.version, current.patch)
            )

        logger, log_path = (
            self._logger_factory(thread) if self._logger_factory else (None, None)
        )
        if log_path:
            self.store.set_run_log_path(thread.id, log_path)

        provider = settings["provider"]
        activity: ClaudeActivityRenderer | CodexActivityRenderer | None
        if provider == PROVIDER_CLAUDE:
            activity = ClaudeActivityRenderer()
        elif provider == PROVIDER_CODEX:
            activity = CodexActivityRenderer(
                stream_reasoning_summaries=settings["stream_reasoning_summaries"]
            )
        else:
            activity = None
        scope = CardReadScope(settings["card_access"], tuple(settings["deck_ids"]))

        def stream(line: str) -> None:
            if self._on_stream_event is not None:
                on_stream_event = self._on_stream_event
                self._safe(lambda: on_stream_event(thread.id, line))

        def record(event: dict[str, Any]) -> None:
            assert activity is not None
            line = activity.record(event)
            if line is not None:
                stream(line)

        def read(request: Any) -> dict[str, Any]:
            if stop_event.is_set():
                raise AgentStopped("Agent run stopped.")
            stream(CARD_READ_ACTIVITY_LINE)
            return self._run_on_main(lambda: self._read_cards(scope, request))

        agent = self._make_agent(settings)
        started = time.monotonic()
        try:
            result: AgentResult = send_with_card_reads(
                agent.send,
                prompt=prompt,
                scope=scope,
                read=read,
                stop_requested=stop_event.is_set,
                snapshot=snapshot,
                project_root=(
                    "" if provider == PROVIDER_OLLAMA else settings["project_folder"]
                ),
                history=history,
                event_callback=record if activity is not None else None,
                run_logger=logger,
            )
        except AgentStopped:
            if self._stopping.is_set():
                raise _ShuttingDown() from None
            raise
        elapsed = time.monotonic() - started

        summary = summary_with_elapsed(
            activity.compact_summary()
            if activity is not None
            else OLLAMA_ACTIVITY_SUMMARY,
            elapsed,
        )

        patch: Patch | None = result.proposals[-1] if result.proposals else None
        if patch is not None:
            check_patch_matches_snapshot(patch, snapshot)

        # One transaction: if it fails (e.g. the store closed during shutdown)
        # the thread stays running with its user turn pending, and is re-run.
        # A run without a patch drops the proposal it was shown (see
        # proposal_prompt_section).
        self.store.complete_run(
            thread.id,
            to_status=READY if patch is not None else ANSWERED,
            messages=[
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
            )
        )

    def _safe(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except Exception:
            pass

    def _safe_notify(self, thread_id: str) -> None:
        self._safe(lambda: self._notify(thread_id))
