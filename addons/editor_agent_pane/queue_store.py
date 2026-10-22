# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Persistent agent request threads (see agent-request-queue-plan.md).

Pure Python over sqlite3 so it can be used from the worker thread and from
tests. One connection, shared behind a lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
TITLE_CHARS = 60

QUEUED = "queued"
RUNNING = "running"
READY = "ready"
ANSWERED = "answered"
FAILED = "failed"
APPLIED = "applied"
DISCARDED = "discarded"
STALE = "stale"

TERMINAL_STATUSES = (APPLIED, DISCARDED)

# The plan's state machine, plus a few closing edges it leaves implicit:
# answered/failed threads can be discarded, a queued thread can be cancelled
# (-> failed) or discarded before it runs, and running -> queued is the
# startup/shutdown reset. A failed run keeps the previous proposal current, so
# failed -> applied applies it; applied -> ready/failed reverts an apply whose
# collection write failed.
TRANSITIONS: dict[str, frozenset[str]] = {
    QUEUED: frozenset({RUNNING, FAILED, DISCARDED}),
    RUNNING: frozenset({READY, ANSWERED, FAILED, QUEUED}),
    READY: frozenset({APPLIED, DISCARDED, STALE, QUEUED}),
    ANSWERED: frozenset({QUEUED, DISCARDED}),
    FAILED: frozenset({QUEUED, DISCARDED, APPLIED}),
    STALE: frozenset({QUEUED, DISCARDED, APPLIED}),
    APPLIED: frozenset({READY, FAILED}),
    DISCARDED: frozenset(),
}
REPLY_FROM = (READY, ANSWERED, FAILED)

MESSAGE_ROLES = ("user", "agent", "system")

PROPOSAL_CURRENT = "current"
PROPOSAL_SUPERSEDED = "superseded"
PROPOSAL_APPLIED = "applied"
PROPOSAL_DISCARDED = "discarded"

_SCHEMA = """
create table threads (
    id text primary key,
    created_at real not null,
    updated_at real not null,
    status text not null,
    title text not null,
    note_id integer,
    context_json text not null,
    settings_json text not null,
    session_json text,
    run_log_path text,
    error text
);
create index threads_note_id on threads (note_id);
create index threads_status on threads (status, updated_at);
create table messages (
    thread_id text not null references threads (id) on delete cascade,
    seq integer not null,
    role text not null,
    text text not null,
    html text not null,
    created_at real not null,
    primary key (thread_id, seq)
);
create table proposals (
    thread_id text not null references threads (id) on delete cascade,
    version integer not null,
    created_at real not null,
    patch_json text not null,
    base_json text not null,
    status text not null,
    primary key (thread_id, version)
);
"""


class QueueTransitionError(RuntimeError):
    """An illegal state change, or the thread was not in the expected state."""


@dataclass(frozen=True)
class Thread:
    id: str
    created_at: float
    updated_at: float
    status: str
    title: str
    note_id: int | None
    context: dict[str, Any]
    settings: dict[str, Any]
    session: dict[str, Any] | None
    run_log_path: str | None
    error: str | None


@dataclass(frozen=True)
class Message:
    thread_id: str
    seq: int
    role: str
    text: str
    html: str
    created_at: float


@dataclass(frozen=True)
class Proposal:
    thread_id: str
    version: int
    created_at: float
    patch: dict[str, Any]
    base: dict[str, Any]
    status: str


def _title(prompt: str) -> str:
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
    if len(first_line) <= TITLE_CHARS:
        return first_line
    return first_line[: TITLE_CHARS - 1] + "…"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _thread(row: sqlite3.Row) -> Thread:
    return Thread(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        status=row["status"],
        title=row["title"],
        note_id=row["note_id"],
        context=json.loads(row["context_json"]),
        settings=json.loads(row["settings_json"]),
        session=json.loads(row["session_json"]) if row["session_json"] else None,
        run_log_path=row["run_log_path"],
        error=row["error"],
    )


