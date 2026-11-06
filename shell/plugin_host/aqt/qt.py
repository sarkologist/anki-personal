# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Qt the plugin host supports: signals, actions for the shell's
menus, timers on the host's loop, and the widgets the shell can draw
(aqt.widgets). Other Qt names can be imported and named, but not created
(see `unsupported`); add-ons can also use web panels (aqt.native)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .widgets import (  # noqa: F401
    AnkiWebView,
    QAbstractButton,
    QBoxLayout,
    QCheckBox,
    QComboBox,
    QDialog,
    QDockWidget,
    QEvent,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QKeyEvent,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QObject,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    Qt,
    QTextCursor,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    Signal,
    qconnect,
    unsupported,
)


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

    def setShortcutContext(self, *_args: Any) -> None:
        pass

    def setToolTip(self, *_args: Any) -> None:
        pass

    def setIcon(self, *_args: Any) -> None:
        pass

    def setMenuRole(self, *_args: Any) -> None:
        pass

    def setData(self, data: Any) -> None:
        self._data = data

    def data(self) -> Any:
        return getattr(self, "_data", None)

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


class QKeySequence:
    class StandardKey(metaclass=type(Qt)):
        pass

    def __init__(self, keys: Any = "") -> None:
        self._keys = keys if isinstance(keys, str) else str(keys)

    def toString(self, *_args: Any) -> str:
        return self._keys


class QShortcut(QObject):
    """Shortcuts on Qt windows: the shell has its own, so these never fire."""

    def __init__(
        self, key: Any = None, parent: Any = None, *_args: Any, **kwargs: Any
    ) -> None:
        super().__init__(parent)
        self.activated = Signal()
        if callable(kwargs.get("activated")):
            self.activated.connect(kwargs["activated"])

    def setKey(self, *_args: Any) -> None:
        pass

    def setContext(self, *_args: Any) -> None:
        pass

    def setEnabled(self, *_args: Any) -> None:
        pass


class sip:
    """`sip.isdeleted`: a closed browser (or editor window) is deleted."""

    @staticmethod
    def isdeleted(obj: Any) -> bool:
        return bool(getattr(obj, "_deleted", False) or getattr(obj, "_closed", False))


class QApplication:
    """The focus as the shell last reported it (aqt.editor)."""

    @staticmethod
    def focusWidget() -> Any:
        from aqt import editor

        return editor.focus_widget()

    @staticmethod
    def instance() -> Any:
        return None

    @staticmethod
    def beep() -> None:
        pass


def __getattr__(name: str) -> Any:
    # other Qt classes: nameable, not creatable
    if name.startswith("Q"):
        value = unsupported(name)
        globals()[name] = value
        return value
    raise AttributeError(name)
