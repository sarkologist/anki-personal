# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import asdict, replace
from pathlib import Path
from threading import Event
from typing import Any

from anki.collection import OpChanges
from anki.notes import NoteId
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
    QTextEdit,
    QTimer,
    QVBoxLayout,
    qconnect,
)
from aqt.utils import tooltip

from ..collection import MAX_REPAIRS
from ..core import FEEDBACK_REASONS
from ..diagnostics import Diagnostics
from ..preparation import (
    TIMEOUT_LESSON,
    buffer_size,
    preparation_needed,
    previous_lessons,
)
from ..providers import CallTimeout, GenerationStopped
from ..refill import record_batch, refill_ready, reserve_batch, reset_day
from ..telemetry import error_location, import_calls, observe, recover
from .collection import (
    MANUAL_TAG,
    SETTINGS,
    Admission,
    admit,
    drop_cue_labels,
    json_field,
    record_feedback,
    refresh_conjugation_tables,
    repair_count,
    snapshot,
)
from .core import MODEL, POLICY, TAG, Target
from .generation import (
    Draft,
    choose_targets,
    generate,
    merge_drafts,
    rank_targets,
    vocabulary_floor,
)
from .lexicon import Lexicon, atomic_json

MODULE = __name__.split(".")[0]


def defer_target(
    state: dict[str, Any], key: str, reason: str, version: str, now: float
) -> None:
    items = state.setdefault("deferred", {})
    old = items.get(key, {})
    attempts = old.get("attempts", 0) + 1 if old.get("vocabulary") == version else 1
    items[key] = {
        "reason": reason,
        "vocabulary": version,
        "attempts": attempts,
        "retry_after": now + min(7 * 86400, 1800 * 2 ** min(attempts, 9)),
    }


def defer_timed_out(
    state: dict[str, Any],
    keys: Iterable[str],
    approved: set[str],
    version: str,
    now: float,
) -> None:
    """Back a timed-out target off like a rejection, keeping its last lesson."""
    for key in keys:
        if key not in approved:
            old = state.get("deferred", {}).get(key, {})
            defer_target(state, key, old.get("reason") or TIMEOUT_LESSON, version, now)


def retry_now(state: dict[str, Any], key: str) -> None:
    """The learner asked for a repair: drop drafts written without their
    feedback and lift the backoff, keeping the lesson for the writer."""

    def other(draft: Any) -> bool:
        try:
            return Target(**draft["target"]).key != key
        except (KeyError, TypeError, ValueError):
            return True

    state["drafts"] = [d for d in state.get("drafts", []) if other(d)]
    if item := state.get("deferred", {}).get(key):
        item.update(attempts=0, retry_after=0)


