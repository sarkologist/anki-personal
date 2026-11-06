# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator

import pytest

from anki import remote
from anki.collection import Collection
from anki.errors import NotFoundError
from tests.shared import getEmptyCol


class FakeShell:
    """The shell's side of the socket: serves calls from the shell's own
    open collection, as `shell/src/plugins` does."""

    def __init__(self, col: Collection, sock: socket.socket) -> None:
        self.col = col
        self.sock = sock
        self.write = threading.Lock()
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        native = self.col._backend._backend
        while frame := remote.read_frame(self.sock):
            kind, id, payload = frame
            try:
                if kind == remote.CALL:
                    service, method, input = remote.split_call(payload)
                    out = native.command(service, method, input)
                elif kind == remote.DB:
                    out = native.db_command(payload)
                else:
                    raise Exception(f"unexpected {kind}")
                reply = remote.OK
            except Exception as error:
                out, reply = bytes(error.args[0]), remote.ERR
            with self.write:
                remote.write_frame(self.sock, reply, id, out)


@pytest.fixture
def attached() -> Iterator[tuple[Collection, Collection]]:
    shell_col = getEmptyCol()
    ours, theirs = socket.socketpair()
    FakeShell(shell_col, theirs)
    connection = remote.Connection(ours)
    plugin_col = Collection(
        shell_col.path, backend=remote.RemoteBackend(connection, shell_col.path)
    )
    yield shell_col, plugin_col
    plugin_col.close()
    connection.close()
    shell_col.close()


def test_a_remote_collection_reads_and_writes_the_shells_collection(attached):
    shell_col, plugin_col = attached
    note = plugin_col.newNote()
    note["Front"] = "remote"
    plugin_col.add_note(note, plugin_col.decks.id("Default"))
    assert shell_col.find_notes("remote") == [note.id]
    assert plugin_col.db.scalar("select count() from notes") == 1


def test_backend_errors_keep_their_pylib_type(attached):
    _shell_col, plugin_col = attached
    with pytest.raises(NotFoundError):
        plugin_col.get_card(12345)


def test_closing_a_remote_collection_leaves_the_shells_open(attached):
    shell_col, plugin_col = attached
    plugin_col.close()
    assert shell_col.db.scalar("select count() from cards") == 0


def test_a_remote_collection_only_attaches_to_the_shells_collection(attached):
    shell_col, _plugin_col = attached
    ours, theirs = socket.socketpair()
    FakeShell(shell_col, theirs)
    backend = remote.RemoteBackend(remote.Connection(ours), shell_col.path)
    with pytest.raises(Exception, match="attach"):
        Collection(shell_col.path + ".other", backend=backend)


def test_the_attached_path_may_be_spelled_differently(attached):
    shell_col, _plugin_col = attached
    ours, theirs = socket.socketpair()
    FakeShell(shell_col, theirs)
    folder, name = os.path.split(shell_col.path)
    spelled = os.path.join(folder, ".", name)
    backend = remote.RemoteBackend(remote.Connection(ours), spelled)
    assert Collection(spelled, backend=backend).db.scalar("select 1") == 1


def test_concurrent_calls_get_their_own_replies(attached):
    _shell_col, plugin_col = attached
    names = [f"deck {i}" for i in range(20)]
    ids: dict[str, int] = {}

    def add(name: str) -> None:
        ids[name] = plugin_col.decks.id(name)

    threads = [threading.Thread(target=add, args=(name,)) for name in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert {plugin_col.decks.name(ids[name]) for name in names} == set(names)


def test_calls_fail_once_the_shell_goes_away(attached):
    _shell_col, plugin_col = attached
    plugin_col._backend.connection.sock.shutdown(socket.SHUT_RDWR)
    with pytest.raises(ConnectionError):
        plugin_col.find_cards("")


def test_remote_mode_imports_without_rsbridge():
    """The plugin host has no _rsbridge, so pylib must import without it."""
    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path, target=None):\n"
        "        if name == 'anki._rsbridge':\n"
        "            raise ImportError('no rsbridge here')\n"
        "sys.meta_path.insert(0, Block())\n"
        "import anki.remote, anki.collection, anki.lang\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == "ok", out.stderr
