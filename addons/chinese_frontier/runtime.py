# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import time
from concurrent.futures import Future
from dataclasses import asdict
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
    QListWidget,
    QListWidgetItem,
    QPushButton,
    Qt,
    QTextEdit,
    QTimer,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import showInfo, tooltip

from .collection import (
    SETTINGS_KEY,
    AdmissionResult,
    Snapshot,
    admit,
    initial_scope_settings,
    request_repair,
    snapshot,
    upgrade_presentation,
)
from .core import GENERATED_TAG, admission_slots
from .diagnostics import Diagnostics
from .generation import GenerationResult, generate
from .lexicon import Lexicon
from .preparation import buffer_size, preparation_needed, previous_lessons, read_lesson
from .providers import GenerationStopped
from .state import State, vocabulary_version
from .telemetry import import_calls, observe, recover

MODULE = __name__.split(".")[0]


class Controller:
    def __init__(self) -> None:
        self.epoch = 0
        self.stop = Event()
        self.busy = False
        self.state = State()
        self.path: Path | None = None
        self.dictionary_folder = Path(__file__).parent / "user_files" / "dictionary"
        self.last_snapshot: Snapshot | None = None
        self.status = "Choose a Chinese deck to start."
        self.notified_error = ""
        self.timer = QTimer(mw)
        qconnect(self.timer.timeout, self.tick)
        self.wake_timer = QTimer(mw)
        self.wake_timer.setSingleShot(True)
        qconnect(self.wake_timer.timeout, self.tick)

    def observe(self, event: dict[str, Any]) -> None:
        observe(self.state.telemetry, event, vars(self.state).get("day", -1))
        self.save_state()

    def diagnostics_data(self) -> dict[str, Any]:
        return {
            "store": self.state.telemetry,
            "state": {
                k: vars(self.state).get(k, 0)
                for k in (
                    "day",
                    "retry_after",
                    "refill_at",
                    "cycle_batches",
                    "cycle_limit",
                )
            },
            "status": self.status,
            "busy": self.busy,
            "prepared": sum(
                not d.audit_only and d.additional_cards > 0 for d in self.state.drafts
            ),
            "buffer": buffer_size(self.config()),
            "deferred": dict(
                sorted(
                    self.state.deferred.items(),
                    key=lambda item: item[1].get("retry_after", 0),
                    reverse=True,
                )[:20]
            ),
        }

    def config(self) -> dict[str, Any]:
        assert mw
        return mw.addonManager.getConfig(MODULE) or {}

    def opened(self) -> None:
        assert mw
        self.closed()
        self.stop = Event()
        identity = hashlib.sha256(mw.col.path.encode()).hexdigest()[:16]
        self.path = Path(__file__).parent / "user_files" / identity / "state.json"
        try:
            self.state = State.load(self.path)
        except Exception as exc:
            self.status = f"Could not read saved frontier state: {exc}"
            tooltip(self.status)
            return
        import_calls(
            self.state.telemetry,
            vars(self.state).get("model_calls", []),
            vars(self.state).get("day", -1),
        )
        recover(self.state.telemetry, vars(self.state).get("day", -1))
        self.save_state()
        interval = max(30, int(self.config().get("poll_seconds", 300)))
        self.timer.start(interval * 1000)
        config = self.config()
        if (
            initial_scope_settings(mw.col, config) is not None
            or mw.col.models.by_name("Chinese Frontier") is not None
        ):
            stop = self.stop

            def configure(col: Any) -> Any:
                if stop.is_set():
                    return OpChanges()
                settings = initial_scope_settings(col, config)
                changes = upgrade_presentation(col)
                if settings is not None:
                    changes.MergeFrom(col.set_config(SETTINGS_KEY, settings))
                return changes

            epoch = self.epoch
            CollectionOp(parent=mw, op=configure).success(
                lambda _result: self.tick() if self.valid(epoch) else None
            ).failure(lambda exc: self.failed(exc, epoch)).run_in_background(
                initiator=self
            )
            return
        mw.progress.single_shot(1500, self.tick)

    def closed(self) -> None:
        recover(self.state.telemetry, vars(self.state).get("day", -1))
        self.save_state()
        self.epoch += 1
        self.stop.set()
        self.timer.stop()
        self.wake_timer.stop()
        self.busy = False
        self.last_snapshot = None
        self.path = None

    def save_state(self) -> None:
        if self.path:
            self.state.save(self.path)

    def valid(self, epoch: int) -> bool:
        return bool(
            mw
            and mw.col
            and self.path
            and epoch == self.epoch
            and not self.stop.is_set()
        )

    def failed(self, exc: Exception, epoch: int) -> None:
        if not self.valid(epoch):
            return
        self.busy = False
        if isinstance(exc, GenerationStopped):
            return
        self.observe(
            {
                "type": "decision",
                "stage": "runtime",
                "outcome": "error",
                "reason": str(exc),
            }
        )
        self.state.last_error = str(exc)[:2000]
        self.state.retry_after = time.time() + 1800
        self.status = self.state.last_error
        self.save_state()
        # Persistent account/CLI failures notify once; repeated polling stays quiet.
        if self.notified_error != self.status:
            self.notified_error = self.status
            tooltip("Chinese Frontier: " + self.status, period=10000)

    def tick(self, *, force: bool = False) -> None:
        if not mw or not mw.col or not self.path or self.busy:
            return
        settings = mw.col.get_config(SETTINGS_KEY, {})
        if not settings.get("enabled"):
            self.status = (
                "Automatic conversion is paused."
                if settings
                else "Choose a Chinese deck in Tools → Chinese Frontier."
            )
            return
        if not force and time.time() < self.state.retry_after:
            return
        self.busy = True
        epoch = self.epoch
        config = self.config()
        QueryOp(
            parent=mw,
            op=lambda col: snapshot(col, settings, config),
            success=lambda result: self.scanned(result, settings, config, epoch),
        ).failure(lambda exc: self.failed(exc, epoch)).run_in_background()

    def scanned(
        self,
        current: Snapshot,
        settings: dict[str, Any],
        config: dict[str, Any],
        epoch: int,
    ) -> None:
        if not self.valid(epoch):
            return
        assert mw
        self.last_snapshot = current
        self.state.reset_day(current.day)
        self.state.telemetry["snapshot"] = {
            "at": time.time(),
            "slots": max((current.slots(did) for did in current.budgets), default=0),
            "known": len(current.known),
            "budgets": list(
                {
                    b.deck_id: asdict(b)
                    for b in [
                        b for budgets in current.budgets.values() for b in budgets
                    ]
                }.values()
            ),
        }
        eligible = {source.key: source for source in current.sources}
        self.state.drafts = [
            draft
            for draft in self.state.drafts
            if draft.source.key in eligible
            and eligible[draft.source.key].fingerprint == draft.source.fingerprint
        ]
        self.save_state()
        if any(
            draft.can_progress(current.slots(draft.source.deck_id))
            for draft in self.state.drafts
        ):
            self.status = "Rechecking prepared sentences…"
            folder = self.dictionary_folder
            QueryOp(
                parent=mw,
                op=lambda _col: Lexicon.load(folder),
                success=lambda lexicon: self.apply(lexicon, settings, config, epoch),
            ).without_collection().failure(
                lambda exc: self.failed(exc, epoch)
            ).run_in_background()
            return
        prepared_keys = {draft.source.key for draft in self.state.drafts}
        if not current.known:
            self.busy = False
            self.status = "No established vocabulary yet. Study existing cards or mark familiar words in add-on configuration."
            return
        available = max((current.slots(did) for did in current.budgets), default=0)
        queued = sum(
            not d.audit_only and d.additional_cards > 0 for d in self.state.drafts
        )
        needed = preparation_needed(available, queued, config)
        if not needed and not current.repairs:
            self.status = "No admission slots: today's limits, pending new cards, or reviews reserve the capacity."
            self.busy = False
            return
        if not self.state.can_refill(needed, len(current.repairs), config, time.time()):
            self.status = (
                "Chinese refill is cooling down; it will retry automatically."
                if config.get("auto_fill", True)
                else "Today's generation budget is used; resumes next Anki day."
            )
            self.save_state()
            self.busy = False
            self.schedule_next()
            return
        version = vocabulary_version(current.known)
        count = max(1, min(12, int(config.get("batch_size", 4))))
        sources = []
        reservations: dict[int, int] = {}
        ahead = max(0, buffer_size(config) - queued)
        words: set[str] = set()
        for source in current.sources:
            if (
                source.key in prepared_keys
                or source.word in words
                or not self.state.can_try(source.key, version)
            ):
                continue
            budgets = current.budgets[source.deck_id]
            repairing = source.key in current.repairs
            if not repairing and any(
                reservations.get(budget.deck_id, 0)
                >= admission_slots(
                    [budget], ignore_review_limit=current.ignore_review_limit
                )
                + ahead
                for budget in budgets
            ):
                continue
            if not repairing and current.slots(source.deck_id) + ahead <= 0:
                continue
            sources.append(source)
            words.add(source.word)
            if not repairing:
                for budget in budgets:
                    reservations[budget.deck_id] = (
                        reservations.get(budget.deck_id, 0) + 1
                    )
            if len(sources) >= count:
                break
        if not sources:
            self.busy = False
            available = max((current.slots(did) for did in current.budgets), default=0)
            if available <= 0:
                self.status = "No admission slots: today's limits, pending new cards, or reviews reserve the capacity."
            elif not current.known:
                self.status = "No established vocabulary yet. Study existing cards or mark familiar words in add-on configuration."
            elif current.sources:
                self.status = "Remaining targets are deferred; retries follow vocabulary growth or their backoff."
            else:
                self.status = "All eligible suspended words are represented."
            self.state.pause_refill(config, time.time())
            self.save_state()
            self.schedule_next()
            return
        self.state.reserve_batch()
        self.save_state()  # Reserve the generation attempt before launching the CLI.
        self.status = f"Generating sentences for {len(sources)} words with {config.get('provider', 'codex')}…"
        stop = self.stop
        folder = self.dictionary_folder
        retained = list(self.state.drafts)

        def checkpoint(approved: list[Any]) -> None:
            prepared = {d.source.key: d for d in retained + approved}

            def save_approved() -> None:
                if self.valid(epoch):
                    self.state.drafts = list(prepared.values())
                    self.save_state()

            mw.taskman.run_on_main(save_approved)

        def report(event: dict[str, Any]) -> None:
            def update() -> None:
                if not self.valid(epoch):
                    return
                self.observe(event)
                if event.get("type") != "call":
                    return
                self.state.active_request = event
                self.status = f"{event['phase']} (attempt {event['attempt']}/3)"
                if event["finished"]:
                    self.state.model_calls = (self.state.model_calls + [event])[-30:]
                self.save_state()

            mw.taskman.run_on_main(update)

        lessons = previous_lessons(self.state.deferred, (s.key for s in sources))
        # Unlike a collection QueryOp, long CLI generation must not prevent
        # syncing, closing a profile, or studying cards.
        mw.taskman.run_in_background(
            lambda: generate(
                sources,
                current.known,
                config,
                folder,
                stop,
                lessons=lessons,
                contexts=getattr(current, "contexts", {}),
                on_approved=checkpoint,
                progress=report,
                observe=report,
            ),
            lambda future: self.generated(
                future,
                settings,
                config,
                version,
                epoch,
                attempted=len(sources),
                retained_keys=prepared_keys,
            ),
            uses_collection=False,
        )

    def generated(
        self,
        future: Future,
        settings: dict[str, Any],
        config: dict[str, Any],
        version: str,
        epoch: int,
        *,
        attempted: int,
        retained_keys: set[str] | None = None,
    ) -> None:
        if not self.valid(epoch):
            return
        try:
            result: GenerationResult = future.result()
        except Exception as exc:
            approved = sum(
                not d.audit_only and d.source.key not in (retained_keys or set())
                for d in self.state.drafts
            )
            self.state.record_batch(attempted, approved, config, time.time())
            self.observe(
                {
                    "type": "batch",
                    "attempted": attempted,
                    "approved": approved,
                    "outcome": "error",
                }
            )
            self.failed(exc, epoch)
            return
        self.state.record_batch(
            attempted,
            sum(not draft.audit_only for draft in result.drafts),
            config,
            time.time(),
        )
        self.observe(
            {
                "type": "batch",
                "attempted": attempted,
                "approved": sum(not d.audit_only for d in result.drafts),
            }
        )
        for key, reason in result.deferred.items():
            self.state.defer(key, reason, version)
        self.state.drafts = list(
            {d.source.key: d for d in self.state.drafts + result.drafts}.values()
        )
        self.state.last_error = ""
        self.state.retry_after = 0
        self.save_state()
        if result.drafts:
            self.apply(result.lexicon, settings, config, epoch)
        else:
            self.busy = False
            self.status = (
                "These targets need simpler sentences; deferred automatically."
            )
            self.schedule_next()

    def apply(
        self,
        lexicon: Lexicon,
        settings: dict[str, Any],
        config: dict[str, Any],
        epoch: int,
    ) -> None:
        if not self.valid(epoch):
            return
        assert mw
        drafts = list(self.state.drafts)
        stop = self.stop
        CollectionOp(
            parent=mw,
            op=lambda col: (
                AdmissionResult(OpChanges(), [], drafts, {})
                if stop.is_set()
                else admit(col, settings, config, drafts, lexicon)
            ),
        ).success(lambda result: self.applied(result, epoch)).failure(
            lambda exc: self.failed(exc, epoch)
        ).run_in_background(initiator=self)

    def applied(self, result: AdmissionResult, epoch: int) -> None:
        if not self.valid(epoch):
            return
        self.observe(
            {
                "type": "admission",
                "notes": len(result.admitted),
                "cards": result.added_cards,
                "updates": len(result.updated),
            }
        )
        for key, reason in result.rejected.items():
            self.observe(
                {
                    "type": "decision",
                    "stage": "admission",
                    "outcome": "rejected",
                    "key": key,
                    "reason": reason,
                }
            )
        self.state.drafts = result.remaining
        version = (
            vocabulary_version(self.last_snapshot.known) if self.last_snapshot else ""
        )
        for key, reason in result.rejected.items():
            self.state.defer(key, reason, version)
        self.state.last_error = ""
        self.state.retry_after = 0
        self.save_state()
        self.busy = False
        self.status = f"Introduced {len(result.admitted)} sentence notes; {len(result.remaining)} prepared for later."
        if result.updated:
            self.status += (
                f" Updated {len(result.updated)} notes, keeping existing schedules."
            )
        if result.admitted or result.updated or not result.remaining:
            self.schedule_next()

    def schedule_next(self) -> None:
        if mw and self.path:
            ready_at = max(self.state.refill_at, self.state.retry_after)
            self.wake_timer.start(max(5000, int((ready_at - time.time()) * 1000)))

    def reviewer_menu(self, reviewer: Any, menu: Any) -> None:
        if (
            not mw
            or not reviewer.card
            or GENERATED_TAG not in reviewer.card.note().tags
        ):
            return
        note_id = reviewer.card.nid
        action = menu.addAction("Replace this sentence automatically")

        def replace() -> None:
            if not mw:
                return
            CollectionOp(
                parent=mw, op=lambda col: request_repair(col, note_id)
            ).success(lambda _result: self.tick(force=True)).run_in_background(
                initiator=self
            )

        qconnect(action.triggered, replace)

    def settings_dialog(self) -> None:
        if not mw or not mw.col:
            return
        settings = mw.col.get_config(SETTINGS_KEY, {})
        config = self.config()
        dialog = QDialog(mw)
        dialog.setWindowTitle("Chinese Frontier")
        dialog.resize(850, 780)
        layout = QVBoxLayout(dialog)
        introduction = QLabel(
            "Replace suspended single-word cards with one-word sentence clozes.<br>Original notes stay suspended; your daily limits control introductions."
        )
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        form = QFormLayout()
        enabled = QCheckBox("Automatically generate and introduce sentence cards")
        enabled.setChecked(bool(settings.get("enabled", True)))
        deck = QComboBox()
        deck.addItem("Choose a deck…", 0)
        for item in mw.col.decks.all_names_and_ids(include_filtered=False):
            deck.addItem(item.name, int(item.id))
        selected = deck.findData(int(settings.get("deck_id", 0)))
        if selected <= 0:
            preferred = [
                index
                for index in range(1, deck.count())
                if deck.itemText(index).casefold()
                in ("chinese", "中文", "汉语", "漢語", "mandarin")
            ]
            selected = preferred[0] if len(preferred) == 1 else 0
        deck.setCurrentIndex(max(0, selected))
        provider = QComboBox()
        provider.addItem("Codex subscription", "codex")
        provider.addItem("Claude subscription", "claude")
        provider.setCurrentIndex(
            max(0, provider.findData(config.get("provider", "codex")))
        )
        model = QLineEdit(str(config.get(f"{provider.currentData()}_model", "")))
        model.setPlaceholderText("Use the CLI default model")
        qconnect(
            provider.currentIndexChanged,
            lambda _index: model.setText(
                str(config.get(f"{provider.currentData()}_model", ""))
            ),
        )
        form.addRow(enabled)
        form.addRow("Chinese source deck", deck)

        def extra_decks(field: str) -> QListWidget:
            choices = QListWidget()
            choices.setMaximumHeight(110)
            chosen = set(settings.get(field, []))
            for item in mw.col.decks.all_names_and_ids(include_filtered=False):
                entry = QListWidgetItem(item.name, choices)
                entry.setData(Qt.ItemDataRole.UserRole, int(item.id))
                entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                entry.setCheckState(
                    Qt.CheckState.Checked
                    if int(item.id) in chosen
                    else Qt.CheckState.Unchecked
                )
            return choices

        sources = extra_decks("source_deck_ids")
        vocabulary = extra_decks("vocabulary_deck_ids")
        sources.setToolTip(
            "Suspended word targets from these decks become new cards in the chosen Chinese deck. These extra sources do not establish context vocabulary."
        )
        vocabulary.setToolTip(
            "Only established live cards supply context; suspended cards here are not converted."
        )
        form.addRow("Extra sources → chosen deck", sources)
        form.addRow("Extra context vocabulary", vocabulary)
        form.addRow("Sentence generator", provider)
        form.addRow("Model (optional)", model)
        layout.addLayout(form)
        status = QTextEdit()
        status.setReadOnly(True)
        status.setMaximumHeight(160)
        diagnostics = Diagnostics(dialog, self.diagnostics_data)

        def update_status() -> None:
            text = self.status
            if current := self.last_snapshot:
                text += f"\n\nEstablished vocabulary: {len(current.known)} words\nEligible sources: {len(current.sources)}\nPrepared sentences: {len(self.state.drafts)}\nDeferred targets: {len(self.state.deferred)}"
                if current.skipped:
                    text += "\nSkipped: " + ", ".join(
                        f"{reason}: {count}"
                        for reason, count in current.skipped.items()
                    )
            for draft in self.state.drafts[:4]:
                text += f"\n\n{draft.source.word}: {draft.candidate.sentence}\n{draft.candidate.translation}"
            for item in list(self.state.deferred.values())[:4]:
                text += "\nDeferred: " + str(
                    read_lesson(item.get("reason", "")).get("reason", "")
                )
            status.setPlainText(text)
            diagnostics.refresh()

        update_status()
        layout.addWidget(status)
        layout.addWidget(diagnostics)
        refresh_timer = QTimer(dialog)
        qconnect(refresh_timer.timeout, update_status)
        refresh_timer.start(1000)
        check = QPushButton("Check now")
        qconnect(check.clicked, lambda: self.tick(force=True))
        layout.addWidget(check)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close
        )

        def save() -> None:
            if not deck.currentData():
                showInfo("Choose your Chinese deck first.", parent=dialog)
                return
            new_settings = {
                **settings,
                "deck_id": int(deck.currentData()),
                "enabled": enabled.isChecked(),
                **{
                    field: [
                        choices.item(index).data(Qt.ItemDataRole.UserRole)
                        for index in range(choices.count())
                        if choices.item(index).checkState() == Qt.CheckState.Checked
                    ]
                    for field, choices in (
                        ("source_deck_ids", sources),
                        ("vocabulary_deck_ids", vocabulary),
                    )
                },
            }
            config["provider"] = provider.currentData()
            config[f"{provider.currentData()}_model"] = model.text().strip()
            self.stop.set()
            self.epoch += 1
            self.stop = Event()
            self.busy = False
            mw.addonManager.writeConfig(MODULE, config)
            CollectionOp(
                parent=mw, op=lambda col: col.set_config(SETTINGS_KEY, new_settings)
            ).success(lambda _result: self.tick(force=True)).run_in_background(
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
    action = QAction("Chinese Frontier…", mw)
    qconnect(action.triggered, controller.settings_dialog)
    mw.form.menuTools.addAction(action)
    mw.addonManager.setConfigAction(MODULE, controller.settings_dialog)
    gui_hooks.profile_did_open.append(controller.opened)
    gui_hooks.profile_will_close.append(controller.closed)
    gui_hooks.sync_did_finish.append(controller.tick)
    gui_hooks.reviewer_will_show_context_menu.append(controller.reviewer_menu)
    gui_hooks.operation_did_execute.append(
        lambda _changes, initiator: (
            controller.schedule_next() if initiator is not controller else None
        )
    )
    gui_hooks.reviewer_did_answer_card.append(
        lambda _reviewer, _card, _ease: controller.schedule_next()
    )
    gui_hooks.state_did_change.append(lambda _new, _old: controller.schedule_next())
    if mw.col:
        controller.opened()
