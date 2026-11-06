# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The few Qt classes the plugin host supports: signals, actions for the
shell's menus, and timers on the host's loop. There are no widgets; add-on
UI is web panels (aqt.native). The widget names exist so modules that only
mention them (type hints, `_aqt.hooks`) still import."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any


class Signal:
    def __init__(self) -> None:
        self._slots: list[Callable[..., Any]] = []
        self._blocked = False

    def connect(self, slot: Callable[..., Any]) -> None:
        self._slots.append(slot)

    def disconnect(self, slot: Callable[..., Any] | None = None) -> None:
        if slot is None:
            self._slots.clear()
        elif slot in self._slots:
            self._slots.remove(slot)

    def emit(self, *args: Any) -> None:
        if self._blocked:
            return
        for slot in list(self._slots):
            slot(*args[: _positional_count(slot, len(args))])


def _positional_count(slot: Callable[..., Any], available: int) -> int:
    """Qt drops trailing arguments a slot doesn't take (`triggered(bool)`
    to a function of none, say)."""
    try:
        params = inspect.signature(slot).parameters.values()
    except (TypeError, ValueError):
        return available
    if any(p.kind == p.VAR_POSITIONAL for p in params):
        return available
    return min(
        available,
        sum(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) for p in params),
    )


def qconnect(signal: Any, func: Callable) -> None:
    signal.connect(func)


class QObject:
    def __init__(self, parent: Any = None) -> None:
        self._parent = parent


class QAction(QObject):
    """A menu item. Added to `mw.form.menuTools` or to a reviewer context
    menu, it shows in the shell's menu; changes are sent on."""

    def __init__(self, text: str = "", parent: Any = None) -> None:
        super().__init__(parent)
        self._text = text
        self._enabled = True
        self._visible = True
        self._checkable = False
        self._checked = False
        self._menu: Any = None
        self.triggered = Signal()

    def _changed(self) -> None:
        if self._menu is not None:
            self._menu.mw.native.menu_changed(self._menu)

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self._changed()

    def setEnabled(self, enabled: bool) -> None:
        if enabled != self._enabled:
            self._enabled = enabled
            self._changed()

    def isEnabled(self) -> bool:
        return self._enabled

    def setVisible(self, visible: bool) -> None:
        if visible != self._visible:
            self._visible = visible
            self._changed()

    def isVisible(self) -> bool:
        return self._visible

    def setCheckable(self, checkable: bool) -> None:
        self._checkable = checkable

    def setChecked(self, checked: bool) -> None:
        self._checked = checked

    def isChecked(self) -> bool:
        return self._checked

    def setShortcut(self, *_args: Any) -> None:
        pass

    def setToolTip(self, *_args: Any) -> None:
        pass

    def trigger(self) -> None:
        if self._enabled:
            if self._checkable:
                self._checked = not self._checked
            self.triggered.emit(self._checked)


class QMenu(QObject):
    """A context menu being filled by a hook; the shell shows its actions."""

    def __init__(self, title: str = "", parent: Any = None) -> None:
        super().__init__(parent)
        self.title = title
        self.actions: list[QAction] = []

    def addAction(self, action: str | QAction, *_args: Any) -> QAction:
        if isinstance(action, str):
            action = QAction(action, self)
        self.actions.append(action)
        return action

    def addSeparator(self) -> None:
        pass

    def addMenu(self, _title: str) -> QMenu:
        # submenus are flattened into this one
        return self


class QTimer(QObject):
    """A timer on the host's loop (so its callbacks run on the main thread)."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.timeout = Signal()
        self._single_shot = False
        self._interval = 0
        self._generation = 0
        self._active = False

    def setSingleShot(self, single: bool) -> None:
        self._single_shot = single

    def isSingleShot(self) -> bool:
        return self._single_shot

    def setInterval(self, ms: int) -> None:
        self._interval = ms

    def interval(self) -> int:
        return self._interval

    def isActive(self) -> bool:
        return self._active

    def start(self, ms: int | None = None) -> None:
        if ms is not None:
            self._interval = ms
        self._generation += 1
        self._active = True
        self._schedule(self._generation)

    def stop(self) -> None:
        self._generation += 1
        self._active = False

    def _schedule(self, generation: int) -> None:
        import aqt

        def fire() -> None:
            if generation != self._generation:
                return
            if self._single_shot:
                self._active = False
            else:
                self._schedule(generation)
            self.timeout.emit()

        assert aqt.mw
        aqt.mw.loop.call_later(max(0, self._interval) / 1000, fire)

    @staticmethod
    def singleShot(ms: int, func: Callable[[], None]) -> None:
        import aqt

        assert aqt.mw
        aqt.mw.loop.call_later(ms / 1000, func)


class _NoWidgets:
    """Qt widgets can't be shown by the native shell."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            f"{type(self).__name__}: Qt widgets aren't available in the native "
            "shell's plugin host; use a web panel (aqt.native.open_panel)"
        )


class QWidget(_NoWidgets):
    pass


class QDialog(QWidget):
    pass


class QEvent:
    pass


class QModelIndex:
    pass


class QMimeData:
    pass


class Qt:
    pass
