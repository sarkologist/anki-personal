# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Qt widgets drawn by the native shell (aqt.qt re-exports them).

The widgets the supported add-ons build their forms from keep their state
here, as Qt's do; a widget shown somewhere (a dock in an editor window, or
a window of its own: a main window or a dialog) is a [Root], which the
shell draws with a generic page (`widgets.js` in the shell). The page asks
for the whole tree when it loads, and gets ordered batches of changes
afterwards (`widget_update`); what the user does comes back as
`widget_event`s, which run the widgets' signals as Qt would. A modal
dialog's `exec` runs the host's loop until it's answered, as Qt's does.

Other widgets raise NotImplementedError when they're created, and so does
showing a widget that isn't in a window."""

from __future__ import annotations

import base64
import datetime
import enum
import functools
import inspect
import itertools
import re
import weakref
from collections.abc import Callable, Iterable
from typing import Any

# Signals and objects
##########################################################################


class Signal:
    """A Qt signal. Bound to its object, it's quiet while the object's
    signals are blocked."""

    def __init__(self, owner: Any = None) -> None:
        self._slots: list[Callable[..., Any]] = []
        self._blocked = False
        self._owner = weakref.ref(owner) if owner is not None else None

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
        owner = self._owner() if self._owner else None
        if owner is not None and owner.signalsBlocked():
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
    def __init__(self, parent: Any = None, *_args: Any, **_kwargs: Any) -> None:
        self._parent = parent
        self._object_name = ""
        self._signals_blocked = False
        self._shortcuts_kept: list[QShortcut] = []

    def parent(self) -> Any:
        return self._parent

    def setParent(self, parent: Any, *_args: Any) -> None:
        self._parent = parent

    def setObjectName(self, name: str) -> None:
        self._object_name = name

    def objectName(self) -> str:
        return self._object_name

    def blockSignals(self, block: bool) -> bool:
        was, self._signals_blocked = self._signals_blocked, bool(block)
        return was

    def signalsBlocked(self) -> bool:
        return self._signals_blocked

    def deleteLater(self) -> None:
        pass


# Enums
##########################################################################


