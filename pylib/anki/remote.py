# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""pylib in another process than the backend: the native shell's plugin host.

A RemoteBackend sends each backend call over a Unix socket to the shell,
which runs it on its own (already open) collection, so the plugin host
needs no _rsbridge (and doesn't load one).

Frames are `u32 length | u8 kind | u32 id | payload` (big endian). A reply
(OK or ERR) carries the id of the request it answers; id 0 is a request
that wants none. Either side may send requests, and each matches replies
against its own pending requests.
"""

from __future__ import annotations

import itertools
import os
import socket
import struct
import threading
import weakref
from collections.abc import Callable
from typing import Any

import anki.lang
from anki import backend_pb2
from anki._backend import RustBackend, backend_exception_to_pylib
from anki.utils import from_json_bytes, to_json_bytes

# kinds
CALL = 1  # payload: u32 service, u32 method, protobuf input
DB = 2  # payload: db_command JSON
HOST = 3  # JSON request to the shell
EVENT = 4  # JSON request from the shell
OK = 5
ERR = 6  # BackendError bytes for CALL and DB, else UTF-8 text

_HEADER = struct.Struct(">IBI")
_CALL = struct.Struct(">II")


def write_frame(sock: socket.socket, kind: int, id: int, payload: bytes) -> None:
    sock.sendall(_HEADER.pack(len(payload) + 5, kind, id) + payload)


def _read_exactly(sock: socket.socket, count: int) -> bytes | None:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data += chunk
    return bytes(data)


def read_frame(sock: socket.socket) -> tuple[int, int, bytes] | None:
    """The next (kind, id, payload), or None once the socket is closed."""
    try:
        header = _read_exactly(sock, _HEADER.size)
        if header is None:
            return None
        length, kind, id = _HEADER.unpack(header)
        payload = _read_exactly(sock, length - 5)
    except OSError:
        return None
    if payload is None:
        return None
    return kind, id, payload


def call_payload(service: int, method: int, input: bytes) -> bytes:
    return _CALL.pack(service, method) + input


def split_call(payload: bytes) -> tuple[int, int, bytes]:
    service, method = _CALL.unpack_from(payload)
    return service, method, payload[_CALL.size :]


class RemoteError(Exception):
    """An ERR reply to a request other than CALL or DB."""


class Connection:
    """The socket to the shell, shared by every thread. Requests from the
    shell go to `on_request(kind, id, payload)`, on the reader thread;
    answer them with `reply()`."""

    def __init__(
        self,
        sock: socket.socket,
        on_request: Callable[[int, int, bytes], None] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.sock = sock
        self.on_request = on_request
        self.on_closed = on_closed
        self._write = threading.Lock()
        self._ids = itertools.count(1)
        self._pending: dict[int, list[Any]] = {}
        self._pending_lock = threading.Lock()
        self.closed = False
        threading.Thread(target=self._read, name="anki.remote", daemon=True).start()

    def request(self, kind: int, payload: bytes) -> tuple[int, bytes]:
        """Send a request and wait for its reply: (OK or ERR, payload)."""
        done = threading.Event()
        slot: list[Any] = [done, None]
        with self._pending_lock:
            if self.closed:
                raise ConnectionError("the shell closed the plugin host connection")
            id = next(self._ids)
            self._pending[id] = slot
        try:
            with self._write:
                write_frame(self.sock, kind, id, payload)
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(id, None)
            raise ConnectionError(str(exc)) from exc
        done.wait()
        if slot[1] is None:
            raise ConnectionError("the shell closed the plugin host connection")
        return slot[1]

    def notify(self, kind: int, payload: bytes) -> None:
        """A request without a reply (id 0)."""
        try:
            with self._write:
                write_frame(self.sock, kind, 0, payload)
        except OSError:
            pass

    def reply(self, id: int, ok: bool, payload: bytes) -> None:
        try:
            with self._write:
                write_frame(self.sock, OK if ok else ERR, id, payload)
        except OSError:
            pass

    def json_request(self, kind: int, message: dict[str, Any]) -> Any:
        status, payload = self.request(kind, to_json_bytes(message))
        if status == ERR:
            raise RemoteError(payload.decode("utf-8", "replace"))
        return from_json_bytes(payload) if payload else None

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def _read(self) -> None:
        while frame := read_frame(self.sock):
            kind, id, payload = frame
            if kind in (OK, ERR):
                with self._pending_lock:
                    slot = self._pending.pop(id, None)
                if slot:
                    slot[1] = (kind, payload)
                    slot[0].set()
            elif self.on_request:
                self.on_request(kind, id, payload)
        with self._pending_lock:
            self.closed = True
            pending, self._pending = self._pending, {}
        for slot in pending.values():
            slot[0].set()
        if self.on_closed:
            self.on_closed()


class RemoteBackend(RustBackend):
    """A backend whose collection is the shell's open one. It attaches to
    it: opening checks the path, and closing leaves it open."""

    def __init__(self, connection: Connection, collection_path: str) -> None:
        self.connection = connection
        self.collection_path = os.path.abspath(collection_path)

    def _run_command(self, service: int, method: int, input: bytes) -> bytes:
        status, out = self.connection.request(
            CALL, call_payload(service, method, input)
        )
        if status == OK:
            return out
        raise _backend_error(out)

    def _db_command(self, input: dict[str, Any]) -> Any:
        status, out = self.connection.request(DB, to_json_bytes(input))
        if status == OK:
            return from_json_bytes(out)
        raise _backend_error(out)

    def open_collection(self, collection_path: str, **_kwargs: Any) -> None:  # type: ignore[override]
        if os.path.abspath(collection_path) != self.collection_path:
            raise Exception(
                f"can only attach to the shell's collection ({self.collection_path})"
            )

    def close_collection(self, **_kwargs: Any) -> None:  # type: ignore[override]
        pass

    def use_for_translations(self, lang: str = "en") -> None:
        """anki.lang's global translations, without a local backend."""
        anki.lang.current_lang = lang
        anki.lang.current_i18n = self
        anki.lang.tr_legacyglobal.backend = weakref.ref(self)


def _backend_error(payload: bytes) -> Exception:
    err = backend_pb2.BackendError()
    err.ParseFromString(payload)
    return backend_exception_to_pylib(err)