class Controller:
    def __init__(self) -> None:
        self.timer = QTimer(mw)
        qconnect(self.timer.timeout, self.tick)
        self.stop = Event()
        self.epoch = 0
        self.busy = False
        self.lexicon = None
        self.tables_refreshed = False
        self.path: Path | None = None
        self.folder = Path(__file__).parent.parent / "user_files" / "french-dictionary"
        self.state: dict[str, Any] = {}
        self.status = "Choose a French deck to start."
        self.last_version = ""
        self.request_started_at = 0.0

    def observe(self, event: dict[str, Any]) -> None:
        observe(
            self.state.setdefault("telemetry", {}), event, self.state.get("day", -1)
        )
        self.save()

    def diagnostics_data(self) -> dict[str, Any]:
        return {
            "store": self.state.setdefault("telemetry", {}),
            "state": {
                k: self.state.get(k, 0)
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
                not d.get("repair_note") for d in self.state.get("drafts", [])
            ),
            "buffer": buffer_size(self.config()),
            "deferred": dict(
                sorted(
                    self.state.get("deferred", {}).items(),
                    key=lambda item: item[1].get("retry_after", 0),
                    reverse=True,
                )[:20]
            ),
        }

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
        recover(self.state.setdefault("telemetry", {}), self.state.get("day", -1))
        self.save()
        self.stop.set()
        self.epoch += 1
        self.timer.stop()
        self.busy = False
        self.tables_refreshed = False
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
        import_calls(
            self.state.setdefault("telemetry", {}),
            self.state.get("model_calls", []),
            self.state.get("day", -1),
        )
        recover(self.state.setdefault("telemetry", {}), self.state.get("day", -1))
        self.save()
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
        self.observe(
            {
                "type": "decision",
                "stage": "runtime",
                "outcome": "error",
                "reason": str(exc),
                "where": error_location(exc),
            }
        )
        message = str(exc)[:2000]
        previous = self.state.get("last_error")
        delay = 1800
        if isinstance(exc, TimeoutError):
            attempts = self.state.get("timeout_count", 0) + 1
            self.state.update(
                timeout_count=attempts,
                steady_batches=0,
                retry_batch_size=max(1, self.state.get("active_batch_size", 1) // 2),
            )
            delay = min(900, 120 * 2 ** min(attempts - 1, 3))
        self.request_started_at = 0
        self.state.update(last_error=message, retry_after=time.time() + delay)
        self.status = f"{message}. Automatic retry in {delay // 60} minutes."
        self.save()
        if mw:
            mw.progress.single_shot(
                delay * 1000, lambda: self.tick() if self.valid(epoch) else None
            )
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
        if self.state.get("retry_batch_size"):
            config["batch_size"] = min(
                int(config.get("batch_size", 4)), self.state["retry_batch_size"]
            )
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
        if not self.tables_refreshed:

            def refreshed(_changes: OpChanges) -> None:
                if not self.valid(epoch):
                    return
                self.tables_refreshed = True
                self.busy = False
                self.tick(force=force)

            def unrefreshed(exc: Exception) -> None:
                # Missing tables are cosmetic; record it and keep generating.
                if not self.valid(epoch):
                    return
                self.tables_refreshed = True
                self.busy = False
                self.observe(
                    {
                        "type": "decision",
                        "stage": "tables",
                        "outcome": "error",
                        "reason": str(exc),
                        "where": error_location(exc),
                    }
                )
                self.tick(force=force)

            queued = {
                d["repair_note"]
                for d in self.state.get("drafts") or []
                if isinstance(d, dict) and isinstance(d.get("repair_note"), int)
            }

            def refresh(col: Any) -> OpChanges:
                changes = drop_cue_labels(col, queued)
                changes.MergeFrom(refresh_conjugation_tables(col, lexicon, queued))
                return changes

            CollectionOp(parent=mw, op=refresh).success(refreshed).failure(
                unrefreshed
            ).run_in_background(initiator=self)
            return
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
        reset_day(self.state, current.day)
        self.state.setdefault("telemetry", {})["snapshot"] = {
            "at": time.time(),
            "slots": current.slots,
            "known": len(getattr(current.known, "surfaces", {})),
            "budgets": list(
                {b.deck_id: asdict(b) for b in getattr(current, "budgets", [])}.values()
            ),
        }
        self.last_version = current.known.version
        try:
            drafts = [Draft.read(d) for d in self.state.get("drafts", [])]
        except (KeyError, TypeError, ValueError) as exc:
            self.failed(exc, epoch)
            return
        if drafts and (current.slots or any(d.repair_note for d in drafts)):
            self.apply(drafts, settings, config, lexicon, stop, epoch)
            return
        queued = sum(not d.repair_note for d in drafts)
        needed = preparation_needed(current.slots, queued, config)
        if not needed and not current.repairs:
            self.status = "No French admission slots: pending new cards, reviews or daily limits reserve capacity."
            self.busy = False
            return
        if not refill_ready(
            self.state,
            current.day,
            needed,
            len(current.repairs),
            config,
            time.time(),
        ):
            self.status = (
                "French refill is cooling down; it will retry automatically."
                if config.get("auto_fill", True)
                else "French generation resumes next Anki day."
            )
            self.save()
            self.busy = False
            return
        if not current.known.surfaces:
            self.status = "Study existing French cards to establish context vocabulary."
            self.busy = False
            return
        reserve_batch(self.state)
        count = min(
            max(needed, len(current.repairs)),
            max(1, min(8, int(config.get("batch_size", 4)))),
        )
        self.state["active_batch_size"] = count
        self.state["active_request"] = None
        self.save()
        version = current.known.version
        self.last_version = version
        deferred = {
            key
            for key, item in self.state.get("deferred", {}).items()
            if item.get("vocabulary") == version
            and time.time() < item.get("retry_after", 0)
        }
        self.request_started_at = time.time()
        self.status = f"Selecting up to {count} French targets with {config.get('provider', 'codex')}…"

        retained_drafts = drafts
        retained_keys = {d.target.key for d in retained_drafts}

        def with_repairs(generated_drafts: list[Draft]) -> list[Draft]:
            return [
                replace(
                    d,
                    repair_note=current.repairs[d.target.key][0],
                    old_fingerprint=current.repairs[d.target.key][1],
                )
                if d.target.key in current.repairs
                else d
                for d in generated_drafts
            ]

        def checkpoint(drafts: list[Draft]) -> None:
            prepared = merge_drafts(retained_drafts, with_repairs(drafts))

            def save_approved() -> None:
                if self.valid(epoch):
                    self.state["drafts"] = [d.as_dict() for d in prepared]
                    self.save()

            mw.taskman.run_on_main(save_approved)

        def report(event: dict[str, Any]) -> None:
            def update() -> None:
                if not self.valid(epoch):
                    return
                self.observe(event)
                if event.get("type") != "call":
                    return
                self.state["active_request"] = event
                self.request_started_at = (
                    0 if event["finished"] else event["started_at"]
                )
                self.status = f"{event['phase']} (attempt {event['attempt']}/3)"
                if event["finished"]:
                    calls = self.state.setdefault("model_calls", [])
                    calls.append(event)
                    self.state["model_calls"] = calls[-30:]
                    self.status += f" finished in {event['elapsed_seconds']}s."
                self.save()

            mw.taskman.run_on_main(update)

        attempted_targets: list[Any] = []
        ranked_any = [False]

        def work() -> Any:
            ranking_deferred = {}

            def select(pool: list[Any], size: int) -> list[Any]:
                ranked_any[0] = True
                selected = rank_targets(
                    pool, size, current.known, lexicon, config, stop, progress=report
                )
                if len(selected) < size:
                    selected_keys = {t.key for t in selected}
                    ranking_deferred.update(
                        (t.key, "Target does not match demonstrated difficulty")
                        for t in pool
                        if t.key not in selected_keys
                    )
                return selected

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
                    current.represented
                    | {(d.target.lemma, d.target.pos) for d in retained_drafts},
                    current.keys | retained_keys,
                    self.folder,
                    count - len(repair_targets),
                    deferred,
                    stop,
                    covered=current.covered,
                    covered_forms=current.covered_forms,
                    minimum_level=int(config.get("minimum_vocabulary_level", 0)),
                    ranker=select
                    if vocabulary_floor(
                        lexicon,
                        current.known,
                        int(config.get("minimum_vocabulary_level", 0)),
                    )
                    >= 3
                    else None,
                )
                if needed and len(repair_targets) < count
                else []
            )
            attempted_targets[:] = targets
            if not targets:
                return [], ranking_deferred, 0
            generated_drafts, failures = generate(
                targets,
                current.known,
                lexicon,
                config,
                stop,
                on_approved=checkpoint,
                progress=report,
                observe=report,
                lessons=previous_lessons(
                    self.state.get("deferred", {}), (t.key for t in targets)
                ),
            )
            return (
                with_repairs(generated_drafts),
                {**ranking_deferred, **failures},
                len(targets),
            )

        def generated(future: Any) -> None:
            if not self.valid(epoch):
                return
            try:
                drafts, failures, attempted = future.result()
                record_batch(
                    self.state,
                    attempted,
                    len(drafts),
                    config,
                    time.time(),
                    spent_model_calls=ranked_any[0],
                )
                self.observe(
                    {"type": "batch", "attempted": attempted, "approved": len(drafts)}
                )
                drafts = merge_drafts(retained_drafts, drafts)
                if attempted:
                    self.state["timeout_count"] = 0
                    self.state["steady_batches"] = (
                        self.state.get("steady_batches", 0) + 1
                    )
                    if self.state["steady_batches"] >= 2:
                        self.state.pop("retry_batch_size", None)
                for key, reason in failures.items():
                    defer_target(self.state, key, reason, version, time.time())
                self.state.update(
                    drafts=[d.as_dict() for d in drafts], last_error="", retry_after=0
                )
                self.save()
                if drafts:
                    self.apply(drafts, settings, config, lexicon, stop, epoch)
                else:
                    self.busy = False
                    self.status = "French targets deferred; retries follow vocabulary growth or backoff."
                    if attempted:
                        mw.progress.single_shot(5000, self.tick)
            except Exception as exc:
                prepared = [Draft.read(d) for d in self.state.get("drafts", [])]
                if attempted_targets:
                    self.observe(
                        {
                            "type": "batch",
                            "attempted": len(attempted_targets),
                            "approved": sum(
                                d.target.key not in retained_keys for d in prepared
                            ),
                            "outcome": "error",
                        }
                    )
                    record_batch(
                        self.state,
                        len(attempted_targets),
                        sum(d.target.key not in retained_keys for d in prepared),
                        config,
                        time.time(),
                    )
                if isinstance(exc, CallTimeout):
                    # Otherwise the same targets rebuild the same prompt every retry.
                    defer_timed_out(
                        self.state,
                        exc.keys,
                        {d.target.key for d in prepared}
                        | {d.target.key for d in getattr(exc, "drafts", [])},
                        version,
                        time.time(),
                    )
                self.failed(exc, epoch)
                if prepared:
                    self.busy = True
                    self.apply(prepared, settings, config, lexicon, stop, epoch)

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
            self.observe(
                {
                    "type": "admission",
                    "notes": len(result.admitted) - result.repaired_notes,
                    "cards": result.added_cards,
                    "updates": result.repaired_notes,
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
            if result.admitted:
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
        action = menu.addAction("Fix this sentence...")
        qconnect(action.triggered, lambda: self.feedback_dialog(note_id))

    def feedback_dialog(self, note_id: int) -> None:
        if not mw or not mw.col:
            return
        target = mw.col.get_note(NoteId(note_id))
        repairs = repair_count(target)
        answer = str(json_field(target["Source"]).get("answer", "")) or "it"
        dialog = QDialog(mw)
        dialog.setWindowTitle("Fix this sentence")
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "What's wrong with it? The new sentence must fix it, and later checks keep "
            "your feedback. Leave everything blank for a different sentence."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        if repairs >= MAX_REPAIRS:
            warning = QLabel(
                f"This sentence has been replaced {repairs} times. It will now be "
                f"tagged {MANUAL_TAG} for you to edit instead."
            )
            warning.setWordWrap(True)
            layout.addWidget(warning)
        boxes = {}
        for key, label in FEEDBACK_REASONS.items():
            boxes[key] = QCheckBox(label)
            layout.addWidget(boxes[key])
        form = QFormLayout()
        avoid = QLineEdit()
        avoid.setPlaceholderText("e.g. pourtant enfin")
        form.addRow("Words to avoid", avoid)
        confused = QLineEdit()
        confused.setPlaceholderText("Shows both words side by side")
        form.addRow(f"I'm confusing {answer} with", confused)
        layout.addLayout(form)
        note = QTextEdit()
        note.setAcceptRichText(False)
        note.setPlaceholderText("Anything else")
        note.setMaximumHeight(80)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        qconnect(buttons.accepted, dialog.accept)
        qconnect(buttons.rejected, dialog.reject)
        layout.addWidget(buttons)
        if not dialog.exec():
            return
        self.submit_feedback(
            note_id,
            reasons=tuple(key for key, box in boxes.items() if box.isChecked()),
            note=note.toPlainText(),
            answered=confused.text(),
            avoid_words=tuple(
                w for w in re.split(r"[\s,;]+", avoid.text()) if w.strip()
            ),
        )

    def submit_feedback(self, note_id: int, **values: Any) -> None:
        key = str(json_field(mw.col.get_note(NoteId(note_id))["Source"]).get("key", ""))

        def done(result: Any) -> None:
            retry_now(self.state, key)
            self.save()
            if result.handed_over:
                tooltip(f"Tagged {MANUAL_TAG} for you to edit.")
            else:
                self.tick(force=True)

        CollectionOp(
            parent=mw, op=lambda col: record_feedback(col, note_id, **values)
        ).success(done).run_in_background(initiator=self)

    def status_text(self) -> str:
        text = self.status
        started = getattr(self, "request_started_at", 0)
        if self.busy and started:
            text += f" ({max(0, int(time.time() - started))}s elapsed)"
        prepared = len(self.state.get("drafts", []))
        if self.busy and prepared:
            text += f"; {prepared} approved card(s) saved."
        return text

    def settings_dialog(self) -> None:
        if not mw or not mw.col:
            return
        settings = mw.col.get_config(SETTINGS, {})
        config = self.config()
        dialog = QDialog(mw)
        dialog.setWindowTitle("French Frontier")
        dialog.resize(850, 650)
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
        status = QLabel(self.status_text())
        status.setWordWrap(True)
        layout.addWidget(status)
        diagnostics = Diagnostics(dialog, self.diagnostics_data)
        layout.addWidget(diagnostics)
        refresh = QTimer(dialog)

        def refresh_status() -> None:
            status.setText(self.status_text())
            diagnostics.refresh()
            check.setEnabled(not self.busy)

        qconnect(refresh.timeout, refresh_status)
        refresh.start(1000)
        check = QPushButton("Check now")
        check.setEnabled(not self.busy)
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