class _Names(type):
    """Enum namespaces that also answer for members nobody gave a meaning
    (so modules that only mention them import)."""

    def __getattr__(cls, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        value = _Placeholder(f"{cls.__name__}.{name}")
        setattr(cls, name, value)
        return value


class _Placeholder(int):
    _next = itertools.count(1 << 40)

    def __new__(cls, name: str) -> _Placeholder:
        value = super().__new__(cls, next(cls._next))
        value._name = name  # type: ignore[attr-defined]
        return value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        return _Placeholder(f"{self._name}.{name}")  # type: ignore[attr-defined]

    def __or__(self, other: Any) -> _Placeholder:
        return self

    __ror__ = __or__

    def __repr__(self) -> str:
        return self._name  # type: ignore[attr-defined]


class Qt(metaclass=_Names):
    class Key(enum.IntEnum):
        Key_Escape = 0x01000000
        Key_Tab = 0x01000001
        Key_Backspace = 0x01000003
        Key_Return = 0x01000004
        Key_Enter = 0x01000005
        Key_Delete = 0x01000007
        Key_Home = 0x01000010
        Key_End = 0x01000011
        Key_Left = 0x01000012
        Key_Up = 0x01000013
        Key_Right = 0x01000014
        Key_Down = 0x01000015
        Key_PageUp = 0x01000016
        Key_PageDown = 0x01000017
        Key_Space = 0x20
        Key_unknown = 0x01FFFFFF

    class KeyboardModifier(enum.IntFlag):
        NoModifier = 0
        ShiftModifier = 0x02000000
        # Qt's Control is Command on macOS, and Meta is Control
        ControlModifier = 0x04000000
        AltModifier = 0x08000000
        MetaModifier = 0x10000000

    class DockWidgetArea(enum.IntFlag):
        NoDockWidgetArea = 0
        LeftDockWidgetArea = 1
        RightDockWidgetArea = 2
        TopDockWidgetArea = 4
        BottomDockWidgetArea = 8
        AllDockWidgetAreas = 15

    class TextFormat(enum.Enum):
        PlainText = 0
        RichText = 1
        AutoText = 2
        MarkdownText = 3

    class Orientation(enum.Enum):
        Horizontal = 1
        Vertical = 2

    class ArrowType(enum.Enum):
        NoArrow = 0
        UpArrow = 1
        DownArrow = 2
        LeftArrow = 3
        RightArrow = 4

    class SortOrder(enum.IntEnum):
        AscendingOrder = 0
        DescendingOrder = 1

    class CheckState(enum.IntEnum):
        Unchecked = 0
        PartiallyChecked = 1
        Checked = 2

    class ItemDataRole(enum.IntEnum):
        DisplayRole = 0
        ToolTipRole = 3
        UserRole = 256


class QSizePolicy(metaclass=_Names):
    def __init__(self, *_args: Any) -> None:
        pass


# Events
##########################################################################


class QEvent(metaclass=_Names):
    def __init__(self, *_args: Any) -> None:
        self._accepted = True

    def accept(self) -> None:
        self._accepted = True

    def ignore(self) -> None:
        self._accepted = False

    def isAccepted(self) -> bool:
        return self._accepted


class QKeyEvent(QEvent):
    def __init__(self, key: int, modifiers: int, text: str = "") -> None:
        super().__init__()
        self._key = key
        self._modifiers = Qt.KeyboardModifier(modifiers)
        self._text = text

    def key(self) -> int:
        return self._key

    def modifiers(self) -> Qt.KeyboardModifier:
        return self._modifiers

    def text(self) -> str:
        return self._text


class QCloseEvent(QEvent):
    pass


class QSize:
    def __init__(self, width: int = 0, height: int = 0) -> None:
        self._width = int(width)
        self._height = int(height)

    def width(self) -> int:
        return self._width

    def height(self) -> int:
        return self._height


class QByteArray:
    def __init__(self, data: Any = b"") -> None:
        self._data = data.encode() if isinstance(data, str) else bytes(data)

    @staticmethod
    def fromBase64(data: Any) -> QByteArray:
        try:
            return QByteArray(base64.b64decode(bytes(QByteArray(data))))
        except ValueError:
            return QByteArray()

    def toBase64(self) -> QByteArray:
        return QByteArray(base64.b64encode(self._data))

    def data(self) -> bytes:
        return self._data

    def isEmpty(self) -> bool:
        return not self._data

    def size(self) -> int:
        return len(self._data)

    def __bytes__(self) -> bytes:
        return self._data


def _keys(keys: Any) -> str:
    """A key sequence as Qt writes it ("Ctrl+Shift+A")."""
    to_string = getattr(keys, "toString", None)
    return str(to_string() if callable(to_string) else keys or "")


# what the page's KeyboardEvent.key is to Qt
_PAGE_KEYS = {
    "Escape": Qt.Key.Key_Escape,
    "Tab": Qt.Key.Key_Tab,
    "Backspace": Qt.Key.Key_Backspace,
    "Enter": Qt.Key.Key_Return,
    "Delete": Qt.Key.Key_Delete,
    "Home": Qt.Key.Key_Home,
    "End": Qt.Key.Key_End,
    "ArrowLeft": Qt.Key.Key_Left,
    "ArrowUp": Qt.Key.Key_Up,
    "ArrowRight": Qt.Key.Key_Right,
    "ArrowDown": Qt.Key.Key_Down,
    "PageUp": Qt.Key.Key_PageUp,
    "PageDown": Qt.Key.Key_PageDown,
    " ": Qt.Key.Key_Space,
}


def key_event(event: dict[str, Any]) -> QKeyEvent:
    """A page keydown (`{key, shift, alt, meta, ctrl}`) as Qt's."""
    name = str(event.get("key", ""))
    if name in _PAGE_KEYS:
        key: int = _PAGE_KEYS[name]
        text = "\r" if name == "Enter" else ""
    elif len(name) == 1:
        key = ord(name.upper())
        text = name
    else:
        key, text = Qt.Key.Key_unknown, ""
    mods = Qt.KeyboardModifier.NoModifier
    for flag, modifier in (
        ("shift", Qt.KeyboardModifier.ShiftModifier),
        ("meta", Qt.KeyboardModifier.ControlModifier),
        ("alt", Qt.KeyboardModifier.AltModifier),
        ("ctrl", Qt.KeyboardModifier.MetaModifier),
    ):
        if event.get(flag):
            mods |= modifier
    return QKeyEvent(key, mods, text)


# Roots: widgets shown by the shell
##########################################################################

# every QShortcut, for the roots' pages
_shortcuts: weakref.WeakSet[QShortcut] = weakref.WeakSet()


class QShortcut(QObject):
    """A shortcut in a dock or window, fired by its page; on another
    window (the main one), it never fires (the shell has its own keys)."""

    def __init__(
        self, key: Any = None, parent: Any = None, *_args: Any, **kwargs: Any
    ) -> None:
        super().__init__(parent)
        self._id = next(_ids)
        _widgets[self._id] = self
        self._keys = _keys(key)
        self._enabled = True
        self.activated = Signal(self)
        self.activatedAmbiguously = Signal(self)
        if callable(kwargs.get("activated")):
            self.activated.connect(kwargs["activated"])
        # its parent keeps it, as Qt's owns it
        if isinstance(parent, QObject):
            parent._shortcuts_kept.append(self)
        _shortcuts.add(self)
        self._structure_changed()

    def _structure_changed(self) -> None:
        if root := root_of(self):
            root.structure_changed()

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "shortcut" and self._enabled:
            self.activated.emit()
        return None

    def setKey(self, key: Any) -> None:
        self._keys = _keys(key)
        self._structure_changed()

    def key(self) -> Any:
        from .qt import QKeySequence

        return QKeySequence(self._keys)

    def setContext(self, *_args: Any) -> None:
        pass

    def setAutoRepeat(self, *_args: Any) -> None:
        pass

    def setEnabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    def isEnabled(self) -> bool:
        return self._enabled


_ids = itertools.count(1)
_widgets: weakref.WeakValueDictionary[int, QWidget] = weakref.WeakValueDictionary()
_roots: dict[int, Root] = {}


def _native() -> Any:
    import aqt

    return aqt.mw.native if aqt.mw else None


def _post(fn: Callable[[], None]) -> None:
    import aqt

    if aqt.mw:
        aqt.mw.loop.post(fn)


class Root:
    """A widget the shell draws (a dock's), and the changes it hasn't sent
    yet. Changes go out once the current task is done, in the order they
    happened; a widget's properties are sent once, as they are by then."""

    def __init__(self, widget: QWidget) -> None:
        self.widget = widget
        self.seq = 0
        self._ops: list[dict[str, Any]] = []
        self._dirty: dict[int, QWidget] = {}
        self._structure = False
        self._scheduled = False
        _roots[widget._id] = self

    def close(self) -> None:
        _roots.pop(self.widget._id, None)

    def changed(self, widget: QWidget) -> None:
        if widget._id not in self._dirty:
            self._dirty[widget._id] = widget
            self._ops.append({"id": widget._id, "props": None})
        self._schedule()

    def op(self, op: dict[str, Any]) -> None:
        self._ops.append(op)
        self._schedule()

    def structure_changed(self) -> None:
        self._structure = True
        self._schedule()

    def _schedule(self) -> None:
        if not self._scheduled:
            self._scheduled = True
            _post(self.flush)

    def flush(self) -> None:
        self._scheduled = False
        if not (self._ops or self._structure):
            return
        if self._structure:
            # the tree has every widget's properties; evals, focus and the
            # like still apply after it
            ops: list[dict[str, Any]] = [{"tree": self.widget._tree()}]
            ops += [op for op in self._ops if "props" not in op]
        else:
            ops = []
            for op in self._ops:
                if op.get("props", 0) is None:
                    widget = self._dirty.get(op["id"])
                    if widget is None:
                        continue
                    op = {"id": op["id"], "props": widget._props()}
                ops.append(op)
        self._ops.clear()
        self._dirty.clear()
        self._structure = False
        self.seq += 1
        native = _native()
        if native:
            native.notify("widget_update", root=self.widget._id, seq=self.seq, ops=ops)

    def tree(self) -> dict[str, Any]:
        """For a page drawing the root afresh. Web view callbacks already
        sent went to the page it replaces: they get None, as for a page
        that's gone."""
        waiting = {op["callback"] for op in self._ops if "callback" in op}
        for widget in list(_widgets.values()):
            if isinstance(widget, AnkiWebView) and root_of(widget) is self:
                for n in [n for n in widget._callbacks if n not in waiting]:
                    callback = widget._callbacks.pop(n)
                    _post(functools.partial(callback, None))
        return {"seq": self.seq, "tree": self.widget._tree()}

    def shortcuts(self) -> list[dict[str, Any]]:
        """The `QShortcut`s of the root's widgets, which its page fires."""
        return [
            {"id": shortcut._id, "keys": shortcut._keys}
            for shortcut in list(_shortcuts)
            if shortcut._keys and root_of(shortcut) is self
        ]


def root_of(widget: Any) -> Root | None:
    """The root drawing `widget`: its dock's or window's. A window not
    shown has none (a dialog isn't drawn in the window it belongs to)."""
    while widget is not None:
        root = _roots.get(widget._id)
        if root is not None and root.widget is widget:
            return root
        if isinstance(widget, QWidget) and widget._is_window():
            return None
        parent = widget._parent
        widget = parent if isinstance(parent, QWidget) else None
    return None


def handle_event(event: dict[str, Any]) -> Any:
    """A `widget_event` from the shell's page."""
    root = _roots.get(int(event.get("root", 0)))
    if root is None:
        return None
    # typing the page hasn't reported yet comes first, as in Qt, where
    # the widget already has it
    for id, value in (event.get("values") or {}).items():
        widget = _widgets.get(int(id))
        if widget is not None and root_of(widget) is root:
            widget._from_page("text", value)
    op = event.get("op")
    if op == "tree":
        return root.tree()
    if op == "values":
        return None
    widget = _widgets.get(int(event.get("id", 0)))
    if widget is None or root_of(widget) is not root:
        return None
    return widget._from_page(op, event.get("value"), event)


# Widgets
##########################################################################


class QWidget(QObject):
    """A widget. Not shown until it's in a root's tree and not hidden."""

    _kind = "widget"

    def __init__(self, parent: Any = None, *_args: Any, **_kwargs: Any) -> None:
        super().__init__(parent)
        self._id = next(_ids)
        _widgets[self._id] = self
        self._layout: QLayout | None = None
        # shown or hidden explicitly; otherwise, as Qt, a widget is shown
        # with its parent, and one without a parent is hidden
        self._shown: bool | None = None
        self._enabled = True
        self._tooltip = ""
        self._sizes: dict[str, int] = {}
        self._window_size: tuple[int, int] | None = None
        self._geometry = b""
        # a window: whether the shell has it open, and a sync is due
        self._window_open = False
        self._window_sync = False

    # state the page draws

    def _props(self) -> dict[str, Any]:
        props: dict[str, Any] = {"hidden": self.isHidden(), "enabled": self._enabled}
        if self._tooltip:
            props["tip"] = self._tooltip
        if self._sizes:
            props["sizes"] = dict(self._sizes)
        return props

    def _tree(self) -> dict[str, Any]:
        node = {"id": self._id, "type": self._kind, **self._props()}
        if self._layout is not None:
            node["layout"] = self._layout._tree()
        root = _roots.get(self._id)
        if root is not None and root.widget is self:
            node["shortcuts"] = root.shortcuts()
        return node

    def _changed(self) -> None:
        if root := root_of(self):
            root.changed(self)

    def _structure_changed(self) -> None:
        if root := root_of(self):
            root.structure_changed()

    def _op(self, **op: Any) -> None:
        if root := root_of(self):
            root.op({"id": self._id, **op})

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "closed" and self._is_window():
            self._closed_by_user()
        return None

    # windows: a main window or dialog, shown, is a shell window

    def _is_window(self) -> bool:
        return False

    def _show_window(self, visible: bool) -> None:
        if visible == (self._shown is True):
            return
        self._shown = visible
        if visible and root_of(self) is None:
            Root(self)
        # the shell gets where it ends up after the task, so hiding and
        # showing again keeps its window (and page)
        if not self._window_sync:
            self._window_sync = True
            _post(self._sync_window)

    def _sync_window(self) -> None:
        self._window_sync = False
        shown = self._shown is True
        if shown == self._window_open:
            return
        self._window_open = shown
        native = _native()
        if shown:
            width, height = self._window_size or self._default_size()
            if native:
                native.notify(
                    "widget_window",
                    root=self._id,
                    title=self.windowTitle(),
                    width=width,
                    height=height,
                )
        else:
            if root := _roots.get(self._id):
                root.close()
            if native:
                native.notify("widget_window_close", root=self._id)

    def _default_size(self) -> tuple[int, int]:
        return (640, 480)

    def _window_gone(self) -> None:
        """The shell closed the window: it isn't shown, whatever happens."""
        self._window_open = False
        self._shown = False
        if root := _roots.get(self._id):
            root.close()

    def _closed_by_user(self) -> None:
        """The shell's window was closed: as Qt's close button, but the
        window is gone whatever closeEvent decides."""
        self._window_open = False
        self.closeEvent(QCloseEvent())
        self._window_gone()

    def closeEvent(self, event: Any) -> None:
        pass

    def resize(self, *size: Any) -> None:
        if len(size) == 1:
            size = (size[0].width(), size[0].height())
        self._window_size = (int(size[0]), int(size[1]))

    def size(self) -> Any:
        return QSize(*(self._window_size or self._default_size()))

    def saveGeometry(self) -> QByteArray:
        # what restoreGeometry had, for the Qt Anki sharing the config
        return QByteArray(self._geometry)

    def restoreGeometry(self, geometry: Any) -> bool:
        self._geometry = bytes(geometry)
        return False

    def raise_(self) -> None:
        # (before the window is open, opening it brings it to the front)
        native = _native()
        if self._is_window() and self._window_open and native:
            native.notify("widget_window_raise", root=self._id)

    activateWindow = raise_

    def isActiveWindow(self) -> bool:
        return bool(self._is_window() and self._shown)

    # Qt API

    def setParent(self, parent: Any, *_args: Any) -> None:
        self._parent = parent
        self._structure_changed()

    def parentWidget(self) -> Any:
        return self._parent if isinstance(self._parent, QWidget) else None

    def window(self) -> QWidget:
        widget = self
        while isinstance(widget._parent, QWidget) and not widget._is_window():
            widget = widget._parent
        return widget

    def setLayout(self, layout: QLayout) -> None:
        self._layout = layout
        layout._set_owner(self)
        self._structure_changed()

    def layout(self) -> QLayout | None:
        return self._layout

    def setVisible(self, visible: bool) -> None:
        if self._is_window():
            self._show_window(bool(visible))
            return
        if self._shown is bool(visible):
            return
        self._shown = bool(visible)
        if visible and self._parent is None and root_of(self) is None:
            raise NotImplementedError(
                f"{type(self).__name__}: the native shell doesn't show add-on "
                "windows yet"
            )
        self._changed()

    def show(self) -> None:
        self.setVisible(True)

    def hide(self) -> None:
        self.setVisible(False)

    def isHidden(self) -> bool:
        if self._is_window():
            return self._shown is not True
        return self._shown is False or (self._shown is None and self._parent is None)

    def isVisible(self) -> bool:
        widget: Any = self
        while isinstance(widget, QWidget):
            if widget.isHidden():
                return False
            if widget._id in _roots:
                return widget._root_shown()
            widget = widget._parent
        return bool(widget is not None and getattr(widget, "isVisible", bool)())

    def _root_shown(self) -> bool:
        return True

    def setEnabled(self, enabled: bool) -> None:
        if self._enabled != bool(enabled):
            self._enabled = bool(enabled)
            self._changed()

    def setDisabled(self, disabled: bool) -> None:
        self.setEnabled(not disabled)

    def isEnabled(self) -> bool:
        return self._enabled

    def setToolTip(self, tip: str) -> None:
        if self._tooltip != tip:
            self._tooltip = tip
            self._changed()

    def toolTip(self) -> str:
        return self._tooltip

    def _size(self, key: str, value: int) -> None:
        if self._sizes.get(key) != value:
            self._sizes[key] = int(value)
            self._changed()

    def setMinimumHeight(self, h: int) -> None:
        self._size("minHeight", h)

    def setMaximumHeight(self, h: int) -> None:
        self._size("maxHeight", h)

    def setMinimumWidth(self, w: int) -> None:
        self._size("minWidth", w)

    def setMaximumWidth(self, w: int) -> None:
        self._size("maxWidth", w)

    def setFixedHeight(self, h: int) -> None:
        self.setMinimumHeight(h)
        self.setMaximumHeight(h)

    def setFixedWidth(self, w: int) -> None:
        self.setMinimumWidth(w)
        self.setMaximumWidth(w)

    def setMinimumSize(self, w: int, h: int) -> None:
        self.setMinimumWidth(w)
        self.setMinimumHeight(h)

    def setFocus(self, *_args: Any) -> None:
        self._op(focus=True)

    def setSizePolicy(self, *_args: Any) -> None:
        pass

    def setContentsMargins(self, *_args: Any) -> None:
        pass

    def setStyleSheet(self, *_args: Any) -> None:
        pass

    def setFocusPolicy(self, *_args: Any) -> None:
        pass

    def setAttribute(self, *_args: Any) -> None:
        pass

    def setWindowTitle(self, title: str) -> None:
        self._window_title = title
        native = _native()
        if self._is_window() and self._window_open and native:
            native.notify("widget_window_title", root=self._id, title=title)

    def windowTitle(self) -> str:
        return getattr(self, "_window_title", "")

    def close(self) -> bool:
        if self._is_window() and self._shown:
            event = QCloseEvent()
            self.closeEvent(event)
            if not event.isAccepted():
                return False
        self.hide()
        return True

    def update(self) -> None:
        pass

    def adjustSize(self) -> None:
        pass

    def keyPressEvent(self, event: QKeyEvent) -> None:
        event.ignore()


class QFrame(QWidget):
    class Shape(metaclass=_Names):
        pass

    def setFrameShape(self, *_args: Any) -> None:
        pass


class QLabel(QFrame):
    _kind = "label"

    def __init__(self, text: str = "", parent: Any = None, *_args: Any) -> None:
        super().__init__(parent)
        self._text = str(text)
        self._format = Qt.TextFormat.AutoText
        self._wrap = False

    def _props(self) -> dict[str, Any]:
        return {
            **super()._props(),
            "text": self._text,
            "rich": self._is_rich(),
            "wrap": self._wrap,
        }

    def _is_rich(self) -> bool:
        if self._format == Qt.TextFormat.RichText:
            return True
        if self._format != Qt.TextFormat.AutoText:
            return False
        # Qt.mightBeRichText: a tag before the first line break
        first = self._text.split("\n", 1)[0]
        return bool(re.search(r"<[a-zA-Z!/][^>]*>", first))

    def setText(self, text: str) -> None:
        if self._text != str(text):
            self._text = str(text)
            self._changed()

    def text(self) -> str:
        return self._text

    def clear(self) -> None:
        self.setText("")

    def setWordWrap(self, wrap: bool) -> None:
        self._wrap = bool(wrap)
        self._changed()

    def setTextFormat(self, fmt: Qt.TextFormat) -> None:
        self._format = fmt
        self._changed()

    def setAlignment(self, *_args: Any) -> None:
        pass

    def setOpenExternalLinks(self, *_args: Any) -> None:
        pass

    def setTextInteractionFlags(self, *_args: Any) -> None:
        pass


class QAbstractButton(QWidget):
    _kind = "button"

    def __init__(self, text: Any = "", parent: Any = None, *_args: Any) -> None:
        if not isinstance(text, str):
            # (parent) alone
            text, parent = "", text
        super().__init__(parent)
        self._text = text
        self._checkable = False
        self._checked = False
        self._shortcut = ""
        self._menu: Any = None
        self.clicked = Signal(self)
        self.toggled = Signal(self)
        self.pressed = Signal(self)
        self.released = Signal(self)

    def _props(self) -> dict[str, Any]:
        props = {**super()._props(), "text": self._text}
        if self._checkable:
            props["checked"] = self._checked
        if self._shortcut:
            props["shortcut"] = self._shortcut
        if self._menu is not None:
            props["menu"] = self._menu._items()
        return props

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "clicked" and self._enabled:
            self.click()
        elif op == "menu" and self._enabled and self._menu is not None:
            self._menu._trigger(value)
        return None

    def setMenu(self, menu: Any) -> None:
        self._menu = menu
        menu._buttons.append(weakref.ref(self))
        self._changed()

    def menu(self) -> Any:
        return self._menu

    def showMenu(self) -> None:
        pass

    def click(self) -> None:
        if self._checkable:
            self.setChecked(not self._checked)
        self.clicked.emit(self._checked)

    def setText(self, text: str) -> None:
        if self._text != text:
            self._text = text
            self._changed()

    def text(self) -> str:
        return self._text

    def setCheckable(self, checkable: bool) -> None:
        self._checkable = bool(checkable)
        self._changed()

    def isCheckable(self) -> bool:
        return self._checkable

    def setChecked(self, checked: bool) -> None:
        if self._checked != bool(checked):
            self._checked = bool(checked)
            self._changed()
            self.toggled.emit(self._checked)

    def isChecked(self) -> bool:
        return self._checked

    def setIcon(self, *_args: Any) -> None:
        pass

    def setShortcut(self, keys: Any) -> None:
        self._shortcut = _keys(keys)
        self._changed()

    def shortcut(self) -> Any:
        from .qt import QKeySequence

        return QKeySequence(self._shortcut)


class QPushButton(QAbstractButton):
    def setDefault(self, *_args: Any) -> None:
        pass

    def setAutoDefault(self, *_args: Any) -> None:
        pass

    def setFlat(self, *_args: Any) -> None:
        pass


class QToolButton(QAbstractButton):
    _kind = "toolbutton"

    def __init__(self, parent: Any = None, *_args: Any) -> None:
        super().__init__("", parent)
        self._arrow = Qt.ArrowType.NoArrow

    def _props(self) -> dict[str, Any]:
        arrow = {
            Qt.ArrowType.UpArrow: "up",
            Qt.ArrowType.DownArrow: "down",
            Qt.ArrowType.LeftArrow: "left",
            Qt.ArrowType.RightArrow: "right",
        }.get(self._arrow)
        return {**super()._props(), "arrow": arrow}

    def setArrowType(self, arrow: Qt.ArrowType) -> None:
        self._arrow = arrow
        self._changed()

    def setAutoRaise(self, *_args: Any) -> None:
        pass

    def setToolButtonStyle(self, *_args: Any) -> None:
        pass


class QCheckBox(QAbstractButton):
    _kind = "checkbox"

    def __init__(self, text: Any = "", parent: Any = None, *_args: Any) -> None:
        super().__init__(text, parent)
        self._checkable = True
        self.stateChanged = Signal(self)
        self.toggled.connect(
            lambda checked: self.stateChanged.emit(
                int(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            )
        )

    def _props(self) -> dict[str, Any]:
        return {**super()._props(), "checked": self._checked}

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "checked" and self._enabled:
            if bool(value) != self._checked:
                self.setChecked(bool(value))
                self.clicked.emit(self._checked)
        return None

    def checkState(self) -> Qt.CheckState:
        return Qt.CheckState.Checked if self._checked else Qt.CheckState.Unchecked

    def setCheckState(self, state: Any) -> None:
        self.setChecked(int(state) == int(Qt.CheckState.Checked))


class QLineEdit(QWidget):
    _kind = "lineedit"

    def __init__(self, text: Any = "", parent: Any = None, *_args: Any) -> None:
        if not isinstance(text, str):
            text, parent = "", text
        super().__init__(parent)
        self._text = text
        self._placeholder = ""
        self._read_only = False
        self.textChanged = Signal(self)
        self.textEdited = Signal(self)
        self.editingFinished = Signal(self)
        self.returnPressed = Signal(self)
        # an editable QComboBox's, which shows it
        self._combo: QComboBox | None = None

    def _props(self) -> dict[str, Any]:
        return {
            **super()._props(),
            "text": self._text,
            "placeholder": self._placeholder,
            "readOnly": self._read_only,
        }

    def _changed(self) -> None:
        if self._combo is not None:
            self._combo._changed()
        else:
            super()._changed()

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "text" and isinstance(value, str) and value != self._text:
            self._text = value
            self.textEdited.emit(value)
            self.textChanged.emit(value)
        elif op == "finished":
            self.editingFinished.emit()
        elif op == "return":
            self.returnPressed.emit()
            self.editingFinished.emit()
        return None

    def setText(self, text: str) -> None:
        if self._text != text:
            self._text = text
            self._changed()
            self.textChanged.emit(text)

    def text(self) -> str:
        return self._text

    def clear(self) -> None:
        self.setText("")

    def setPlaceholderText(self, text: str) -> None:
        self._placeholder = text
        self._changed()

    def placeholderText(self) -> str:
        return self._placeholder

    def setReadOnly(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        self._changed()

    def setClearButtonEnabled(self, *_args: Any) -> None:
        pass

    def selectAll(self) -> None:
        pass


class QTextCursor(metaclass=_Names):
    class MoveOperation(enum.Enum):
        Start = 1
        End = 11

    def __init__(self, text: str = "", position: int = 0) -> None:
        self._text = text
        self._position = position

    def position(self) -> int:
        return self._position

    def blockNumber(self) -> int:
        return self._text[: self._position].count("\n")

    def atStart(self) -> bool:
        return self._position == 0

    def atEnd(self) -> bool:
        return self._position >= len(self._text)

    def movePosition(self, op: Any, *_args: Any) -> bool:
        if op == QTextCursor.MoveOperation.End:
            self._position = len(self._text)
        elif op == QTextCursor.MoveOperation.Start:
            self._position = 0
        return True


class QTextDocument:
    def __init__(self, text: str) -> None:
        self._text = text

    def blockCount(self) -> int:
        return self._text.count("\n") + 1

    def toPlainText(self) -> str:
        return self._text

    def isEmpty(self) -> bool:
        return not self._text


class QPlainTextEdit(QWidget):
    """Multi-line text. A subclass's keyPressEvent sees the page's Enter,
    and Up/Down on the first/last line; Qt's own handling of those (calling
    the base class) is done by the page."""

    _kind = "textedit"

    def __init__(self, text: Any = "", parent: Any = None, *_args: Any) -> None:
        if not isinstance(text, str):
            text, parent = "", text
        super().__init__(parent)
        self._text = text
        self._placeholder = ""
        self._read_only = False
        self._cursor = len(text)
        self.textChanged = Signal(self)
        # set while the base class handles a page key
        self._default_key = False

    def _props(self) -> dict[str, Any]:
        return {
            **super()._props(),
            "text": self._text,
            "placeholder": self._placeholder,
            "readOnly": self._read_only,
            "keys": type(self).keyPressEvent is not QPlainTextEdit.keyPressEvent,
        }

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "text" and isinstance(value, str):
            self._set_from_page(
                value, len(value) if self._cursor > len(value) else None
            )
        elif op == "key" and isinstance(event, dict):
            text = event.get("text")
            if isinstance(text, str):
                self._set_from_page(text, int(event.get("cursor") or 0))
            self._default_key = False
            self.keyPressEvent(key_event(event))
            return {"default": self._default_key}
        return None

    def _set_from_page(self, text: str, cursor: int | None) -> None:
        if cursor is not None:
            self._cursor = max(0, min(cursor, len(text)))
        if text != self._text:
            self._text = text
            self.textChanged.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # the page does what Qt would (a new line, moving the caret)
        self._default_key = True

    def setPlainText(self, text: str) -> None:
        self._text = text
        self._cursor = len(text)
        self._changed()
        self.textChanged.emit()

    setText = setPlainText

    def appendPlainText(self, text: str) -> None:
        self.setPlainText(f"{self._text}\n{text}" if self._text else text)

    def toPlainText(self) -> str:
        return self._text

    def clear(self) -> None:
        self.setPlainText("")

    def setPlaceholderText(self, text: str) -> None:
        self._placeholder = text
        self._changed()

    def setReadOnly(self, read_only: bool) -> None:
        self._read_only = bool(read_only)
        self._changed()

    def textCursor(self) -> QTextCursor:
        return QTextCursor(self._text, self._cursor)

    def setTextCursor(self, cursor: QTextCursor) -> None:
        self._cursor = cursor.position()
        self._op(cursor=self._cursor)

    def moveCursor(self, op: Any, *_args: Any) -> None:
        cursor = self.textCursor()
        cursor.movePosition(op)
        self.setTextCursor(cursor)

    def document(self) -> QTextDocument:
        return QTextDocument(self._text)

    def setTabChangesFocus(self, *_args: Any) -> None:
        pass

    def setLineWrapMode(self, *_args: Any) -> None:
        pass


QTextEdit = QPlainTextEdit


class QComboBox(QWidget):
    _kind = "combo"

    class InsertPolicy(enum.Enum):
        NoInsert = 0
        InsertAtTop = 1
        InsertAtCurrent = 2
        InsertAtBottom = 3

    class SizeAdjustPolicy(metaclass=_Names):
        pass

    def __init__(self, parent: Any = None, *_args: Any) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, Any]] = []
        self._index = -1
        self._line_edit: QLineEdit | None = None
        self.currentIndexChanged = Signal(self)
        self.currentTextChanged = Signal(self)
        self.activated = Signal(self)
        self.textActivated = Signal(self)
        self.editTextChanged = Signal(self)

    def _props(self) -> dict[str, Any]:
        props = {
            **super()._props(),
            "items": [text for text, _data in self._items],
            "index": self._index,
        }
        if self._line_edit is not None:
            props["editable"] = True
            props["text"] = self._line_edit.text()
            props["placeholder"] = self._line_edit.placeholderText()
        return props

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "index" and isinstance(value, int) and 0 <= value < len(self._items):
            self.setCurrentIndex(value)
            self.activated.emit(value)
            self.textActivated.emit(self._items[value][0])
        elif op == "text" and self._line_edit is not None and isinstance(value, str):
            if value != self._line_edit.text():
                self._line_edit._text = value
                self.editTextChanged.emit(value)
                self.currentTextChanged.emit(value)
        return None

    def _set_index(self, index: int) -> None:
        if index == self._index:
            return
        self._index = index
        if self._line_edit is not None:
            self._line_edit._text = self.itemText(index)
        self._changed()
        self.currentIndexChanged.emit(index)
        self.currentTextChanged.emit(self.currentText())

    def addItem(self, text: Any, data: Any = None) -> None:
        if not isinstance(text, str):
            # (icon, text, data)
            text, data = data, None
        self._items.append((str(text), data))
        self._changed()
        if self._index < 0:
            self._set_index(0)

    def addItems(self, texts: Iterable[str]) -> None:
        for text in texts:
            self.addItem(text)

    def insertItem(self, index: int, text: str, data: Any = None) -> None:
        index = max(0, min(index, len(self._items)))
        self._items.insert(index, (str(text), data))
        if self._index >= index:
            self._index += 1
        self._changed()
        if self._index < 0:
            self._set_index(0)

    def removeItem(self, index: int) -> None:
        if not 0 <= index < len(self._items):
            return
        del self._items[index]
        self._changed()
        if index < self._index:
            self._index -= 1
        elif index == self._index:
            self._index = -1
            self._set_index(min(index, len(self._items) - 1))

    def clear(self) -> None:
        self._items.clear()
        self._changed()
        self._set_index(-1)

    def count(self) -> int:
        return len(self._items)

    def currentIndex(self) -> int:
        return self._index

    def setCurrentIndex(self, index: int) -> None:
        if -1 <= index < len(self._items):
            self._set_index(index)

    def currentText(self) -> str:
        if self._line_edit is not None:
            return self._line_edit.text()
        return self.itemText(self._index)

    def setCurrentText(self, text: str) -> None:
        index = self.findText(text)
        if index >= 0:
            self.setCurrentIndex(index)
        elif self._line_edit is not None:
            self.setEditText(text)

    def currentData(self, *_role: Any) -> Any:
        return self.itemData(self._index)

    def itemText(self, index: int) -> str:
        return self._items[index][0] if 0 <= index < len(self._items) else ""

    def itemData(self, index: int, *_role: Any) -> Any:
        return self._items[index][1] if 0 <= index < len(self._items) else None

    def setItemText(self, index: int, text: str) -> None:
        if 0 <= index < len(self._items):
            self._items[index] = (text, self._items[index][1])
            self._changed()

    def setItemData(self, index: int, data: Any, *_role: Any) -> None:
        if 0 <= index < len(self._items):
            self._items[index] = (self._items[index][0], data)

    def findData(self, data: Any, *_args: Any) -> int:
        for i, (_text, item) in enumerate(self._items):
            if item == data:
                return i
        return -1

    def findText(self, text: str, *_args: Any) -> int:
        for i, (item, _data) in enumerate(self._items):
            if item == text:
                return i
        return -1

    def setEditable(self, editable: bool) -> None:
        if editable and self._line_edit is None:
            self._line_edit = QLineEdit(self.currentText(), self)
            self._line_edit._combo = self
            self._line_edit.textChanged.connect(self.editTextChanged.emit)
        elif not editable:
            self._line_edit = None
        self._changed()

    def isEditable(self) -> bool:
        return self._line_edit is not None

    def lineEdit(self) -> QLineEdit | None:
        return self._line_edit

    def setEditText(self, text: str) -> None:
        if self._line_edit is not None and self._line_edit.text() != text:
            self._line_edit.setText(text)
            self.currentTextChanged.emit(text)

    def setInsertPolicy(self, *_args: Any) -> None:
        pass

    def setSizeAdjustPolicy(self, *_args: Any) -> None:
        pass

    def setMinimumContentsLength(self, *_args: Any) -> None:
        pass


class QSplitter(QFrame):
    _kind = "splitter"

    def __init__(self, orientation: Any = None, parent: Any = None) -> None:
        if isinstance(orientation, QWidget) or (
            orientation is not None and not isinstance(orientation, Qt.Orientation)
        ):
            orientation, parent = None, orientation
        super().__init__(parent)
        self._orientation = orientation or Qt.Orientation.Horizontal
        self._children: list[QWidget] = []
        self._split_sizes: list[int] = []
        self.splitterMoved = Signal(self)

    def _props(self) -> dict[str, Any]:
        sizes = list(self._split_sizes)
        sizes += [0] * (len(self._children) - len(sizes))
        return {
            **super()._props(),
            "vertical": self._orientation == Qt.Orientation.Vertical,
            "split": sizes[: len(self._children)],
        }

    def _tree(self) -> dict[str, Any]:
        node = super()._tree()
        node["children"] = [child._tree() for child in self._children]
        return node

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "split" and isinstance(value, list) and len(value) >= 2:
            self._split_sizes = [max(0, int(v)) for v in value]
            self.splitterMoved.emit(self._split_sizes[0], 1)
        return None

    def addWidget(self, widget: QWidget) -> None:
        self._children.append(widget)
        widget._parent = self
        self._structure_changed()

    def insertWidget(self, index: int, widget: QWidget) -> None:
        self._children.insert(index, widget)
        widget._parent = self
        self._structure_changed()

    def widget(self, index: int) -> QWidget | None:
        return self._children[index] if 0 <= index < len(self._children) else None

    def count(self) -> int:
        return len(self._children)

    def setSizes(self, sizes: list[int]) -> None:
        self._split_sizes = [int(s) for s in sizes]
        self._changed()

    def sizes(self) -> list[int]:
        sizes = list(self._split_sizes)
        return sizes + [0] * (len(self._children) - len(sizes))

    def orientation(self) -> Qt.Orientation:
        return self._orientation

    def setStretchFactor(self, *_args: Any) -> None:
        pass

    def setChildrenCollapsible(self, *_args: Any) -> None:
        pass

    def setHandleWidth(self, *_args: Any) -> None:
        pass


class QMainWindow(QWidget):
    """A window of its own, shown in the shell, drawing its central widget
    (no menus, toolbars or docks; the editor windows take docks)."""

    _kind = "window"

    def __init__(self, parent: Any = None, *_args: Any, **_kwargs: Any) -> None:
        super().__init__(parent)
        self._central: QWidget | None = None

    def _is_window(self) -> bool:
        return True

    def _default_size(self) -> tuple[int, int]:
        return (900, 640)

    def _tree(self) -> dict[str, Any]:
        node = super()._tree()
        node["child"] = self._central._tree() if self._central else None
        return node

    def addDockWidget(self, area: Any, dock: QDockWidget, *_args: Any) -> None:
        raise NotImplementedError(
            "QMainWindow.addDockWidget: only the editor windows take docks"
        )

    def setCentralWidget(self, widget: QWidget) -> None:
        self._central = widget
        widget._parent = self
        self._structure_changed()

    def centralWidget(self) -> QWidget | None:
        return self._central

    def statusBar(self) -> Any:
        raise NotImplementedError("QMainWindow.statusBar")


class QDockWidget(QWidget):
    """A dock, drawn as the side panel of the window it's added to."""

    _kind = "dock"

    class DockWidgetFeature(metaclass=_Names):
        pass

    def __init__(self, title: Any = "", parent: Any = None, *_args: Any) -> None:
        if not isinstance(title, str):
            title, parent = "", title
        super().__init__(parent)
        self._title = title
        self._child: QWidget | None = None
        self._floating = False
        # set by the window it's added to
        self._on_shown: Callable[[QDockWidget, bool], None] | None = None
        self.visibilityChanged = Signal(self)

    def _props(self) -> dict[str, Any]:
        return {**super()._props(), "title": self._title}

    def _tree(self) -> dict[str, Any]:
        node = super()._tree()
        node["child"] = self._child._tree() if self._child else None
        return node

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "close":
            self.hide()
        return None

    def _root_shown(self) -> bool:
        parent = self._parent
        return bool(parent is None or getattr(parent, "isVisible", bool)())

    def setWidget(self, widget: QWidget) -> None:
        self._child = widget
        widget._parent = self
        self._structure_changed()

    def widget(self) -> QWidget | None:
        return self._child

    def setVisible(self, visible: bool) -> None:
        if self.isHidden() == (not visible):
            return
        self._shown = bool(visible)
        self._changed()
        if self._on_shown is not None:
            self._on_shown(self, visible)
        self.visibilityChanged.emit(bool(visible))

    def toggleViewAction(self) -> Any:
        raise NotImplementedError("QDockWidget.toggleViewAction")

    def setFloating(self, floating: bool) -> None:
        self._floating = bool(floating)

    def isFloating(self) -> bool:
        return self._floating

    def setAllowedAreas(self, *_args: Any) -> None:
        pass

    def setFeatures(self, *_args: Any) -> None:
        pass

    def setTitleBarWidget(self, *_args: Any) -> None:
        pass


class QDialog(QWidget):
    """A window of its own. `exec` shows it and runs the host's loop until
    it's answered, so the shell's other events (its own buttons) go on."""

    class DialogCode(enum.IntEnum):
        Rejected = 0
        Accepted = 1

    def __init__(self, parent: Any = None, *_args: Any, **_kwargs: Any) -> None:
        super().__init__(parent)
        self._result = 0
        self._running = False
        self.accepted = Signal(self)
        self.rejected = Signal(self)
        self.finished = Signal(self)

    def _is_window(self) -> bool:
        return True

    def _default_size(self) -> tuple[int, int]:
        return (460, 200)

    def _tree(self) -> dict[str, Any]:
        node = super()._tree()
        if "shortcuts" in node:
            # Escape rejects, as in Qt
            node["shortcuts"].append({"id": self._id, "keys": "Escape"})
        return node

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "shortcut" and self._shown:
            self.reject()
            return None
        return super()._from_page(op, value, event)

    def _closed_by_user(self) -> None:
        # Qt's close button rejects
        self._window_open = False
        if self._shown:
            self.reject()
        self._window_gone()

    def closeEvent(self, event: Any) -> None:
        pass

    def exec(self) -> int:
        import aqt

        self._result = 0
        self._running = True
        self.show()
        try:
            if aqt.mw:
                aqt.mw.loop.run_until(lambda: not self._running)
        finally:
            self._running = False
        return self._result

    exec_ = exec

    def open(self) -> None:
        self.show()

    def done(self, result: int) -> None:
        self._result = int(result)
        self._running = False
        self.hide()
        self.finished.emit(self._result)
        if self._result == QDialog.DialogCode.Accepted:
            self.accepted.emit()
        else:
            self.rejected.emit()

    def accept(self) -> None:
        self.done(QDialog.DialogCode.Accepted)

    def reject(self) -> None:
        self.done(QDialog.DialogCode.Rejected)

    def result(self) -> int:
        return self._result

    def setResult(self, result: int) -> None:
        self._result = int(result)

    def setModal(self, *_args: Any) -> None:
        pass

    def setWindowModality(self, *_args: Any) -> None:
        pass

    def setSizeGripEnabled(self, *_args: Any) -> None:
        pass


class QFileDialog(metaclass=_Names):
    """A folder picker in the shell (the add-on's call waits for it, as in
    Qt); other file dialogs aren't supported."""

    class Option(enum.IntFlag):
        ShowDirsOnly = 1
        DontResolveSymlinks = 2

    def __init__(self, *_args: Any) -> None:
        raise NotImplementedError("QFileDialog: only getExistingDirectory")

    @staticmethod
    def getExistingDirectory(
        parent: Any = None, caption: str = "", directory: str = "", *_args: Any
    ) -> str:
        native = _native()
        if native is None:
            return ""
        out = native.request("choose_folder", title=caption, folder=directory)
        return out if isinstance(out, str) else ""


class QDateTime:
    """A point in time (local, as the page shows it)."""

    def __init__(self, secs: float = 0.0) -> None:
        self._secs = float(secs)

    @staticmethod
    def fromSecsSinceEpoch(secs: int, *_args: Any) -> QDateTime:
        return QDateTime(secs)

    @staticmethod
    def currentDateTime() -> QDateTime:
        return QDateTime(datetime.datetime.now().timestamp())

    @staticmethod
    def currentSecsSinceEpoch() -> int:
        return int(datetime.datetime.now().timestamp())

    def toSecsSinceEpoch(self) -> int:
        return int(self._secs)

    def addSecs(self, secs: int) -> QDateTime:
        return QDateTime(self._secs + secs)

    def secsTo(self, other: QDateTime) -> int:
        return int(other._secs - self._secs)

    def isValid(self) -> bool:
        return True

    def __lt__(self, other: QDateTime) -> bool:
        return self._secs < other._secs

    def __eq__(self, other: object) -> bool:
        return isinstance(other, QDateTime) and self._secs == other._secs

    def __hash__(self) -> int:
        return hash(self._secs)


class QDateTimeEdit(QWidget):
    """A date and time, picked to the minute (the page's datetime-local)."""

    _kind = "datetime"

    def __init__(self, value: Any = None, parent: Any = None, *_args: Any) -> None:
        if isinstance(value, QWidget):
            value, parent = None, value
        super().__init__(parent)
        self._value = value if isinstance(value, QDateTime) else QDateTime()
        self._minimum: QDateTime | None = None
        self.dateTimeChanged = Signal(self)

    @staticmethod
    def _local(secs: float) -> str:
        return datetime.datetime.fromtimestamp(secs).strftime("%Y-%m-%dT%H:%M")

    def _props(self) -> dict[str, Any]:
        props = {**super()._props(), "value": self._local(self._value._secs)}
        if self._minimum is not None:
            props["min"] = self._local(self._minimum._secs)
        return props

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "datetime" and isinstance(value, str):
            try:
                secs = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M").timestamp()
            except ValueError:
                return None
            if secs != self._value._secs:
                self._value = QDateTime(secs)
                self.dateTimeChanged.emit(self._value)
        return None

    def dateTime(self) -> QDateTime:
        return self._value

    def setDateTime(self, value: QDateTime) -> None:
        if value != self._value:
            self._value = value
            self._changed()
            self.dateTimeChanged.emit(value)

    def setMinimumDateTime(self, value: QDateTime) -> None:
        self._minimum = value
        self._changed()

    def setCalendarPopup(self, *_args: Any) -> None:
        pass

    def setDisplayFormat(self, *_args: Any) -> None:
        pass


class QDialogButtonBox(QWidget):
    """A dialog's buttons, as a row (macOS order: rejecting ones before
    accepting ones, on the right)."""

    class StandardButton(enum.IntFlag):
        NoButton = 0
        Ok = 0x00000400
        Save = 0x00000800
        Open = 0x00002000
        Yes = 0x00004000
        No = 0x00010000
        Abort = 0x00040000
        Retry = 0x00080000
        Ignore = 0x00100000
        Close = 0x00200000
        Cancel = 0x00400000
        Discard = 0x00800000
        Help = 0x01000000
        Apply = 0x02000000
        Reset = 0x04000000

    class ButtonRole(enum.IntEnum):
        InvalidRole = -1
        AcceptRole = 0
        RejectRole = 1
        DestructiveRole = 2
        ActionRole = 3
        HelpRole = 4
        YesRole = 5
        NoRole = 6
        ResetRole = 7
        ApplyRole = 8

    _STANDARD = {
        StandardButton.Ok: ("OK", ButtonRole.AcceptRole),
        StandardButton.Save: ("Save", ButtonRole.AcceptRole),
        StandardButton.Open: ("Open", ButtonRole.AcceptRole),
        StandardButton.Yes: ("Yes", ButtonRole.YesRole),
        StandardButton.No: ("No", ButtonRole.NoRole),
        StandardButton.Abort: ("Abort", ButtonRole.RejectRole),
        StandardButton.Retry: ("Retry", ButtonRole.AcceptRole),
        StandardButton.Ignore: ("Ignore", ButtonRole.AcceptRole),
        StandardButton.Close: ("Close", ButtonRole.RejectRole),
        StandardButton.Cancel: ("Cancel", ButtonRole.RejectRole),
        StandardButton.Discard: ("Don't Save", ButtonRole.DestructiveRole),
        StandardButton.Help: ("Help", ButtonRole.HelpRole),
        StandardButton.Apply: ("Apply", ButtonRole.ApplyRole),
        StandardButton.Reset: ("Reset", ButtonRole.ResetRole),
    }
    _ACCEPTING = (ButtonRole.AcceptRole, ButtonRole.YesRole)
    _REJECTING = (ButtonRole.RejectRole, ButtonRole.NoRole)

    def __init__(self, buttons: Any = None, *args: Any) -> None:
        parent = next((a for a in (buttons, *args) if isinstance(a, QWidget)), None)
        super().__init__(parent)
        self._buttons: list[tuple[QPushButton, Any, Any]] = []
        self.accepted = Signal(self)
        self.rejected = Signal(self)
        self.clicked = Signal(self)
        self.helpRequested = Signal(self)
        self.setLayout(QHBoxLayout())
        if isinstance(buttons, int) and not isinstance(buttons, _Placeholder):
            self.setStandardButtons(buttons)

    def _relayout(self) -> None:
        layout = QHBoxLayout()
        layout.addStretch(1)
        rank = {role: 1 for role in self._REJECTING} | {
            role: 2 for role in self._ACCEPTING
        }
        # sorted is stable: added order within each group
        for button, _role, _standard in sorted(
            self._buttons, key=lambda b: rank.get(b[1], 0)
        ):
            layout.addWidget(button)
        self.setLayout(layout)

    def _clicked(self, button: QPushButton, role: Any) -> None:
        self.clicked.emit(button)
        if role in self._ACCEPTING:
            self.accepted.emit()
        elif role in self._REJECTING:
            self.rejected.emit()
        elif role == self.ButtonRole.HelpRole:
            self.helpRequested.emit()

    def addButton(self, button: Any, role: Any = None) -> QPushButton | None:
        standard = None
        if isinstance(button, self.StandardButton) or (
            isinstance(button, int) and not isinstance(button, _Placeholder)
        ):
            standard = self.StandardButton(button)
            text, role = self._STANDARD.get(standard, ("", self.ButtonRole.ActionRole))
            button = QPushButton(text)
        elif isinstance(button, str):
            button = QPushButton(button)
        if role is None:
            role = self.ButtonRole.ActionRole
        button.clicked.connect(
            lambda _checked=False, b=button, r=role: self._clicked(b, r)
        )
        self._buttons.append((button, role, standard))
        self._relayout()
        return button

    def setStandardButtons(self, buttons: int) -> None:
        for standard in self._STANDARD:
            if int(buttons) & int(standard):
                self.addButton(standard)

    def button(self, standard: Any) -> QPushButton | None:
        for button, _role, s in self._buttons:
            if s is not None and int(s) == int(standard):
                return button
        return None

    def buttons(self) -> list[QPushButton]:
        return [button for button, _role, _s in self._buttons]

    def buttonRole(self, button: QPushButton) -> Any:
        for b, role, _s in self._buttons:
            if b is button:
                return role
        return self.ButtonRole.InvalidRole

    def setOrientation(self, *_args: Any) -> None:
        pass

    def setCenterButtons(self, *_args: Any) -> None:
        pass


class QInputDialog(metaclass=_Names):
    """`getText` and `getItem`, as a dialog in the shell."""

    @staticmethod
    def getText(
        parent: Any,
        title: str,
        label: str,
        echo: Any = None,
        text: str = "",
        *_args: Any,
        **_kwargs: Any,
    ) -> tuple[str, bool]:
        dialog = QDialog(parent)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        dialog.setLayout(layout)
        prompt = QLabel(label)
        prompt.setWordWrap(True)
        layout.addWidget(prompt)
        edit = QLineEdit(text)
        layout.addWidget(edit)
        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        edit.returnPressed.connect(dialog.accept)
        layout.addWidget(buttons)
        dialog.resize(420, 160)
        ok = dialog.exec() == QDialog.DialogCode.Accepted
        return edit.text(), ok

    @staticmethod
    def getItem(
        parent: Any,
        title: str,
        label: str,
        items: list[str],
        current: int = 0,
        editable: bool = True,
        *_args: Any,
        **_kwargs: Any,
    ) -> tuple[str, bool]:
        dialog = QDialog(parent)
        dialog.setWindowTitle(title)
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)
        dialog.setLayout(layout)
        layout.addWidget(QLabel(label))
        combo = QComboBox()
        combo.addItems(items)
        combo.setCurrentIndex(current)
        combo.setEditable(editable)
        layout.addWidget(combo)
        layout.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.resize(420, 160)
        ok = dialog.exec() == QDialog.DialogCode.Accepted
        return combo.currentText(), ok


