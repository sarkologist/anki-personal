# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The plugin host's `mw`: the collection, a main-thread loop with timers,
taskman, progress and add-on config, as far as the supported add-ons use
them. The shell owns the windows; see aqt.native for how this talks to it."""

from __future__ import annotations

import heapq
import itertools
import json
import logging
import os
import queue
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from anki.collection import Collection


def report_exception(where: str) -> None:
    """A plugin error goes to the log; the host carries on."""
    print(f"plugin host: error in {where}:\n{traceback.format_exc()}", flush=True)


class Loop:
    """The sidecar's main thread: callbacks posted from any thread, plus
    timers, run one at a time, like Qt's event loop."""

    def __init__(self) -> None:
        self._queue: queue.SimpleQueue[Callable[[], None] | None] = queue.SimpleQueue()
        self._timers: list[tuple[float, int, Callable[[], None]]] = []
        self._timers_lock = threading.Lock()
        self._seq = itertools.count()
        self._stopped = False
        self.thread = threading.current_thread()
        # run after each task (aqt.editor sends the notes add-ons changed)
        self.after_task: list[Callable[[], None]] = []

    def post(self, fn: Callable[[], None]) -> None:
        self._queue.put(fn)

    def call_later(self, seconds: float, fn: Callable[[], None]) -> None:
        with self._timers_lock:
            heapq.heappush(
                self._timers, (time.monotonic() + seconds, next(self._seq), fn)
            )
        # wake the loop so it waits for the new deadline
        self._queue.put(lambda: None)

    def stop(self) -> None:
        self._stopped = True
        self._queue.put(None)

    def is_main_thread(self) -> bool:
        return threading.current_thread() is self.thread

    def run(self) -> None:
        while not self._stopped:
            for fn in self._due_timers():
                self._run(fn)
            try:
                fn = self._queue.get(timeout=self._wait())
            except queue.Empty:
                continue
            if fn is None:
                break
            self._run(fn)

    def run_until(self, done: Callable[[], bool]) -> None:
        """A nested loop (a modal dialog's): run tasks until `done`."""
        while not done() and not self._stopped:
            for fn in self._due_timers():
                self._run(fn)
            if done():
                return
            try:
                fn = self._queue.get(timeout=self._wait())
            except queue.Empty:
                continue
            if fn is None:
                # stopping: the outer loop sees it too
                self._stopped = True
                return
            self._run(fn)

    def run_pending(self) -> None:
        """Run what's queued and due now (tests)."""
        while True:
            for fn in self._due_timers():
                self._run(fn)
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                return
            if fn is not None:
                self._run(fn)

    def _wait(self) -> float | None:
        with self._timers_lock:
            if not self._timers:
                return None
            return max(0.0, self._timers[0][0] - time.monotonic())

    def _due_timers(self) -> list[Callable[[], None]]:
        due = []
        now = time.monotonic()
        with self._timers_lock:
            while self._timers and self._timers[0][0] <= now:
                due.append(heapq.heappop(self._timers)[2])
        return due

    def _run(self, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            report_exception(getattr(fn, "__qualname__", repr(fn)))
        for hook in self.after_task:
            try:
                hook()
            except Exception:
                report_exception(getattr(hook, "__qualname__", repr(hook)))


class TaskManager:
    """aqt.taskman: collection tasks run one at a time, others in parallel."""

    def __init__(self, loop: Loop) -> None:
        self.loop = loop
        self._collection_executor = ThreadPoolExecutor(max_workers=1)
        self._no_collection_executor = ThreadPoolExecutor()

    def run_on_main(self, closure: Callable[[], None]) -> None:
        self.loop.post(closure)

    def run_in_background(
        self,
        task: Callable,
        on_done: Callable[[Future], None] | None = None,
        args: dict[str, Any] | None = None,
        uses_collection: bool = True,
    ) -> Future:
        executor = (
            self._collection_executor
            if uses_collection
            else self._no_collection_executor
        )
        fut = executor.submit(task, **(args or {}))
        if on_done is not None:
            fut.add_done_callback(
                lambda future: self.run_on_main(lambda: on_done(future))
            )
        return fut

    def with_progress(
        self,
        task: Callable,
        on_done: Callable[[Future], None] | None = None,
        uses_collection: bool = True,
        **_kwargs: Any,
    ) -> None:
        self.run_in_background(task, on_done, uses_collection=uses_collection)

    def shutdown(self) -> None:
        for executor in (self._collection_executor, self._no_collection_executor):
            executor.shutdown(wait=False, cancel_futures=True)


class ProgressManager:
    """aqt.progress without the progress window: just its timers."""

    def __init__(self, mw: MainWindow) -> None:
        self.mw = mw

    def single_shot(
        self, ms: int, func: Callable[[], None], requires_collection: bool = True
    ) -> None:
        def fire() -> None:
            if not requires_collection or self.mw.col:
                func()

        self.mw.loop.call_later(ms / 1000, fire)

    def timer(
        self,
        ms: int,
        func: Callable,
        repeat: bool,
        requiresCollection: bool = True,
        *,
        parent: Any = None,
    ) -> Any:
        from aqt.qt import QTimer, qconnect

        timer = QTimer(parent)
        timer.setSingleShot(not repeat)

        def fire() -> None:
            if not requiresCollection or self.mw.col:
                func()

        qconnect(timer.timeout, fire)
        timer.start(ms)
        return timer

    def start(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def finish(self) -> None:
        pass

    def update(self, *_args: Any, **_kwargs: Any) -> None:
        pass


class AddonManager:
    """Config and web exports for add-ons in the old Anki's addons21 folder,
    read and written the same way, so both hosts share them."""

    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.web_exports: dict[str, str] = {}
        self.config_actions: dict[str, Callable[[], Any]] = {}
        self.on_web_exports: Callable[[str, str], None] | None = None

    def addonsFolder(self, module: str | None = None) -> str:
        if module is None:
            return self.folder
        return os.path.join(self.folder, module)

    @staticmethod
    def addonFromModule(module: str) -> str:
        return module.split(".", maxsplit=1)[0]

    addon_from_module = addonFromModule

    def addonMeta(self, module: str) -> dict[str, Any]:
        try:
            with open(
                os.path.join(self.addonsFolder(module), "meta.json"), encoding="utf8"
            ) as f:
                return json.load(f)
        except Exception:
            return {}

    def writeAddonMeta(self, module: str, meta: dict[str, Any]) -> None:
        with open(
            os.path.join(self.addonsFolder(module), "meta.json"), "w", encoding="utf8"
        ) as f:
            json.dump(meta, f)

    def isEnabled(self, module: str) -> bool:
        return not self.addonMeta(module).get("disabled")

    def addonConfigDefaults(self, module: str) -> dict[str, Any] | None:
        try:
            with open(
                os.path.join(self.addonsFolder(module), "config.json"), encoding="utf8"
            ) as f:
                return json.load(f)
        except Exception:
            return None

    def getConfig(self, module: str) -> dict[str, Any] | None:
        addon = self.addonFromModule(module)
        config = self.addonConfigDefaults(addon)
        if config is None:
            return None
        config.update(self.addonMeta(addon).get("config", {}))
        return config

    def writeConfig(self, module: str, conf: dict) -> None:
        addon = self.addonFromModule(module)
        meta = self.addonMeta(addon)
        meta["config"] = conf
        self.writeAddonMeta(addon, meta)

    def setConfigAction(self, module: str, fn: Callable[[], Any]) -> None:
        self.config_actions[self.addonFromModule(module)] = fn

    def setConfigUpdatedAction(self, module: str, fn: Callable[[Any], None]) -> None:
        pass

    def logs_folder(self, module: str) -> Path:
        """As Anki's: `logs/addons/<add-on>` in the base folder."""
        base = os.path.dirname(os.path.abspath(self.folder))
        return Path(base) / "logs" / "addons" / self.addonFromModule(module)

    def get_logger(self, module: str) -> logging.Logger:
        """The add-on's logger, writing to `<add-on>.log` in its logs folder
        (kept for 10 days), as in Anki."""
        addon = self.addonFromModule(module)
        logger = logging.getLogger(f"addon.{addon}")
        if not logger.handlers:
            path = self.logs_folder(addon) / f"{addon}.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            handler = TimedRotatingFileHandler(
                filename=path, when="D", interval=1, backupCount=10, encoding="utf-8"
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s:%(levelname)s:%(name)s: %(message)s")
            )
            logger.addHandler(handler)
            # not in the host's own log
            logger.propagate = False
        return logger

    def setWebExports(self, module: str, pattern: str) -> None:
        addon = self.addonFromModule(module)
        self.web_exports[addon] = pattern
        if self.on_web_exports:
            self.on_web_exports(addon, pattern)


class Menu:
    """A menu-bar menu: actions added here appear in the shell's menu."""

    def __init__(self, name: str, mw: MainWindow) -> None:
        self.name = name
        self.mw = mw
        self.actions: list[Any] = []

    def addAction(self, action: Any) -> Any:
        from aqt.qt import QAction

        if isinstance(action, str):
            action = QAction(action, self.mw)
        self.actions.append(action)
        action._menu = self
        self.mw.native.menu_changed(self)
        return action

    def addSeparator(self) -> None:
        pass

    def addMenu(self, title: str) -> Menu:
        # submenus are flattened into this one
        return self

    def removeAction(self, action: Any) -> None:
        if action in self.actions:
            self.actions.remove(action)
            self.mw.native.menu_changed(self)


class Form:
    def __init__(self, mw: MainWindow) -> None:
        self.menuTools = Menu("tools", mw)


class DeckBrowser:
    def __init__(self, mw: MainWindow) -> None:
        self.mw = mw

    def refresh(self) -> None:
        self.mw.native.notify("refresh_main")

    def show(self) -> None:
        self.refresh()


class Reviewer:
    """What reviewer hooks see: the shown card, and which side."""

    def __init__(self, mw: MainWindow) -> None:
        self.mw = mw
        self.card: Any = None
        self.state: str | None = None
        # the reviewer's page isn't the add-ons'
        self.web: Any = None


class ProfileManager:
    def __init__(self, name: str, folder: str, base: str) -> None:
        self.name = name
        self._folder = folder
        self.base = base

    def profileFolder(self, create: bool = True) -> str:
        return self._folder

    def addonFolder(self) -> str:
        return os.path.join(self.base, "addons21")


class MainWindow:
    """`aqt.mw`. `col` is set while the shell's profile is open."""

    def __init__(self, native: Any, addons_folder: str) -> None:
        self.native = native
        self.loop = Loop()
        self.col: Collection | None = None
        self.state = "deckBrowser"
        self.taskman = TaskManager(self.loop)
        self.progress = ProgressManager(self)
        self.addonManager = AddonManager(addons_folder)
        self.form = Form(self)
        self.deckBrowser = DeckBrowser(self)
        self.reviewer = Reviewer(self)
        self.pm: ProfileManager | None = None

    def weakref(self) -> MainWindow:
        return self

    def serverURL(self) -> str:
        return "anki://localhost/"
