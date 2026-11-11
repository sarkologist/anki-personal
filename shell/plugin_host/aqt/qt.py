# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The Qt the plugin host supports: signals, actions for the shell's
menus and the widgets' button menus, timers on the host's loop, and the
widgets the shell can draw (aqt.widgets). Other Qt names can be imported and named, but not created
(see `unsupported`); add-ons can also use web panels (aqt.native)."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from typing import Any

from .widgets import (  # noqa: F401
    AnkiWebView,
    QAbstractButton,
    QAbstractItemView,
    QBoxLayout,
    QByteArray,
    QCheckBox,
    QCloseEvent,
    QComboBox,
    QDateTime,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QEvent,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QKeyEvent,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QObject,
    QPlainTextEdit,
    QPushButton,
    QShortcut,
    QSize,
    QSizePolicy,
    QSplitter,
    Qt,
    QTableWidget,
    QTableWidgetItem,
    QTextCursor,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
    Signal,
    _keys,
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
        self._shortcut = ""
        # the button menus showing it
        self._in: list[QMenu] = []
        self.triggered = Signal()

    def _changed(self) -> None:
        if self._menu is not None:
            self._menu.mw.native.menu_changed(self._menu)
        for menu in self._in:
            menu._changed()

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

    def setShortcut(self, keys: Any) -> None:
        self._shortcut = _keys(keys)
        self._changed()

    def shortcut(self) -> QKeySequence:
        return QKeySequence(self._shortcut)

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
    """A context menu being filled by a hook, or a button's menu; the shell
    shows its actions."""

    def __init__(self, title: str = "", parent: Any = None) -> None:
        super().__init__(parent)
        self.title = title
        self.actions: list[QAction] = []
        # the actions, and None for separators
        self._entries: list[QAction | None] = []
        self._buttons: list[Any] = []

    def addAction(self, action: str | QAction, *_args: Any) -> QAction:
        if isinstance(action, str):
            action = QAction(action, self)
        self.actions.append(action)
        self._entries.append(action)
        action._in.append(self)
        self._changed()
        return action

    def addSeparator(self) -> None:
        self._entries.append(None)
        self._changed()

    def _changed(self) -> None:
        for ref in self._buttons:
            if (button := ref()) is not None:
                button._changed()

    def _items(self) -> list[dict[str, Any]]:
        """For the page: the visible actions, and separators between them."""
        items: list[dict[str, Any]] = []
        for entry in self._entries:
            if entry is None:
                if items and "sep" not in items[-1]:
                    items.append({"sep": True})
            elif entry.isVisible():
                items.append(
                    {
                        "id": _menu_id(entry),
                        "text": entry.text(),
                        "enabled": entry.isEnabled(),
                    }
                )
        if items and "sep" in items[-1]:
            items.pop()
        return items

    def _trigger(self, id: Any) -> None:
        for entry in self._entries:
            if entry is not None and entry.isVisible() and _menu_id(entry) == id:
                entry.trigger()
                return

    def addMenu(self, _title: str) -> QMenu:
        # submenus are flattened into this one
        return self


_menu_ids = itertools.count(1)


def _menu_id(action: QAction) -> int:
    """An action's id in the button menus' pages."""
    if not hasattr(action, "_menu_id"):
        action._menu_id = next(_menu_ids)
    return action._menu_id


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