# Tables
##########################################################################


class QAbstractItemView(QWidget):
    class SelectionBehavior(metaclass=_Names):
        pass

    class SelectionMode(metaclass=_Names):
        pass

    class EditTrigger(metaclass=_Names):
        pass

    class ScrollHint(metaclass=_Names):
        pass

    class ScrollMode(metaclass=_Names):
        pass


class QHeaderView(QObject):
    class ResizeMode(metaclass=_Names):
        pass

    def __init__(self, table: QTableWidget, prop: str) -> None:
        super().__init__(table)
        self._table = table
        self._prop = prop
        self._visible = True

    def setVisible(self, visible: bool) -> None:
        if self._visible != bool(visible):
            self._visible = bool(visible)
            self._table._changed()

    def hide(self) -> None:
        self.setVisible(False)

    def show(self) -> None:
        self.setVisible(True)

    def isHidden(self) -> bool:
        return not self._visible

    def resizeSection(self, column: int, width: int) -> None:
        self._table.setColumnWidth(column, width)

    def sectionSize(self, column: int) -> int:
        return self._table.columnWidth(column)

    def setStretchLastSection(self, *_args: Any) -> None:
        pass

    def setSectionResizeMode(self, *_args: Any) -> None:
        pass

    def setDefaultSectionSize(self, *_args: Any) -> None:
        pass

    def setHighlightSections(self, *_args: Any) -> None:
        pass

    def setSectionsClickable(self, *_args: Any) -> None:
        pass

    def setMinimumSectionSize(self, *_args: Any) -> None:
        pass


