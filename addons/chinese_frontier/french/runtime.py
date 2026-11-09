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
from typing import Any, Callable

from anki.collection import OpChanges
from anki.notes import NoteId
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import QAction, QTimer, qconnect
from aqt.utils import tooltip

from .. import categories, panels
from ..attention import EDIT, STALLED, Attention, rejections
from ..attention_ui import AttentionNotice
from ..collection import MAX_REPAIRS
from ..core import FEEDBACK_REASONS
from ..diagnostics import Diagnostics
from ..panels import field
from ..preparation import (
    TIMEOUT_LESSON,
    buffer_size,
    preparation_needed,
    reviewed_lessons,
)
from ..providers import (
    CALL_BUDGET,
    CallBudget,
    CallBudgetExhausted,
    CallTimeout,
    GenerationStopped,
)
from ..refill import (
    calls_left,
    daily_budget_used,
    record_batch,
    refill_ready,
    reserve_batch,
    reset_day,
)
from ..telemetry import error_location, import_calls, observe, recover
from .collection import (
    HOLD_TAG,
    MANUAL_TAG,
    SETTINGS,
    Admission,
    Snapshot,
    admit,
    adopted_original,
    cancel_fix,
    dismiss_handover,
    drop_cue_labels,
    fix_readings,
    fix_target_for_card,
    is_generated,
    json_field,
    needs_attention,
    pending_fixes,
    record_feedback,
    refresh_conjugation_tables,
    release_hold,
    repair_count,
    request_fix,
    restore_original,
    snapshot,
)
from .core import POLICY, Target
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


PROVIDERS = [
    {"value": "codex", "label": "Codex subscription"},
    {"value": "claude", "label": "Claude subscription"},
]


def defer_target(
    state: dict[str, Any], key: str, reason: str, version: str, now: float
) -> None:
    items = state.setdefault("deferred", {})
    # Attempts survive vocabulary changes, which happen whenever a card
    # graduates; resetting them kept every deferred target near a 1 h backoff.
    attempts = items.get(key, {}).get("attempts", 0) + 1
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


def resolve_attention(col: Any, item: Attention) -> OpChanges:
    if item.kind == EDIT:
        return dismiss_handover(col, item.note_id)
    if item.kind == STALLED:
        return cancel_fix(col, item.card_id)
    return release_hold(col, item.note_id)


