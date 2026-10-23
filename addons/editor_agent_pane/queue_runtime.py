# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Owns the per-profile agent queue: one store and one background runner.

Opened when a profile loads and closed before it unloads, so runs are owned
by the add-on rather than by any editor window. The worker reaches the
collection and the GUI only through ``run_on_main``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aqt
from anki.errors import NotFoundError
from aqt import gui_hooks
from aqt.utils import showWarning, tooltip

from .agent_log import AgentRunLogger, JsonLineAgentRunLogger, ensure_agent_log_folder
from .card_access import CardReadScope, read_cards
from .codex_client import AgentStopped
from .queue_runner import (
    CANCELLED,
    DEFAULT_CATALOG_LIMIT,
    CollectionUnavailable,
    QueueRunner,
    default_make_agent,
)
from .queue_store import ANSWERED, FAILED, READY, QueueStore, Thread

QUEUE_FILENAME = "agent_queue.sqlite"
MAIN_THREAD_POLL_SECONDS = 0.1
CARD_LOOKUP_FAILED = "Card lookup failed. Check the Anki search query and retry."
NOTIFY_TEXT = {
    READY: "Agent: proposal ready",
    ANSWERED: "Agent: reply ready",
    FAILED: "Agent: request failed",
}

ConfigGetter = Callable[[], dict[str, Any]]
PanesGetter = Callable[[], Iterable[Any]]


@dataclass
class QueueService:
    store: QueueStore
    runner: QueueRunner


_service: QueueService | None = None
_closing = threading.Event()
_config: ConfigGetter = dict
_panes: PanesGetter = tuple
_addon = ""
_installed = False


def install(*, addon: str, config: ConfigGetter, panes: PanesGetter) -> None:
    global _installed, _addon, _config, _panes
    _addon, _config, _panes = addon, config, panes
    if _installed:
        return
    _installed = True
    gui_hooks.profile_did_open.append(_on_profile_did_open)
    gui_hooks.profile_will_close.append(close_service)


def _on_profile_did_open() -> None:
    try:
        open_service()
    except Exception as exc:
        showWarning(f"The agent queue could not be opened: {exc}")


def service() -> QueueService | None:
    return _service


def open_service() -> QueueService:
    global _service
    if _service is not None:
        return _service
    mw = aqt.mw
    assert mw is not None
    config = _config()
    _closing.clear()
    store = QueueStore.open(Path(mw.pm.profileFolder()) / QUEUE_FILENAME)
    store.sweep(_positive_float(config.get("queue_retention_days"), 30.0))
    runner = QueueRunner(
        store,
        run_on_main=run_on_main,
        read_cards=_read_cards,
        read_note_state=read_note_state,
        make_agent=default_make_agent,
        notify=_notify,
        logger_factory=_logger_factory,
        concurrency=int(_positive_float(config.get("queue_concurrency"), 1.0)),
        on_stream_event=_on_stream_event,
        read_catalog=read_catalog,
        resume_sessions=_resume_sessions,
        catalog_limit=int(
            _positive_float(config.get("queue_catalog_limit"), DEFAULT_CATALOG_LIMIT)
        ),
    )
    _service = QueueService(store=store, runner=runner)
    runner.start()
    return _service


def close_service() -> None:
    global _service
    current = _service
    if current is None:
        return
    _service = None
    # Mark runs as interrupted (re-queued) before unblocking a worker waiting
    # on the main thread; the other order records them as cancelled.
    current.runner.request_stop()
    _closing.set()
    current.runner.join()
    current.store.close()


def _resume_sessions() -> bool:
    """Read live, so toggling queue_resume_sessions applies to the next run."""
    value = _config().get("queue_resume_sessions", True)
    return value if isinstance(value, bool) else True