class QTableWidgetItem:
    def __init__(self, text: Any = "", *_args: Any) -> None:
        self._text = text if isinstance(text, str) else ""
        self._tip = ""
        self._data: dict[Any, Any] = {}
        self._table: QTableWidget | None = None

    def _changed(self) -> None:
        if self._table is not None:
            self._table._changed()

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text
        self._changed()

    def toolTip(self) -> str:
        return self._tip

    def setToolTip(self, tip: str) -> None:
        self._tip = tip
        self._changed()

    def setData(self, role: Any, value: Any) -> None:
        self._data[role] = value

    def data(self, role: Any) -> Any:
        return self._data.get(role)

    def tableWidget(self) -> QTableWidget | None:
        return self._table

    def row(self) -> int:
        table = self._table
        if table is None:
            return -1
        for r, cells in enumerate(table._cells):
            if self in cells:
                return r
        return -1

    def setFlags(self, *_args: Any) -> None:
        pass

    def flags(self) -> Any:
        return Qt.ItemFlag.ItemIsEnabled

    def setTextAlignment(self, *_args: Any) -> None:
        pass

    def setForeground(self, *_args: Any) -> None:
        pass

    def setBackground(self, *_args: Any) -> None:
        pass

    def setFont(self, *_args: Any) -> None:
        pass

    def setIcon(self, *_args: Any) -> None:
        pass


