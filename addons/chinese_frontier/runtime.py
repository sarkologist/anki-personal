# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import re
import time
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from threading import Event
from typing import Any

from anki.cards import CardId
from anki.collection import OpChanges
from aqt import gui_hooks, mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import QAction, QTimer, qconnect
from aqt.utils import showInfo, tooltip

from . import categories, panels
from .attention import EDIT, STALLED, Attention, rejections
from .attention_ui import AttentionNotice
from .collection import (
    HOLD_TAG,
    KEY_PREFIX,
    MAX_REPAIRS,
    SETTINGS_KEY,
    STALE_SOURCE,
    AdmissionResult,
    Snapshot,
    admit,
    adopted_original,
    cancel_fix,
    dismiss_handover,
    fix_target_for_card,
    initial_scope_settings,
    is_generated,
    needs_attention,
    pending_fixes,
    record_feedback,
    release_hold,
    repair_count,
    request_fix,
    restore_original,
    snapshot,
    upgrade_presentation,
)
from .core import FEEDBACK_REASONS
from .diagnostics import Diagnostics
from .generation import GenerationResult, generate
from .lexicon import Lexicon
from .panels import Panel, field
from .preparation import buffer_size, preparation_needed, previous_lessons, read_lesson
from .providers import (
    CALL_BUDGET,
    CallBudget,
    CallBudgetExhausted,
    CallTimeout,
    GenerationStopped,
)
from .refill import calls_left
from .state import State, vocabulary_version
from .telemetry import error_location, import_calls, observe, recover

MODULE = __name__.split(".")[0]
PROVIDERS = [
    {"value": "codex", "label": "Codex subscription"},
    {"value": "claude", "label": "Claude subscription"},
]


def resolve_attention(col: Any, item: Attention) -> OpChanges:
    if item.kind == EDIT:
        return dismiss_handover(col, item.note_id)
    if item.kind == STALLED:
        return cancel_fix(col, item.card_id)
    return release_hold(col, item.note_id)