def _positive_float(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def run_on_main(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` on the main thread and block the calling worker for its result."""
    if threading.current_thread() is threading.main_thread():
        return fn()
    mw = aqt.mw
    if mw is None or _closing.is_set():
        raise AgentStopped("Anki is closing.")
    future: Future = Future()

    def call() -> None:
        if not future.set_running_or_notify_cancel():
            return
        try:
            future.set_result(fn())
        except BaseException as exc:
            future.set_exception(exc)

    mw.taskman.run_on_main(call)
    while True:
        if _closing.is_set():
            future.cancel()
            raise AgentStopped("Anki is closing.")
        try:
            return future.result(timeout=MAIN_THREAD_POLL_SECONDS)
        except FutureTimeout:
            continue


def _read_cards(scope: CardReadScope, request: Any) -> dict[str, Any]:
    mw = aqt.mw
    if mw is None or mw.col is None:
        raise CollectionUnavailable("The collection is closed.")
    try:
        return read_cards(mw.col, scope, request)
    except Exception:
        return {"error": CARD_LOOKUP_FAILED}


def read_note_state(note_ids: list[int]) -> dict[int, dict[str, Any] | None]:
    mw = aqt.mw
    if mw is None or mw.col is None:
        raise CollectionUnavailable("The collection is closed.")
    col: Any = mw.col
    states: dict[int, dict[str, Any] | None] = {}
    for note_id in note_ids:
        try:
            note = col.get_note(note_id)
        except NotFoundError:
            states[note_id] = None
            continue
        states[note_id] = {
            "mod": int(note.mod),
            "fields": list(note.fields),
            "tags": list(note.tags),
        }
    return states


# Notes per (home deck, note type), for trimming the catalog in the prompt.
_USAGE_SQL = (
    "select (case when c.odid != 0 then c.odid else c.did end) as deck,"
    " n.mid, count(distinct n.id) from cards c join notes n on n.id = c.nid"
    " group by deck, n.mid"
)


def read_catalog() -> dict[str, Any]:
    """Note types (with field names), normal decks and how many notes of each
    note type each deck holds, for new-note proposals."""
    mw = aqt.mw
    if mw is None or mw.col is None:
        raise CollectionUnavailable("The collection is closed.")
    col: Any = mw.col
    notetypes = []
    for entry in col.models.all_names_and_ids():
        notetype = col.models.get(entry.id)
        if not notetype:
            continue
        notetypes.append(
            {
                "id": int(entry.id),
                "name": str(entry.name),
                "fields": [str(field["name"]) for field in notetype["flds"]],
            }
        )
    decks = [
        {"id": int(deck.id), "name": str(deck.name)}
        for deck in col.decks.all_names_and_ids(include_filtered=False)
    ]
    usage = [
        [int(deck), int(mid), int(count)] for deck, mid, count in col.db.all(_USAGE_SQL)
    ]
    return {"notetypes": notetypes, "decks": decks, "usage": usage}


def _logger_factory(_thread: Thread) -> tuple[AgentRunLogger | None, str | None]:
    mw = aqt.mw
    if mw is None:
        return None, None
    logger = JsonLineAgentRunLogger(mw.addonManager.get_logger(_addon))
    try:
        path = ensure_agent_log_folder(mw.addonManager, _addon) / f"{_addon}.log"
    except Exception:
        return logger, None
    return logger, str(path)


def _notify(thread_id: str) -> None:
    mw = aqt.mw
    if mw is not None:
        mw.taskman.run_on_main(lambda: thread_changed(thread_id))


def thread_changed(thread_id: str, *, exclude: Any = None, notify: bool = True) -> None:
    """Main thread: fan a thread change out to open panes, and maybe tooltip
    (not with ``notify=False``, for changes that are not a run's outcome)."""
    for pane in list(_panes()):
        if pane is exclude:
            continue
        try:
            pane.on_thread_changed(thread_id)
        except Exception:
            pass
    current = _service
    if current is None or not notify or not _config().get("queue_notify", True):
        return
    thread = current.store.get_thread(thread_id)
    if thread is None or thread.status not in NOTIFY_TEXT:
        return
    if thread.status == FAILED and thread.error == CANCELLED:
        return
    tooltip(NOTIFY_TEXT[thread.status])


def _on_stream_event(thread_id: str, line: str) -> None:
    mw = aqt.mw
    if mw is not None:
        mw.taskman.run_on_main(lambda: thread_activity(thread_id, line))


def thread_activity(thread_id: str, line: str) -> None:
    for pane in list(_panes()):
        try:
            pane.on_thread_activity(thread_id, line)
        except Exception:
            pass