class QTableWidget(QAbstractItemView):
    """A table of text, selected a row at a time (the page's list)."""

    _kind = "table"

    def __init__(self, rows: Any = 0, columns: int = 0, parent: Any = None) -> None:
        if not isinstance(rows, int):
            rows, parent = 0, rows
        super().__init__(parent)
        self._columns = int(columns)
        self._headers: list[str] = []
        self._cells: list[list[QTableWidgetItem | None]] = []
        self._widths: dict[int, int] = {}
        self._current = -1
        self._selected = -1
        self._horizontal = QHeaderView(self, "hheader")
        self._vertical = QHeaderView(self, "vheader")
        self.itemSelectionChanged = Signal(self)
        self.currentCellChanged = Signal(self)
        self.currentItemChanged = Signal(self)
        self.cellClicked = Signal(self)
        self.cellDoubleClicked = Signal(self)
        self.cellActivated = Signal(self)
        self.itemClicked = Signal(self)
        self.itemDoubleClicked = Signal(self)
        self.itemActivated = Signal(self)
        self.setRowCount(int(rows))

    def _props(self) -> dict[str, Any]:
        return {
            **super()._props(),
            "headers": list(self._headers),
            "rows": [
                [item.text() if item else "" for item in cells] for cells in self._cells
            ],
            "tips": [
                [item.toolTip() if item else "" for item in cells]
                for cells in self._cells
            ],
            "widths": {str(c): w for c, w in self._widths.items()},
            "selected": self._selected,
            "hheader": self._horizontal._visible,
            "vheader": self._vertical._visible,
        }

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if not isinstance(value, int) or not 0 <= value < len(self._cells):
            return None
        column = int((event or {}).get("column") or 0)
        if op == "row":
            self.selectRow(value)
            self.cellClicked.emit(value, column)
            if item := self.item(value, column):
                self.itemClicked.emit(item)
        elif op == "activate":
            self.selectRow(value)
            self.cellDoubleClicked.emit(value, column)
            self.cellActivated.emit(value, column)
            if item := self.item(value, column):
                self.itemDoubleClicked.emit(item)
                self.itemActivated.emit(item)
        return None

    def _set_current(self, row: int, selected: int) -> None:
        previous = self._current
        changed = selected != self._selected
        self._current = row
        self._selected = selected
        self._changed()
        if row != previous:
            self.currentCellChanged.emit(row, 0, previous, 0)
            self.currentItemChanged.emit(self.item(row, 0), self.item(previous, 0))
        if changed:
            self.itemSelectionChanged.emit()

    # rows and columns

    def setRowCount(self, rows: int) -> None:
        rows = max(0, int(rows))
        del self._cells[rows:]
        while len(self._cells) < rows:
            self._cells.append([None] * self._columns)
        self._changed()
        if self._current >= rows or self._selected >= rows:
            self._set_current(
                self._current if self._current < rows else -1,
                self._selected if self._selected < rows else -1,
            )

    def rowCount(self) -> int:
        return len(self._cells)

    def setColumnCount(self, columns: int) -> None:
        self._columns = max(0, int(columns))
        for cells in self._cells:
            del cells[self._columns :]
            cells.extend([None] * (self._columns - len(cells)))
        self._changed()

    def columnCount(self) -> int:
        return self._columns

    def insertRow(self, row: int) -> None:
        row = max(0, min(int(row), len(self._cells)))
        self._cells.insert(row, [None] * self._columns)
        if self._current >= row:
            self._current += 1
        if self._selected >= row:
            self._selected += 1
        self._changed()

    def removeRow(self, row: int) -> None:
        if 0 <= row < len(self._cells):
            del self._cells[row]
            current = self._current - (self._current > row)
            selected = self._selected - (self._selected > row)
            self._set_current(
                -1 if self._current == row else current,
                -1 if self._selected == row else selected,
            )

    def setHorizontalHeaderLabels(self, labels: Iterable[str]) -> None:
        self._headers = [str(label) for label in labels]
        self._changed()

    def setItem(self, row: int, column: int, item: QTableWidgetItem) -> None:
        if 0 <= row < len(self._cells) and 0 <= column < self._columns:
            item._table = self
            self._cells[row][column] = item
            self._changed()

    def item(self, row: int, column: int) -> QTableWidgetItem | None:
        if 0 <= row < len(self._cells) and 0 <= column < self._columns:
            return self._cells[row][column]
        return None

    def takeItem(self, row: int, column: int) -> QTableWidgetItem | None:
        item = self.item(row, column)
        if item is not None:
            self._cells[row][column] = None
            item._table = None
            self._changed()
        return item

    def clear(self) -> None:
        self._headers = []
        self.clearContents()

    def clearContents(self) -> None:
        for cells in self._cells:
            cells[:] = [None] * self._columns
        self._changed()

    # selection

    def currentRow(self) -> int:
        return self._current

    def currentColumn(self) -> int:
        return 0 if self._current >= 0 else -1

    def currentItem(self) -> QTableWidgetItem | None:
        return self.item(self._current, 0)

    def setCurrentCell(self, row: int, _column: int = 0, *_args: Any) -> None:
        self.selectRow(row)

    def selectRow(self, row: int) -> None:
        if 0 <= row < len(self._cells):
            self._set_current(row, row)

    def clearSelection(self) -> None:
        if self._selected != -1:
            self._set_current(self._current, -1)

    def selectedItems(self) -> list[QTableWidgetItem]:
        if self._selected < 0:
            return []
        return [item for item in self._cells[self._selected] if item is not None]

    def selectedRows(self) -> list[int]:
        return [self._selected] if self._selected >= 0 else []

    # looks

    def setColumnWidth(self, column: int, width: int) -> None:
        self._widths[int(column)] = int(width)
        self._changed()

    def columnWidth(self, column: int) -> int:
        return self._widths.get(int(column), 100)

    def horizontalHeader(self) -> QHeaderView:
        return self._horizontal

    def verticalHeader(self) -> QHeaderView:
        return self._vertical

    def setSelectionBehavior(self, *_args: Any) -> None:
        pass

    def setSelectionMode(self, *_args: Any) -> None:
        pass

    def setEditTriggers(self, *_args: Any) -> None:
        pass

    def setWordWrap(self, *_args: Any) -> None:
        pass

    def setAlternatingRowColors(self, *_args: Any) -> None:
        pass

    def setSortingEnabled(self, *_args: Any) -> None:
        pass

    def setShowGrid(self, *_args: Any) -> None:
        pass

    def setVerticalScrollMode(self, *_args: Any) -> None:
        pass

    def setHorizontalScrollMode(self, *_args: Any) -> None:
        pass

    def resizeColumnsToContents(self) -> None:
        pass

    def resizeRowsToContents(self) -> None:
        pass

    def scrollToItem(self, *_args: Any) -> None:
        pass

    def scrollToTop(self) -> None:
        pass


