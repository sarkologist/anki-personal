# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

from anki.collection import OpChanges
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTimer,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import tooltip

from ..providers import GenerationStopped
from .collection import SETTINGS, Admission, admit, request_repair, snapshot
from .core import MODEL, POLICY, TAG
from .generation import Draft, choose_targets, generate
from .lexicon import Lexicon, atomic_json

MODULE = __name__.split(".")[0]


class Controller:
    def __init__(self) -> None:
        self.timer = QTimer(mw)
        qconnect(self.timer.timeout, self.tick)
        self.stop = Event()
        self.epoch = 0
        self.busy = False
        self.lexicon = None
        self.path: Path | None = None
        self.folder = Path(__file__).parent.parent / "user_files" / "french-dictionary"
        self.state: dict[str, Any] = {}
        self.status = "Choose a French deck to start."
        self.last_version = ""

    def config(self) -> dict[str, Any]:
        base = mw.addonManager.getConfig(MODULE) or {}
        return {**base, **base.get("french", {})}

    def save(self) -> None:
        if self.path:
            atomic_json(self.path, self.state)

    def valid(self, epoch: int) -> bool:
        return bool(
            mw
            and mw.col
            and self.path
            and epoch == self.epoch
            and not self.stop.is_set()
        )

    def closed(self) -> None:
        self.stop.set()
        self.epoch += 1
        self.timer.stop()
        self.busy = False
        self.path = None

    def opened(self) -> None:
        self.closed()
        self.stop = Event()
        identity = hashlib.sha256(mw.col.path.encode()).hexdigest()[:16]
        self.path = (
            Path(__file__).parent.parent / "user_files" / identity / "french-state.json"
        )
        try:
            self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
            if self.state.get("policy") not in (None, POLICY):
                self.state["drafts"] = []
            self.state["policy"] = POLICY
        except (ValueError, OSError) as exc:
            self.status = "Could not read French state: " + str(exc)
            tooltip(self.status)
            return
        config = self.config()
        if (
            not mw.col.get_config(SETTINGS, {})
            and config.get("initial_french_collection_path") == mw.col.path
        ):
            deck = mw.col.decks.by_name(config.get("initial_french_deck_name", ""))
            if deck and not deck.get("dyn"):
                settings = {"enabled": True, "deck_id": int(deck["id"])}
                epoch = self.epoch
                CollectionOp(
                    parent=mw, op=lambda col: col.set_config(SETTINGS, settings)
                ).success(
                    lambda _changes: self.tick() if self.valid(epoch) else None
                ).run_in_background(initiator=self)
        self.timer.start(max(30, int(config.get("poll_seconds", 300))) * 1000)
        mw.progress.single_shot(1500, self.tick)

    def failed(self, exc: Exception, epoch: int) -> None:
        if not self.valid(epoch):
            return
        self.busy = False
        if isinstance(exc, GenerationStopped):
            return
        message = str(exc)[:2000]
        previous = self.state.get("last_error")
        self.state.update(last_error=message, retry_after=time.time() + 1800)
        self.status = message
        self.save()
        if previous != message:
            tooltip("French Frontier: " + message, period=10000)

    def tick(self, *, force: bool = False) -> None:
        if not mw or not mw.col or not self.path or self.busy:
            return
        settings = mw.col.get_config(SETTINGS, {})
        if not settings.get("enabled"):
            self.status = "French generation is paused. Choose settings in Tools → French Frontier."
            return
        if not force and time.time() < self.state.get("retry_after", 0):
            return
        self.busy = True
        epoch = self.epoch
        config = self.config()
        stop = self.stop
        if self.lexicon is None:
            self.status = "Loading French vocabulary and morphology…"

            def loaded(future: Any) -> None:
                if not self.valid(epoch):
                    return
                try:
                    self.lexicon = future.result()
                    self.busy = False
                    self.tick(force=force)
                except Exception as exc:
                    self.failed(exc, epoch)

            mw.taskman.run_in_background(
                lambda: Lexicon.load(self.folder), loaded, uses_collection=False
            )
            return
        lexicon = self.lexicon
        QueryOp(
            parent=mw,
            op=lambda col: snapshot(col, settings, config, lexicon),
            success=lambda current: self.scanned(
                current, settings, config, lexicon, stop, epoch
            ),
        ).failure(lambda exc: self.failed(exc, epoch)).run_in_background()

    def scanned(
        self,
        current: Any,
        settings: dict[str, Any],
        config: dict[str, Any],
        lexicon: Lexicon,
        stop: Event,
        epoch: int,
    ) -> None:
        if not self.valid(epoch):
            return
        if self.state.get("day") != current.day:
            self.state.update(day=current.day, batches=0)
        self.last_version = current.known.version
        try:
            drafts = [Draft.read(d) for d in self.state.get("drafts", [])]
        except (KeyError, TypeError, ValueError) as exc:
            self.failed(exc, epoch)
            return
        if drafts and (current.slots or any(d.repair_note for d in drafts)):
            self.apply(drafts, settings, config, lexicon, stop, epoch)
            return
        if not current.slots and not current.repairs:
            self.status = "No French admission slots: pending new cards, reviews or daily limits reserve capacity."
            self.busy = False
            return
        if self.state.get("batches", 0) >= max(
            1, int(config.get("max_batches_per_day", 8))
        ):
            self.status = "French generation resumes next Anki day."
            self.busy = False
            return
        if not current.known.surfaces:
            self.status = "Study existing French cards to establish context vocabulary."
            self.busy = False
            return
        self.state["batches"] = self.state.get("batches", 0) + 1
        self.save()
        count = min(
            max(current.slots, len(current.repairs)),
            max(1, min(8, int(config.get("batch_size", 4)))),
        )
        version = current.known.version
        self.last_version = version
        deferred = {
            key
            for key, item in self.state.get("deferred", {}).items()
            if item.get("vocabulary") == version
            and time.time() < item.get("retry_after", 0)
        }
        self.status = f"Generating up to {count} French cards with {config.get('provider', 'codex')}…"

        def work() -> Any:
            repairs = {
                key: item
                for key, item in current.repairs.items()
                if key not in deferred
            }
            repair_targets = [item[2] for item in repairs.values()][:count]
            targets = repair_targets + (
                choose_targets(
                    lexicon,
                    current.known,
                    current.represented,
                    current.keys,
                    self.folder,
                    count - len(repair_targets),
                    deferred,
                    stop,
                )
                if current.slots and len(repair_targets) < count
                else []
            )
            if not targets:
                return [], {}
            generated_drafts, failures = generate(
                targets, current.known, lexicon, config, stop
            )
            return [
                replace(
                    d,
                    repair_note=repairs[d.target.key][0],
                    old_fingerprint=repairs[d.target.key][1],
                )
                if d.target.key in repairs
                else d
                for d in generated_drafts
            ], failures

        def generated(future: Any) -> None:
            if not self.valid(epoch):
                return
            try:
                drafts, failures = future.result()
                items = self.state.setdefault("deferred", {})
                for key, reason in failures.items():
                    old = items.get(key, {})
                    attempts = (
                        old.get("attempts", 0) + 1
                        if old.get("vocabulary") == version
                        else 1
                    )
                    items[key] = {
                        "reason": reason,
                        "vocabulary": version,
                        "attempts": attempts,
                        "retry_after": time.time()
                        + min(7 * 86400, 1800 * 2 ** min(attempts, 9)),
                    }
                self.state.update(
                    drafts=[d.as_dict() for d in drafts], last_error="", retry_after=0
                )
                self.save()
                if drafts:
                    self.apply(drafts, settings, config, lexicon, stop, epoch)
                else:
                    self.busy = False
                    self.status = "French targets deferred; retries follow vocabulary growth or backoff."
                    mw.progress.single_shot(5000, self.tick)
            except Exception as exc:
                self.failed(exc, epoch)

        mw.taskman.run_in_background(work, generated, uses_collection=False)

    def apply(
        self,
        drafts: list[Draft],
        settings: dict[str, Any],
        config: dict[str, Any],
        lexicon: Lexicon,
        stop: Event,
        epoch: int,
    ) -> None:
        def applied(result: Admission) -> None:
            if not self.valid(epoch):
                return
            self.state["drafts"] = [d.as_dict() for d in result.remaining]
            for key, reason in result.rejected.items():
                self.state.setdefault("deferred", {})[key] = {
                    "reason": reason,
                    "vocabulary": self.last_version,
                    "retry_after": time.time() + 1800,
                }
            self.save()
            self.busy = False
            self.status = f"Added {len(result.admitted)} French notes; {len(result.remaining)} prepared for later."
            if result.admitted and not result.remaining:
                mw.progress.single_shot(5000, self.tick)

        CollectionOp(
            parent=mw,
            op=lambda col: (
                Admission(OpChanges(), [], drafts, {})
                if stop.is_set()
                else admit(col, settings, config, drafts, lexicon)
            ),
        ).success(applied).failure(
            lambda exc: self.failed(exc, epoch)
        ).run_in_background(initiator=self)

    def reviewer_menu(self, reviewer: Any, menu: Any) -> None:
        if not reviewer.card:
            return
        note = reviewer.card.note()
        if TAG not in note.tags or note.note_type()["name"] != MODEL:
            return
        note_id = int(note.id)
        action = menu.addAction("Replace this French sentence automatically")
        qconnect(
            action.triggered,
            lambda: (
                CollectionOp(parent=mw, op=lambda col: request_repair(col, note_id))
                .success(lambda _r: self.tick(force=True))
                .run_in_background(initiator=self)
            ),
        )

    def settings_dialog(self) -> None:
        if not mw or not mw.col:
            return
        settings = mw.col.get_config(SETTINGS, {})
        config = self.config()
        dialog = QDialog(mw)
        dialog.setWindowTitle("French Frontier")
        dialog.resize(620, 300)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        enabled = QCheckBox(
            "Automatically introduce French vocabulary and conjugation cards"
        )
        enabled.setChecked(bool(settings.get("enabled", True)))
        deck = QComboBox()
        for item in mw.col.decks.all_names_and_ids(include_filtered=False):
            deck.addItem(item.name, int(item.id))
        preferred = next(
            (i for i in range(deck.count()) if deck.itemText(i).casefold() == "french"),
            0,
        )
        deck.setCurrentIndex(
            max(0, deck.findData(settings.get("deck_id", deck.itemData(preferred))))
        )
        provider = QComboBox()
        provider.addItem("Codex subscription", "codex")
        provider.addItem("Claude subscription", "claude")
        provider.setCurrentIndex(
            max(0, provider.findData(config.get("provider", "codex")))
        )
        model = QLineEdit(config.get(f"{provider.currentData()}_model", ""))
        model.setPlaceholderText("Use the CLI default model")
        qconnect(
            provider.currentIndexChanged,
            lambda _i: model.setText(config.get(f"{provider.currentData()}_model", "")),
        )
        form.addRow(enabled)
        form.addRow("French deck", deck)
        form.addRow("Generator", provider)
        form.addRow("Model", model)
        layout.addLayout(form)
        status = QLabel(self.status)
        status.setWordWrap(True)
        layout.addWidget(status)
        refresh = QTimer(dialog)
        qconnect(refresh.timeout, lambda: status.setText(self.status))
        refresh.start(1000)
        check = QPushButton("Check now")
        qconnect(check.clicked, lambda: self.tick(force=True))
        layout.addWidget(check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )

        def save() -> None:
            base = mw.addonManager.getConfig(MODULE) or {}
            french = dict(base.get("french", {}))
            french.update(provider=provider.currentData())
            french[f"{provider.currentData()}_model"] = model.text().strip()
            base["french"] = french
            mw.addonManager.writeConfig(MODULE, base)
            self.stop.set()
            self.epoch += 1
            self.stop = Event()
            self.busy = False
            saved = {"enabled": enabled.isChecked(), "deck_id": int(deck.currentData())}
            CollectionOp(
                parent=mw, op=lambda col: col.set_config(SETTINGS, saved)
            ).success(lambda _r: self.tick(force=True)).run_in_background(
                initiator=self
            )
            dialog.accept()

        qconnect(buttons.accepted, save)
        qconnect(buttons.rejected, dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()


_controller: Controller | None = None


def install() -> None:
    global _controller
    if _controller is not None or not mw:
        return
    controller = _controller = Controller()
    action = QAction("French Frontier…", mw)
    qconnect(action.triggered, controller.settings_dialog)
    mw.form.menuTools.addAction(action)
    gui_hooks.profile_did_open.append(controller.opened)
    gui_hooks.profile_will_close.append(controller.closed)
    gui_hooks.sync_did_finish.append(controller.tick)
    gui_hooks.reviewer_will_show_context_menu.append(controller.reviewer_menu)
    gui_hooks.reviewer_did_answer_card.append(
        lambda *_args: mw.progress.single_shot(1500, controller.tick)
    )
    if mw.col:
        controller.opened()