def _message(row: sqlite3.Row) -> Message:
    return Message(
        thread_id=row["thread_id"],
        seq=row["seq"],
        role=row["role"],
        text=row["text"],
        html=row["html"],
        created_at=row["created_at"],
    )


def _proposal(row: sqlite3.Row) -> Proposal:
    return Proposal(
        thread_id=row["thread_id"],
        version=row["version"],
        created_at=row["created_at"],
        patch=json.loads(row["patch_json"]),
        base=json.loads(row["base_json"]),
        status=row["status"],
    )


class QueueStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._lock = threading.RLock()

    @classmethod
    def open(cls, path: str | Path) -> QueueStore:
        conn = sqlite3.connect(str(path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        store = cls(conn)
        store._migrate()
        return store

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn

    def _migrate(self) -> None:
        with self._tx() as conn:
            version = conn.execute("pragma user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Agent queue schema {version} is newer than this add-on supports."
                )
            if version < 1:
                conn.executescript(_SCHEMA)
            conn.execute(f"pragma user_version = {SCHEMA_VERSION}")

    def schema_version(self) -> int:
        with self._lock:
            return int(self._conn.execute("pragma user_version").fetchone()[0])

    # threads

    def create_thread(
        self,
        context: dict[str, Any],
        settings: dict[str, Any],
        first_prompt: str,
        note_id: int | None = None,
        first_prompt_html: str = "",
    ) -> Thread:
        thread_id = uuid.uuid4().hex
        now = time.time()
        with self._tx() as conn:
            conn.execute(
                "insert into threads (id, created_at, updated_at, status, title,"
                " note_id, context_json, settings_json) values (?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    now,
                    now,
                    QUEUED,
                    _title(first_prompt),
                    note_id,
                    _dumps(context),
                    _dumps(settings),
                ),
            )
            self._insert_message(
                conn, thread_id, "user", first_prompt, first_prompt_html
            )
        thread = self.get_thread(thread_id)
        assert thread is not None
        return thread

    def get_thread(self, thread_id: str) -> Thread | None:
        with self._lock:
            row = self._conn.execute(
                "select * from threads where id = ?", (thread_id,)
            ).fetchone()
        return _thread(row) if row else None

    def threads_for_note(self, note_id: int) -> list[Thread]:
        with self._lock:
            rows = self._conn.execute(
                "select * from threads where note_id = ? order by created_at, rowid",
                (note_id,),
            ).fetchall()
        return [_thread(row) for row in rows]

    def list_threads(self, statuses: Iterable[str] | None = None) -> list[Thread]:
        sql = "select * from threads"
        args: tuple[str, ...] = ()
        if statuses is not None:
            args = tuple(statuses)
            if not args:
                return []
            sql += f" where status in ({','.join('?' for _ in args)})"
        with self._lock:
            rows = self._conn.execute(
                sql + " order by created_at, rowid", args
            ).fetchall()
        return [_thread(row) for row in rows]

    def _set_column(self, thread_id: str, column: str, value: Any) -> None:
        with self._tx() as conn:
            conn.execute(
                f"update threads set {column} = ?, updated_at = ? where id = ?",
                (value, time.time(), thread_id),
            )

    def set_error(self, thread_id: str, error: str | None) -> None:
        self._set_column(thread_id, "error", error)

    def set_settings(self, thread_id: str, settings: dict[str, Any]) -> None:
        """Replace the settings used for the thread's next run."""
        self._set_column(thread_id, "settings_json", _dumps(settings))

    def set_session(self, thread_id: str, session: dict[str, Any] | None) -> None:
        self._set_column(
            thread_id, "session_json", _dumps(session) if session is not None else None
        )

    def set_run_log_path(self, thread_id: str, path: str | None) -> None:
        self._set_column(thread_id, "run_log_path", path)

    # state machine

    def transition(self, thread_id: str, from_status: str, to_status: str) -> None:
        if to_status not in TRANSITIONS.get(from_status, frozenset()):
            raise QueueTransitionError(
                f"Illegal thread transition: {from_status} -> {to_status}."
            )
        with self._tx() as conn:
            self._transition(conn, thread_id, from_status, to_status)

    def _transition(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        from_status: str,
        to_status: str,
    ) -> None:
        cursor = conn.execute(
            "update threads set status = ?, updated_at = ? where id = ? and status = ?",
            (to_status, time.time(), thread_id, from_status),
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "select status from threads where id = ?", (thread_id,)
            ).fetchone()
            actual = row["status"] if row else "missing"
            raise QueueTransitionError(
                f"Thread {thread_id} is {actual}, not {from_status}."
            )
        closing = {APPLIED: PROPOSAL_APPLIED, DISCARDED: PROPOSAL_DISCARDED}
        if to_status in closing:
            conn.execute(
                "update proposals set status = ? where thread_id = ? and status = ?",
                (closing[to_status], thread_id, PROPOSAL_CURRENT),
            )
        elif from_status == APPLIED:
            # Reverting a failed apply: the applied proposal is current again.
            conn.execute(
                "update proposals set status = ? where thread_id = ? and version ="
                " (select max(version) from proposals where thread_id = ?"
                " and status = ?)",
                (PROPOSAL_CURRENT, thread_id, thread_id, PROPOSAL_APPLIED),
            )

    def reply(self, thread_id: str, text: str, html: str = "") -> None:
        """Append a user message and send the thread back to the worker."""
        with self._tx() as conn:
            row = conn.execute(
                "select status from threads where id = ?", (thread_id,)
            ).fetchone()
            status = row["status"] if row else "missing"
            if status not in REPLY_FROM:
                raise QueueTransitionError(f"Cannot reply to a {status} thread.")
            self._insert_message(conn, thread_id, "user", text, html)
            self._transition(conn, thread_id, status, QUEUED)
            conn.execute("update threads set error = null where id = ?", (thread_id,))

    def complete_run(
        self,
        thread_id: str,
        *,
        to_status: str,
        messages: Iterable[tuple[str, str, str]] = (),
        proposal: dict[str, Any] | None = None,
        base: dict[str, Any] | None = None,
        drop_proposal: bool = False,
        error: str | None = None,
    ) -> None:
        """Record a run's outcome in one transaction: running -> ``to_status``.

        ``messages`` are (role, text, html). ``proposal`` (with its ``base``)
        becomes the current proposal; ``drop_proposal`` instead supersedes the
        current one. Nothing is written if the thread is not running.
        """
        if to_status not in TRANSITIONS[RUNNING]:
            raise QueueTransitionError(
                f"Illegal thread transition: {RUNNING} -> {to_status}."
            )
        with self._tx() as conn:
            self._transition(conn, thread_id, RUNNING, to_status)
            for role, text, html in messages:
                self._insert_message(conn, thread_id, role, text, html)
            if proposal is not None:
                self._insert_proposal(conn, thread_id, proposal, base or {})
            elif drop_proposal:
                self._supersede_current(conn, thread_id)
            conn.execute(
                "update threads set error = ? where id = ?", (error, thread_id)
            )

    def next_runnable(self) -> Thread | None:
        with self._lock:
            row = self._conn.execute(
                "select * from threads where status = ? order by updated_at, rowid"
                " limit 1",
                (QUEUED,),
            ).fetchone()
        return _thread(row) if row else None

    def claim_next(self) -> Thread | None:
        """Atomically pop the oldest queued thread and mark it running."""
        with self._lock:
            thread = self.next_runnable()
            if thread is None:
                return None
            self.transition(thread.id, QUEUED, RUNNING)
            return self.get_thread(thread.id)

    def reset_interrupted(self) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "update threads set status = ? where status = ?", (QUEUED, RUNNING)
            )
            return cursor.rowcount

    def sweep(self, retention_days: float) -> int:
        cutoff = time.time() - retention_days * 86400
        placeholders = ",".join("?" for _ in TERMINAL_STATUSES)
        with self._tx() as conn:
            cursor = conn.execute(
                f"delete from threads where status in ({placeholders})"
                " and updated_at < ?",
                (*TERMINAL_STATUSES, cutoff),
            )
            return cursor.rowcount

    def _set_updated_at_for_tests(self, thread_id: str, updated_at: float) -> None:
        with self._tx() as conn:
            conn.execute(
                "update threads set updated_at = ? where id = ?",
                (updated_at, thread_id),
            )

    # messages

    def add_message(
        self, thread_id: str, role: str, text: str, html: str = ""
    ) -> Message:
        with self._tx() as conn:
            seq = self._insert_message(conn, thread_id, role, text, html)
            row = conn.execute(
                "select * from messages where thread_id = ? and seq = ?",
                (thread_id, seq),
            ).fetchone()
        return _message(row)

    def _insert_message(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        role: str,
        text: str,
        html: str,
    ) -> int:
        if role not in MESSAGE_ROLES:
            raise ValueError(f"Unknown message role: {role!r}.")
        seq = conn.execute(
            "select coalesce(max(seq), 0) + 1 from messages where thread_id = ?",
            (thread_id,),
        ).fetchone()[0]
        now = time.time()
        conn.execute(
            "insert into messages (thread_id, seq, role, text, html, created_at)"
            " values (?,?,?,?,?,?)",
            (thread_id, seq, role, text, html or "", now),
        )
        conn.execute("update threads set updated_at = ? where id = ?", (now, thread_id))
        return int(seq)

    def messages(self, thread_id: str) -> list[Message]:
        with self._lock:
            rows = self._conn.execute(
                "select * from messages where thread_id = ? order by seq",
                (thread_id,),
            ).fetchall()
        return [_message(row) for row in rows]

    # proposals

    def add_proposal(
        self,
        thread_id: str,
        patch: dict[str, Any],
        base: dict[str, Any],
    ) -> Proposal:
        with self._tx() as conn:
            version = self._insert_proposal(conn, thread_id, patch, base)
            row = conn.execute(
                "select * from proposals where thread_id = ? and version = ?",
                (thread_id, version),
            ).fetchone()
        return _proposal(row)

    def _supersede_current(self, conn: sqlite3.Connection, thread_id: str) -> None:
        conn.execute(
            "update proposals set status = ? where thread_id = ? and status = ?",
            (PROPOSAL_SUPERSEDED, thread_id, PROPOSAL_CURRENT),
        )

    def _insert_proposal(
        self,
        conn: sqlite3.Connection,
        thread_id: str,
        patch: dict[str, Any],
        base: dict[str, Any],
    ) -> int:
        now = time.time()
        version = conn.execute(
            "select coalesce(max(version), 0) + 1 from proposals where thread_id = ?",
            (thread_id,),
        ).fetchone()[0]
        self._supersede_current(conn, thread_id)
        conn.execute(
            "insert into proposals (thread_id, version, created_at, patch_json,"
            " base_json, status) values (?,?,?,?,?,?)",
            (
                thread_id,
                version,
                now,
                _dumps(patch),
                _dumps(base),
                PROPOSAL_CURRENT,
            ),
        )
        conn.execute("update threads set updated_at = ? where id = ?", (now, thread_id))
        return int(version)

    def current_proposal(self, thread_id: str) -> Proposal | None:
        with self._lock:
            row = self._conn.execute(
                "select * from proposals where thread_id = ? and status = ?",
                (thread_id, PROPOSAL_CURRENT),
            ).fetchone()
        return _proposal(row) if row else None

    def proposals(self, thread_id: str) -> list[Proposal]:
        with self._lock:
            rows = self._conn.execute(
                "select * from proposals where thread_id = ? order by version",
                (thread_id,),
            ).fetchall()
        return [_proposal(row) for row in rows]