# Layouts
##########################################################################


class QLayout(QObject):
    _kind = "v"

    def __init__(self, parent: Any = None) -> None:
        super().__init__(None)
        self._owner: QWidget | None = None
        self._margins = [0, 0, 0, 0]
        self._spacing: int | None = None
        if isinstance(parent, QWidget):
            parent.setLayout(self)

    def _set_owner(self, owner: QWidget) -> None:
        self._owner = owner
        for widget in self._widgets():
            widget._parent = owner
        for layout in self._layouts():
            layout._set_owner(owner)

    def _widgets(self) -> list[QWidget]:
        return []

    def _layouts(self) -> list[QLayout]:
        return []

    def _adopt(self, widget: QWidget) -> None:
        if self._owner is not None:
            widget._parent = self._owner
        self._changed()

    def _changed(self) -> None:
        if self._owner is not None:
            self._owner._structure_changed()

    def _base(self) -> dict[str, Any]:
        node: dict[str, Any] = {"kind": self._kind, "margins": self._margins}
        if self._spacing is not None:
            node["spacing"] = self._spacing
        return node

    def setContentsMargins(self, left: int, top: int, right: int, bottom: int) -> None:
        self._margins = [int(left), int(top), int(right), int(bottom)]
        self._changed()

    def setSpacing(self, spacing: int) -> None:
        self._spacing = int(spacing)
        self._changed()

    def setAlignment(self, *_args: Any) -> None:
        pass

    def setSizeConstraint(self, *_args: Any) -> None:
        pass