class Controller:
    notice: AttentionNotice | None = None
    settings_panel: Panel | None = None

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
            "feedback": categories.overview_text(mw.col) if mw and mw.col else "",
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

    def attention(self) -> list[Attention]:
        assert mw and mw.col
        settings = mw.col.get_config(SETTINGS_KEY, {})
        return needs_attention(
            mw.col,
            self.last_snapshot,
            rejections(self.state.deferred),
            bool(settings) and not settings.get("enabled"),
        )

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
        if self.notice:
            self.notice.refresh()
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
        if self.notice:
            self.notice.reset()

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
        if isinstance(exc, CallBudgetExhausted):
            self.status = "Today's model call budget is used; resumes next Anki day."
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
        epoch = self.epoch
        config = self.config()
        if not force and categories.classify_in_background(
            config, self.stop, self.observe, lambda: self.classified(epoch)
        ):
            self.busy = True
            return
        self.busy = True
        QueryOp(
            parent=mw,
            op=lambda col: snapshot(col, settings, config),
            success=lambda result: self.scanned(result, settings, config, epoch),
        ).failure(lambda exc: self.failed(exc, epoch)).run_in_background()

    def classified(self, epoch: int) -> None:
        if self.valid(epoch):
            self.busy = False
            self.tick()

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
        if self.notice:
            self.notice.refresh()
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
        if not needed and not current.repairs and not current.fixes:
            self.status = "No admission slots: today's limits, pending new cards, or reviews reserve the capacity."
            self.busy = False
            return
        if not self.state.can_refill(
            needed, len(current.repairs) + len(current.fixes), config, time.time()
        ):
            self.save_state()
            self.busy = False
            if self.state.daily_budget_used(config):
                # The poll timer notices the next Anki day; waking every few
                # seconds would only rescan the collection.
                self.status = (
                    "Today's generation budget is used; resumes next Anki day."
                )
                return
            self.status = "Chinese refill is cooling down; it will retry automatically."
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
            repairing = source.key in current.repairs or source.key in current.fixes
            if not repairing and any(
                reservations.get(budget.deck_id, 0)
                >= current.budget_slots([budget]) + ahead
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
                self.status = "Remaining targets are deferred; each retries when its backoff ends."
            else:
                self.status = "All eligible suspended words are represented."
            self.state.pause_refill(config, time.time())
            self.save_state()
            self.schedule_next()
            return
        self.state.reserve_batch()
        self.save_state()  # Reserve the generation attempt before launching the CLI.
        config = {
            **config,
            CALL_BUDGET: CallBudget(calls_left(vars(self.state), config)),
        }
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
                familiar=getattr(current, "familiar", ()),
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
            if isinstance(exc, CallTimeout):
                # Otherwise the same targets rebuild the same prompt every retry.
                self.state.defer_timed_out(
                    exc.keys, {d.source.key for d in self.state.drafts}, version
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
            # A stale draft says nothing about the target; regenerate it promptly.
            if reason != STALE_SOURCE:
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
        if not mw or not mw.col or not reviewer.card:
            return
        card_id = reviewer.card.id
        note = reviewer.card.note()
        if is_generated(note):
            action = menu.addAction("Fix this sentence...")
            qconnect(action.triggered, lambda: self.feedback_dialog(card_id))
            if adopted_original(note):
                restore = menu.addAction("Restore the original card")
                qconnect(restore.triggered, lambda: self.restore(note.id))
            return
        target = fix_target_for_card(mw.col, card_id, self.config())
        if target is None:
            return
        action = menu.addAction(
            "Fix this sentence..."
            if target.cloze
            else "Make a sentence for this word..."
        )
        qconnect(action.triggered, lambda: self.fix_dialog(card_id))

    def fix_dialog(self, card_id: int) -> None:
        """The dictionary turns a lone clozed character into its word, if cached."""
        folder = self.dictionary_folder

        def load(_col: Any) -> Lexicon | None:
            if not (folder / "cedict-headwords.txt").exists():
                return None
            try:
                return Lexicon.load(folder)
            except ValueError:
                return None

        QueryOp(
            parent=mw,
            op=load,
            success=lambda lexicon: self.feedback_dialog(card_id, lexicon),
        ).without_collection().run_in_background()

    def restore(self, note_id: int) -> None:
        CollectionOp(parent=mw, op=lambda col: restore_original(col, note_id)).success(
            lambda _changes: tooltip("Your original card is back in review.")
        ).run_in_background(initiator=self)

    def feedback_dialog(self, card_id: int, lexicon: Lexicon | None = None) -> None:
        if not mw or not mw.col:
            return
        card = mw.col.get_card(CardId(card_id))
        target = card.note()
        generated = is_generated(target)
        fix = (
            None
            if generated
            else fix_target_for_card(mw.col, card_id, self.config(), lexicon)
        )
        if not generated and fix is None:
            return
        repairs = repair_count(target) if generated else 0
        key = next(
            (t[len(KEY_PREFIX) :] for t in target.tags if t.startswith(KEY_PREFIX)), ""
        )
        note_id = target.id
        body = [
            field(
                "text",
                text="What's wrong with it? The card is suspended until a new sentence "
                "fixes it, and later checks keep your feedback. Leave everything blank "
                "for a different sentence."
                if generated
                else "This card is suspended now and replaced by a new sentence card, "
                "with its schedule, once a sentence passes review. Your note is not "
                "edited; its other cards keep their sentence.",
            )
        ]
        if repairs >= MAX_REPAIRS:
            body.append(
                field(
                    "text",
                    text=f"This sentence has been replaced {repairs} times. It will "
                    "now be tagged chinese-frontier::manual for you to edit instead.",
                )
            )
        if fix is not None:
            words = list(fix.words)
            body += [
                field(
                    "input",
                    "word",
                    label="Target",
                    items=[{"value": word, "label": word} for word in words],
                    value=words[0] if words else "",
                ),
                field("input", "meaning", label="Meaning", value=fix.meaning),
            ]
        boxes, edits, learnt = categories.learnt_fields(mw.col)
        body += [
            *(
                field("checkbox", f"reason.{reason}", label=label)
                for reason, label in FEEDBACK_REASONS.items()
            ),
            *boxes,
            field(
                "input", "avoid", label="Words to avoid", placeholder="e.g. 认为 打算"
            ),
            field(
                "input",
                "answered",
                label="I answered",
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

        def message(message: dict[str, Any]) -> None:
            if message.get("name") == "cancel":
                panel.close()
            if message.get("name") != "ok" or not mw or not mw.col:
                return
            panel.close()
            values = message.get("values", {})
            extra, learnt_values = learnt(values)
            reasons = (
                *(r for r in FEEDBACK_REASONS if values.get(f"reason.{r}") is True),
                *extra,
            )
            avoid = str(values.get("avoid") or "").strip()
            words = tuple(re.split(r"[\s,，、;；]+", avoid) if avoid else ())
            text = str(values.get("note") or "")
            word = str(values.get("answered") or "")
            if fix is not None:
                self.submit_fix(
                    card_id,
                    word=str(values.get("word") or ""),
                    definition=str(values.get("meaning") or ""),
                    reasons=reasons,
                    note=text,
                    avoid_words=words,
                    answered=word,
                    values=learnt_values,
                )
                return

            def done(result: Any) -> None:
                self.state.retry_now(key)
                self.save_state()
                if result.handed_over:
                    tooltip("Tagged chinese-frontier::manual for you to edit.")
                else:
                    self.tick(force=True)

            CollectionOp(
                parent=mw,
                op=lambda col: record_feedback(
                    col,
                    note_id,
                    reasons=reasons,
                    note=text,
                    avoid_words=words,
                    answered=word,
                    values=learnt_values,
                ),
            ).success(done).run_in_background(initiator=self)

        panel = panels.open_panel(
            "Fix this sentence", message, width=520, height=560 if fix else 500
        )
        panel.render(*body, submit="ok")

    def submit_fix(self, card_id: int, **values: Any) -> None:
        config = self.config()

        def done(result: Any) -> None:
            self.state.retry_now(result.key)
            self.save_state()
            tooltip("Suspended; a new sentence card will take over its schedule.")
            self.tick(force=True)

        CollectionOp(
            parent=mw, op=lambda col: request_fix(col, card_id, config=config, **values)
        ).success(done).run_in_background(initiator=self)

    def settings_dialog(self) -> None:
        if not mw or not mw.col:
            return
        if self.settings_panel:
            self.settings_panel.close()
        settings = mw.col.get_config(SETTINGS_KEY, {})
        config = self.config()
        decks: list[dict[str, Any]] = [
            {"value": int(item.id), "label": item.name}
            for item in mw.col.decks.all_names_and_ids(include_filtered=False)
        ]
        ids = [deck["value"] for deck in decks]
        selected = int(settings.get("deck_id", 0))
        if selected not in ids:
            preferred = [
                deck["value"]
                for deck in decks
                if deck["label"].casefold()
                in ("chinese", "中文", "汉语", "漢語", "mandarin")
            ]
            selected = preferred[0] if len(preferred) == 1 else 0
        provider = config.get("provider", "codex")
        if provider not in [p["value"] for p in PROVIDERS]:
            provider = "codex"
        diagnostics = Diagnostics(self.diagnostics_data)

        def status_text() -> str:
            text = self.status
            if recurring := categories.status_line(mw.col):
                text += "\n" + recurring
            if current := self.last_snapshot:
                text += f"\n\nEstablished vocabulary: {len(current.known)} words\nEligible sources: {len(current.sources)}\nPrepared sentences: {len(self.state.drafts)}\nDeferred targets: {len(self.state.deferred)}\nHeld for repair (tag {HOLD_TAG}): {len(mw.col.find_notes(f'tag:{HOLD_TAG}'))}"
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
            return text

        def fixes() -> dict[str, dict[str, Any]]:
            items = []
            for item in pending_fixes(mw.col):
                days = max(0, int((time.time() - item.requested_at) // 86400))
                text = f"{item.word}: waiting {days} d"
                if item.problem:
                    text += f"; stalled, {item.problem}"
                elif reason := self.state.deferred.get(item.key, {}).get("reason"):
                    text += "; deferred: " + str(read_lesson(reason).get("reason", ""))
                if item.collision:
                    text += "; another generated card tests this word"
                items.append({"value": item.card_id, "label": text})
            hidden = {"hidden": not items}
            return {
                "fixes.label": hidden,
                "fixes": {"items": items, **hidden},
                "fixes.cancel": hidden,
            }

        def checked(values: dict[str, Any], name: str) -> list[int]:
            chosen = values.get(name) or []
            return [deck_id for deck_id in ids if deck_id in chosen]

        def save(values: dict[str, Any]) -> None:
            deck_id = values.get("deck")
            if not isinstance(deck_id, int) or deck_id not in ids:
                showInfo("Choose your Chinese deck first.")
                return
            new_settings = {
                **settings,
                "deck_id": deck_id,
                "enabled": values.get("enabled") is True,
                "source_deck_ids": checked(values, "sources"),
                "vocabulary_deck_ids": checked(values, "vocabulary"),
            }
            chosen = values.get("provider")
            chosen = chosen if chosen in [p["value"] for p in PROVIDERS] else "codex"
            config["provider"] = chosen
            config[f"{chosen}_model"] = str(values.get("model") or "").strip()
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
            panel.update({"status": {"value": status_text()}, **diagnostics.fields()})
            mw.progress.single_shot(1000, refresh)

        def closed() -> None:
            if self.settings_panel is panel:
                self.settings_panel = None

        panel = self.settings_panel = panels.open_panel(
            "Chinese Frontier", message, width=850, height=780, on_closed=closed
        )
        shown = fixes()
        panel.render(
            field(
                "text",
                text="Replace suspended single-word cards with one-word sentence "
                "clozes.\nOriginal notes stay suspended; your daily limits control "
                "introductions.",
            ),
            field(
                "checkbox",
                "enabled",
                label="Automatically generate and introduce sentence cards",
                value=bool(settings.get("enabled", True)),
            ),
            field(
                "select",
                "deck",
                label="Chinese source deck",
                items=[{"value": 0, "label": "Choose a deck…"}, *decks],
                value=selected,
            ),
            field(
                "checklist",
                "sources",
                label="Extra sources → chosen deck",
                tooltip="Suspended word targets from these decks become new cards in the chosen Chinese deck. These extra sources do not establish context vocabulary.",
                items=decks,
                value=[i for i in ids if i in settings.get("source_deck_ids", [])],
                height=110,
            ),
            field(
                "checklist",
                "vocabulary",
                label="Extra context vocabulary",
                tooltip="Only established live cards supply context; suspended cards here are not converted.",
                items=decks,
                value=[i for i in ids if i in settings.get("vocabulary_deck_ids", [])],
                height=110,
            ),
            field(
                "select",
                "provider",
                label="Sentence generator",
                items=PROVIDERS,
                value=provider,
                notify=True,
            ),
            field(
                "input",
                "model",
                label="Model (optional)",
                placeholder="Use the CLI default model",
                value=str(config.get(f"{provider}_model", "")),
            ),
            field("readonly", "status", value=status_text(), height=160),
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
            field("button", "check", label="Check now"),
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
    action = QAction("Chinese Frontier…", mw)
    qconnect(action.triggered, controller.settings_dialog)
    mw.form.menuTools.addAction(action)
    mw.addonManager.setConfigAction(MODULE, controller.settings_dialog)
    controller.notice = AttentionNotice(
        "Chinese",
        controller.attention,
        resolve_attention,
        lambda: bool(mw.col.get_config(SETTINGS_KEY, {})),
    )
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