class Controller:
    notice: AttentionNotice | None = None
    last_snapshot: Snapshot | None = None
    settings_panel: panels.Panel | None = None

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
            "feedback": categories.overview_text(mw.col) if mw and mw.col else "",
            "busy": self.busy,
            "prepared": sum(
                not (d.get("repair_note") or d.get("fix_card"))
                for d in self.state.get("drafts", [])
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
        self.last_snapshot = None
        if self.notice:
            self.notice.reset()

    def attention(self) -> list[Attention]:
        assert mw and mw.col
        settings = mw.col.get_config(SETTINGS, {})
        return needs_attention(
            mw.col,
            self.last_snapshot,
            rejections(self.state.get("deferred", {})),
            bool(settings) and not settings.get("enabled"),
        )

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
        if self.notice:
            self.notice.refresh()
        self.timer.start(max(30, int(config.get("poll_seconds", 300))) * 1000)
        mw.progress.single_shot(1500, self.tick)

    def failed(self, exc: Exception, epoch: int) -> None:
        if not self.valid(epoch):
            return
        self.busy = False
        if isinstance(exc, CallBudgetExhausted):
            self.status = "French model call budget is used; resumes next Anki day."
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
        epoch = self.epoch
        config = self.config()
        if not force and categories.classify_in_background(
            config, self.stop, self.observe, lambda: self.classified(epoch)
        ):
            self.busy = True
            return
        self.busy = True
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
        self.last_snapshot = current
        if self.notice:
            self.notice.refresh()
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
        # Fixes replace the learner's cards, so like repairs they need no slot.
        repairs = {**current.repairs, **current.fixes}
        if drafts and (
            current.slots or any(d.repair_note or d.fix_card for d in drafts)
        ):
            self.apply(drafts, settings, config, lexicon, stop, epoch)
            return
        queued = sum(not (d.repair_note or d.fix_card) for d in drafts)
        needed = preparation_needed(current.slots, queued, config)
        if not needed and not repairs:
            self.status = "No French admission slots: pending new cards, reviews or daily limits reserve capacity."
            self.busy = False
            return
        if not refill_ready(
            self.state,
            current.day,
            needed,
            len(repairs),
            config,
            time.time(),
        ):
            self.status = (
                "French generation resumes next Anki day."
                if daily_budget_used(self.state, config)
                else "French refill is cooling down; it will retry automatically."
            )
            self.save()
            self.busy = False
            return
        if not current.known.surfaces:
            self.status = "Study existing French cards to establish context vocabulary."
            self.busy = False
            return
        reserve_batch(self.state)
        config = {**config, CALL_BUDGET: CallBudget(calls_left(self.state, config))}
        count = min(
            max(needed, len(repairs)),
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
            if time.time() < item.get("retry_after", 0)
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
                else replace(
                    d,
                    fix_card=current.fixes[d.target.key][0],
                    old_fingerprint=current.fixes[d.target.key][1],
                )
                if d.target.key in current.fixes
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

            repair_targets = [
                item[2] for key, item in repairs.items() if key not in deferred
            ][:count]
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
                lessons=reviewed_lessons(
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
                    self.status = (
                        "French targets deferred; each retries when its backoff ends."
                    )
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
        if not mw or not mw.col or not reviewer.card:
            return
        card_id = int(reviewer.card.id)
        note = reviewer.card.note()
        note_id = int(note.id)
        if is_generated(note):
            action = menu.addAction("Fix this sentence...")
            qconnect(action.triggered, lambda: self.feedback_dialog(note_id))
            if adopted_original(note):
                restore = menu.addAction("Restore the original card")
                qconnect(restore.triggered, lambda: self.restore(note_id))
        elif fix_target_for_card(mw.col, card_id):
            action = menu.addAction("Fix this sentence...")
            qconnect(action.triggered, lambda: self.fix_dialog(card_id))

    def restore(self, note_id: int) -> None:
        CollectionOp(parent=mw, op=lambda col: restore_original(col, note_id)).success(
            lambda _changes: tooltip("Your original card is back in review.")
        ).run_in_background(initiator=self)

    def fix_dialog(self, card_id: int) -> None:
        """Fixing the learner's own card needs the dictionary for its lemma."""
        if self.lexicon is not None:
            self.open_fix_dialog(card_id, self.lexicon)
            return

        def loaded(future: Any) -> None:
            try:
                self.lexicon = future.result()
            except Exception as exc:
                tooltip(f"French dictionary unavailable: {exc}")
                return
            self.open_fix_dialog(card_id, self.lexicon)

        mw.taskman.run_in_background(
            lambda: Lexicon.load(self.folder), loaded, uses_collection=False
        )

    def open_fix_dialog(self, card_id: int, lexicon: Lexicon) -> None:
        if not mw or not mw.col or not (fix := fix_target_for_card(mw.col, card_id)):
            return
        readings = fix_readings(lexicon, fix.answer)
        labels = [f"{word} ({pos})" for word, pos in readings]
        form, read = self.feedback_form(fix.answer)

        def submit(values: dict[str, Any]) -> None:
            text = str(values.get("lemma") or "")
            # A typed lemma has no part of speech.
            word, pos = next(
                (reading for reading, label in zip(readings, labels) if label == text),
                (text.split(" (", 1)[0], ""),
            )
            definition = str(values.get("meaning") or "")
            feedback = read(values)

            def done(result: Any) -> None:
                retry_now(self.state, result.key)
                self.save()
                self.tick(force=True)

            CollectionOp(
                parent=mw,
                op=lambda col: request_fix(
                    col,
                    card_id,
                    lemma=word,
                    pos=pos,
                    definition=definition,
                    lexicon=lexicon,
                    **feedback,
                ),
            ).success(done).run_in_background(initiator=self)

        self.feedback_panel(
            [
                field(
                    "text",
                    text="This card is suspended now and replaced by a new sentence "
                    "card, with its schedule, once a sentence passes review. Your "
                    "note is not edited.",
                ),
                field("text", text=f"{fix.sentence}\n{fix.translation}".strip()),
                field(
                    "input",
                    "lemma",
                    label="Target",
                    items=[{"value": label, "label": label} for label in labels],
                    value=labels[0] if labels else "",
                ),
                field(
                    "input",
                    "meaning",
                    label="Meaning",
                    placeholder=f"What {fix.answer} means here, e.g. to see",
                ),
                *form,
            ],
            submit,
        )

    def feedback_dialog(self, note_id: int) -> None:
        if not mw or not mw.col:
            return
        target = mw.col.get_note(NoteId(note_id))
        repairs = repair_count(target)
        answer = str(json_field(target["Source"]).get("answer", "")) or "it"
        body = [
            field(
                "text",
                text="What's wrong with it? The card is suspended until a new sentence "
                "fixes it, and later checks keep your feedback. Leave everything blank "
                "for a different sentence.",
            )
        ]
        if repairs >= MAX_REPAIRS:
            body.append(
                field(
                    "text",
                    text=f"This sentence has been replaced {repairs} times. It will "
                    f"now be tagged {MANUAL_TAG} for you to edit instead.",
                )
            )
        form, read = self.feedback_form(answer)
        self.feedback_panel(
            body + form, lambda values: self.submit_feedback(note_id, **read(values))
        )

    def feedback_form(
        self, answer: str
    ) -> tuple[list[dict[str, Any]], Callable[[dict[str, Any]], dict[str, Any]]]:
        """The learner's complaints: the fields, and a reader for their answers."""
        boxes, edits, learnt = categories.learnt_fields(mw.col)
        fields = [
            *(
                field("checkbox", f"reason.{key}", label=label)
                for key, label in FEEDBACK_REASONS.items()
            ),
            *boxes,
            field(
                "input",
                "avoid",
                label="Words to avoid",
                placeholder="e.g. pourtant enfin",
            ),
            field(
                "input",
                "answered",
                label=f"I'm confusing {answer} with",
                placeholder="The new sentence will rule it out",
            ),
            *edits,
            field("textarea", "note", placeholder="Anything else", rows=3),
            field(
                "row",
                align="end",
                body=[
                    field("button", "cancel", label="Cancel"),
                    field("button", "ok", label="OK"),
                ],
            ),
        ]

        def read(values: dict[str, Any]) -> dict[str, Any]:
            extra, learnt_values = learnt(values)
            return {
                "reasons": (
                    *(k for k in FEEDBACK_REASONS if values.get(f"reason.{k}") is True),
                    *extra,
                ),
                "values": learnt_values,
                "note": str(values.get("note") or ""),
                "answered": str(values.get("answered") or ""),
                "avoid_words": tuple(
                    w
                    for w in re.split(r"[\s,;]+", str(values.get("avoid") or ""))
                    if w.strip()
                ),
            }

        return fields, read

    def feedback_panel(
        self, body: list[dict[str, Any]], submit: Callable[[dict[str, Any]], None]
    ) -> None:
        """A "Fix this sentence" panel; OK closes it and submits its values."""

        def message(message: dict[str, Any]) -> None:
            if message.get("name") not in ("ok", "cancel"):
                return
            panel.close()
            if message["name"] == "ok" and mw and mw.col:
                submit(message.get("values", {}))

        panel = panels.open_panel("Fix this sentence", message, width=520, height=540)
        panel.render(*body, submit="ok")

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

    def classified(self, epoch: int) -> None:
        if self.valid(epoch):
            self.busy = False
            self.tick()

    def status_text(self) -> str:
        text = self.status
        if mw and mw.col and (recurring := categories.status_line(mw.col)):
            text += "\n" + recurring
        if mw and mw.col and (held := len(mw.col.find_notes(f"tag:{HOLD_TAG}"))):
            text += f"\nHeld for repair (tag {HOLD_TAG}): {held}"
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
        if self.settings_panel:
            self.settings_panel.close()
        settings = mw.col.get_config(SETTINGS, {})
        config = self.config()
        decks: list[dict[str, Any]] = [
            {"value": int(item.id), "label": item.name}
            for item in mw.col.decks.all_names_and_ids(include_filtered=False)
        ]
        ids = [deck["value"] for deck in decks]
        preferred = next(
            (d["value"] for d in decks if d["label"].casefold() == "french"),
            ids[0] if ids else 0,
        )
        selected = settings.get("deck_id", preferred)
        if selected not in ids:
            selected = ids[0] if ids else 0
        provider = config.get("provider", "codex")
        if provider not in [p["value"] for p in PROVIDERS]:
            provider = "codex"
        diagnostics = Diagnostics(self.diagnostics_data)

        def fixes() -> dict[str, dict[str, Any]]:
            items = []
            for item in pending_fixes(mw.col):
                days = max(0, int((time.time() - item.requested_at) // 86400))
                text = f"{item.lemma}: waiting {days} d"
                if item.problem:
                    text += f"; stalled, {item.problem}"
                elif (
                    reason := self.state.get("deferred", {})
                    .get(item.key, {})
                    .get("reason")
                ):
                    text += f"; deferred: {reason}"
                items.append({"value": item.card_id, "label": text})
            hidden = {"hidden": not items}
            return {
                "fixes.label": hidden,
                "fixes": {"items": items, **hidden},
                "fixes.cancel": hidden,
            }

        def save(values: dict[str, Any]) -> None:
            deck_id = values.get("deck")
            if not isinstance(deck_id, int) or deck_id not in ids:
                return
            chosen = values.get("provider")
            chosen = chosen if chosen in [p["value"] for p in PROVIDERS] else "codex"
            base = mw.addonManager.getConfig(MODULE) or {}
            french = dict(base.get("french", {}))
            french.update(provider=chosen)
            french[f"{chosen}_model"] = str(values.get("model") or "").strip()
            base["french"] = french
            mw.addonManager.writeConfig(MODULE, base)
            self.stop.set()
            self.epoch += 1
            self.stop = Event()
            self.busy = False
            saved = {"enabled": values.get("enabled") is True, "deck_id": deck_id}
            CollectionOp(
                parent=mw, op=lambda col: col.set_config(SETTINGS, saved)
            ).success(lambda _r: self.tick(force=True)).run_in_background(
                initiator=self
            )
            panel.close()

        def message(message: dict[str, Any]) -> Any:
            name = str(message.get("name", ""))
            values = message.get("values", {})
            if not mw or not mw.col:
                return None
            if name.startswith("diagnostics."):
                return diagnostics.message(message)
            if categories.settings_message(panel, message):
                return None
            if message.get("type") == "change" and name == "provider":
                model = config.get(f"{values.get('provider')}_model", "")
                return panels.update({"model": {"value": str(model)}})
            if name == "cancel_fix" and isinstance(card_id := values.get("fixes"), int):
                CollectionOp(
                    parent=mw, op=lambda col: cancel_fix(col, card_id)
                ).success(
                    lambda _changes: panel.send(panels.update(fixes()))
                ).run_in_background(initiator=self)
            elif name == "check":
                self.tick(force=True)
            elif name == "save":
                save(values)
            elif name == "close":
                panel.close()
            return None

        def refresh() -> None:
            if panel.closed or not mw or not mw.col:
                return
            panel.update(
                {
                    "status": {"text": self.status_text()},
                    "check": {"disabled": self.busy},
                    **diagnostics.fields(),
                }
            )
            mw.progress.single_shot(1000, refresh)

        def closed() -> None:
            if self.settings_panel is panel:
                self.settings_panel = None

        panel = self.settings_panel = panels.open_panel(
            "French Frontier", message, width=850, height=650, on_closed=closed
        )
        shown = fixes()
        panel.render(
            field(
                "checkbox",
                "enabled",
                label="Automatically introduce French vocabulary and conjugation cards",
                value=bool(settings.get("enabled", True)),
            ),
            field("select", "deck", label="French deck", items=decks, value=selected),
            field(
                "select",
                "provider",
                label="Generator",
                items=PROVIDERS,
                value=provider,
                notify=True,
            ),
            field(
                "input",
                "model",
                label="Model",
                placeholder="Use the CLI default model",
                value=str(config.get(f"{provider}_model", "")),
            ),
            field("text", "status", text=self.status_text()),
            field(
                "text",
                "fixes.label",
                text="Fixes waiting for a sentence",
                **shown["fixes.label"],
            ),
            field("list", "fixes", height=110, **shown["fixes"]),
            field(
                "button",
                "fixes.cancel",
                label="Cancel selected fix (unsuspends your card)",
                action="cancel_fix",
                **shown["fixes.cancel"],
            ),
            *categories.settings_fields(mw.col),
            diagnostics.spec(),
            field("button", "check", label="Check now", disabled=self.busy),
            field(
                "row",
                align="end",
                body=[
                    field("button", "close", label="Close"),
                    field("button", "save", label="Save"),
                ],
            ),
        )
        mw.progress.single_shot(1000, refresh)


_controller: Controller | None = None


def install() -> None:
    global _controller
    if _controller is not None or not mw:
        return
    controller = _controller = Controller()
    action = QAction("French Frontier…", mw)
    qconnect(action.triggered, controller.settings_dialog)
    mw.form.menuTools.addAction(action)
    controller.notice = AttentionNotice(
        "French",
        controller.attention,
        resolve_attention,
        lambda: bool(mw.col.get_config(SETTINGS, {})),
    )
    gui_hooks.profile_did_open.append(controller.opened)
    gui_hooks.profile_will_close.append(controller.closed)
    gui_hooks.sync_did_finish.append(controller.tick)
    gui_hooks.reviewer_will_show_context_menu.append(controller.reviewer_menu)
    gui_hooks.reviewer_did_answer_card.append(
        lambda *_args: mw.progress.single_shot(1500, controller.tick)
    )
    if mw.col:
        controller.opened()