class QBoxLayout(QLayout):
    def __init__(self, parent: Any = None) -> None:
        self._items: list[tuple[str, Any, int]] = []
        super().__init__(parent)

    def _widgets(self) -> list[QWidget]:
        return [item for kind, item, _ in self._items if kind == "w"]

    def _layouts(self) -> list[QLayout]:
        return [item for kind, item, _ in self._items if kind == "l"]

    def _tree(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for kind, item, stretch in self._items:
            if kind == "w":
                items.append({"w": item._tree(), "stretch": stretch})
            elif kind == "l":
                items.append({"l": item._tree(), "stretch": stretch})
            elif kind == "stretch":
                items.append({"stretch": stretch})
            else:
                items.append({"space": stretch})
        return {**self._base(), "items": items}

    def addWidget(self, widget: QWidget, stretch: int = 0, *_args: Any) -> None:
        self._items.append(("w", widget, int(stretch)))
        self._adopt(widget)

    def insertWidget(self, index: int, widget: QWidget, stretch: int = 0) -> None:
        self._items.insert(index, ("w", widget, int(stretch)))
        self._adopt(widget)

    def addLayout(self, layout: QLayout, stretch: int = 0) -> None:
        self._items.append(("l", layout, int(stretch)))
        if self._owner is not None:
            layout._set_owner(self._owner)
        self._changed()

    def addStretch(self, stretch: int = 0) -> None:
        self._items.append(("stretch", None, int(stretch) or 1))
        self._changed()

    def addSpacing(self, size: int) -> None:
        self._items.append(("space", None, int(size)))
        self._changed()

    def removeWidget(self, widget: QWidget) -> None:
        self._items = [i for i in self._items if i[1] is not widget]
        self._changed()

    def count(self) -> int:
        return len(self._items)


class QVBoxLayout(QBoxLayout):
    _kind = "v"


class QHBoxLayout(QBoxLayout):
    _kind = "h"


class QFormLayout(QLayout):
    _kind = "form"

    class FieldGrowthPolicy(metaclass=_Names):
        pass

    def __init__(self, parent: Any = None) -> None:
        # (label, field); a label alone spans both columns
        self._rows: list[tuple[QWidget | None, QWidget | QLayout | None]] = []
        super().__init__(parent)

    def _widgets(self) -> list[QWidget]:
        return [w for row in self._rows for w in row if isinstance(w, QWidget)]

    def _layouts(self) -> list[QLayout]:
        return [f for _label, f in self._rows if isinstance(f, QLayout)]

    def _tree(self) -> dict[str, Any]:
        rows = []
        for label, field in self._rows:
            row: dict[str, Any] = {}
            if label is not None:
                row["label"] = label._tree()
            if isinstance(field, QWidget):
                row["w"] = field._tree()
            elif isinstance(field, QLayout):
                row["l"] = field._tree()
            rows.append(row)
        return {**self._base(), "rows": rows}

    def addRow(self, label: Any, field: Any = None) -> None:
        if field is None and not isinstance(label, str):
            # one widget or layout across both columns
            label, field = None, label
        if isinstance(label, str):
            label = QLabel(label)
        self._rows.append((label, field))
        for item in (label, field):
            if isinstance(item, QWidget):
                self._adopt(item)
            elif isinstance(item, QLayout) and self._owner is not None:
                item._set_owner(self._owner)
        self._changed()

    def rowCount(self) -> int:
        return len(self._rows)

    def setRowVisible(self, row: Any, visible: bool) -> None:
        for label, field in self._rows:
            if row in (label, field) or row == self._rows.index((label, field)):
                for item in (label, field):
                    if isinstance(item, QWidget):
                        item.setVisible(visible)

    def setLabelAlignment(self, *_args: Any) -> None:
        pass

    def setFieldGrowthPolicy(self, *_args: Any) -> None:
        pass


# Web views
##########################################################################


class AnkiWebView(QWidget):
    """aqt.webview.AnkiWebView: a page of the add-on's HTML in a frame. As
    in Qt, evals wait until the page has loaded; the shell's page replays
    the evals since the last stdHtml when it draws the view again."""

    _kind = "web"
    # beyond this, a redrawn view starts blank rather than replay
    REPLAY_LIMIT = 16 * 1024 * 1024

    def __init__(self, parent: Any = None, title: str = "", kind: Any = None) -> None:
        super().__init__(parent)
        self._page = 0
        self._replay: list[str] | None = []
        self._replay_size = 0
        self._bridge: Callable[[str], Any] | None = None
        self._callbacks: dict[int, Callable[[Any], None]] = {}
        self._callback_ids = itertools.count(1)

    def _props(self) -> dict[str, Any]:
        return {**super()._props(), "page": self._page}

    def _tree(self) -> dict[str, Any]:
        node = super()._tree()
        if self._replay:
            node["replay"] = list(self._replay)
        return node

    def _from_page(self, op: str | None, value: Any, event: Any = None) -> Any:
        if op == "bridge" and isinstance(value, str):
            return self._bridge(value) if self._bridge else None
        if op == "evalResult" and isinstance(event, dict):
            callback = self._callbacks.pop(int(event.get("n", 0)), None)
            if callback:
                callback(value)
        return None

    def stdHtml(
        self,
        body: str,
        css: list[str] | None = None,
        js: list[str] | None = None,
        head: str = "",
        context: Any = None,
        default_css: bool = True,
    ) -> None:
        self._page += 1
        self._replay = []
        self._replay_size = 0
        native = _native()
        if native:
            native.notify(
                "widget_page",
                widget=self._id,
                page=self._page,
                body=body,
                css=list(css or []),
                js=list(js or []),
                head=head,
                default_css=default_css,
            )
        self._changed()

    def setHtml(self, html: str, *_args: Any) -> None:
        self.stdHtml(html, default_css=False)

    def eval(self, js: str) -> None:
        self._record(js)
        self._op(eval=js)

    def evalWithCallback(self, js: str, callback: Callable[[Any], None]) -> None:
        n = next(self._callback_ids)
        self._callbacks[n] = callback
        self._record(js)
        self._op(eval=js, callback=n)

    def _record(self, js: str) -> None:
        if self._replay is None:
            return
        self._replay_size += len(js)
        if self._replay_size > self.REPLAY_LIMIT:
            self._replay = None
        else:
            self._replay.append(js)

    def set_bridge_command(self, func: Callable[[str], Any], context: Any) -> None:
        self._bridge = func

    def set_open_links_externally(self, *_args: Any) -> None:
        pass

    def setFocus(self, *_args: Any) -> None:
        pass

    def cleanup(self) -> None:
        pass

    def page(self) -> Any:
        return None


def unsupported(name: str) -> type:
    """A Qt class the shell can't show: it can be named (and its enums
    read), but not created."""

    def __init__(self: Any, *_args: Any, **_kwargs: Any) -> None:
        raise NotImplementedError(
            f"{name} isn't available in the native shell's plugin host"
        )

    return _Names(name, (), {"__init__": __init__})
