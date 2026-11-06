# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""A pytest plugin (`-p remote_everywhere`, with this folder on PYTHONPATH): every Collection made without a backend is attached over
anki.remote's socket protocol to a real local one (the "shell")."""
import socket
import threading

from anki import remote
from anki.collection import Collection

_init, _close = Collection.__init__, Collection.close
calls = [0]


def serve(col, sock):
    native = col._backend._backend
    lock = threading.Lock()
    while frame := remote.read_frame(sock):
        kind, id, payload = frame
        calls[0] += 1
        try:
            out = native.command(*remote.split_call(payload)) if kind == remote.CALL else native.db_command(payload)
            status = remote.OK
        except Exception as error:
            out, status = bytes(error.args[0]), remote.ERR
        with lock:
            remote.write_frame(sock, status, id, out)


def init(self, path, backend=None, server=False):
    if backend is not None:
        return _init(self, path, backend, server)
    shell = Collection.__new__(Collection)
    _init(shell, path)
    ours, theirs = socket.socketpair()
    threading.Thread(target=serve, args=(shell, theirs), daemon=True).start()
    _init(self, path, backend=remote.RemoteBackend(remote.Connection(ours), shell.path))
    self._test_shell = shell


def close(self, downgrade=False):
    _close(self, downgrade)
    shell = getattr(self, "_test_shell", None)
    if shell is not None:
        self._backend.connection.close()
        _close(shell, downgrade)


Collection.__init__ = init
Collection.close = close


def pytest_terminal_summary(terminalreporter):
    terminalreporter.write_line(f"remote calls served: {calls[0]}")
