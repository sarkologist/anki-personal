# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""aqt.operations for the plugin host: ops run on the host's task threads,
and a CollectionOp's changes go to the add-ons' hooks and to the shell,
which refreshes its pages."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, Generic, TypeVar

import aqt
from anki.collection import Collection, OpChanges

T = TypeVar("T")


def show_exception(exception: Exception) -> None:
    from aqt.main import report_exception
    from aqt.utils import showWarning

    try:
        raise exception
    except Exception:
        report_exception("an operation")
    showWarning(str(exception))


class CollectionOp(Generic[T]):
    def __init__(self, parent: Any, op: Callable[[Collection], T]) -> None:
        self._op = op
        self._success: Callable[[T], Any] | None = None
        self._failure: Callable[[Exception], Any] | None = None

    def success(self, success: Callable[[T], Any] | None) -> CollectionOp[T]:
        self._success = success
        return self

    def failure(self, failure: Callable[[Exception], Any] | None) -> CollectionOp[T]:
        self._failure = failure
        return self

    def with_backend_progress(self, *_args: Any) -> CollectionOp[T]:
        return self

    def run_in_background(self, *, initiator: object | None = None) -> None:
        mw = aqt.mw
        assert mw

        def done(future: Future) -> None:
            if exception := future.exception():
                if not isinstance(exception, Exception):
                    future.result()
                if self._failure:
                    self._failure(exception)
                else:
                    show_exception(exception)
                return
            result = future.result()
            try:
                if self._success:
                    self._success(result)
            finally:
                on_op_finished(result, initiator)

        mw.taskman.run_in_background(lambda: self._op(mw.col), done)


def on_op_finished(result: Any, initiator: object | None) -> None:
    mw = aqt.mw
    assert mw
    changes = result if isinstance(result, OpChanges) else result.changes
    if changes.notetype and mw.col:
        mw.col.models._clear_cache()
    aqt.gui_hooks.operation_did_execute(changes, initiator)
    mw.native.operation_did_execute(changes)


class QueryOp(Generic[T]):
    def __init__(
        self,
        *,
        parent: Any,
        op: Callable[[Collection], T],
        success: Callable[[T], Any],
    ) -> None:
        self._op = op
        self._success = success
        self._failure: Callable[[Exception], Any] | None = None
        self._uses_collection = True

    def failure(self, failure: Callable[[Exception], Any] | None) -> QueryOp[T]:
        self._failure = failure
        return self

    def without_collection(self) -> QueryOp[T]:
        self._uses_collection = False
        return self

    def with_progress(self, label: str | None = None) -> QueryOp[T]:
        return self

    def with_backend_progress(self, *_args: Any) -> QueryOp[T]:
        return self

    def run_in_background(self) -> None:
        mw = aqt.mw
        assert mw

        def op() -> T:
            return self._op(mw.col)  # type: ignore[arg-type]

        def done(future: Future) -> None:
            if exception := future.exception():
                if not isinstance(exception, Exception):
                    future.result()
                if self._failure:
                    self._failure(exception)
                else:
                    show_exception(exception)
                return
            self._success(future.result())

        mw.taskman.run_in_background(op, done, uses_collection=self._uses_collection)
