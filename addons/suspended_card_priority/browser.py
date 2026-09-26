from __future__ import annotations

import html
import threading
from pathlib import Path

import aqt
from anki.collection import BrowserColumns
from anki.errors import NotFoundError
from aqt import gui_hooks
from aqt.browser.table import Column
from aqt.operations import QueryOp
from aqt.qt import QAction, Qt, sip
from aqt.utils import showInfo, showWarning, tooltip

from .core import CardText, ReferenceIndex, load_scores, save_scores, sort_ids
from .scoring import score_batch

KEY = "suspendedCardPriority"
FILE = "suspended-card-priority.json"
_scores: dict[str, dict] = {}
_path: Path | None = None
_stop = threading.Event()
_running = False


def _config() -> dict:
    return aqt.mw.addonManager.getConfig(__name__) or {}


def _load() -> None:
    global _scores, _path
    _path = Path(aqt.mw.pm.profileFolder()) / FILE
    try:
        _scores = load_scores(_path)
    except (ValueError, OSError) as exc:
        _scores = {}
        showWarning(f"Priority scores could not be loaded: {exc}")


def _columns(columns: dict) -> None:
    columns[KEY] = Column(
        key=KEY,
        cards_mode_label="Priority",
        notes_mode_label="Priority (cards only)",
        sorting_cards=BrowserColumns.SORTING_DESCENDING,
        sorting_notes=BrowserColumns.SORTING_NONE,
        alignment=BrowserColumns.ALIGNMENT_CENTER,
        cards_mode_tooltip="Higher scores first. Snapshot estimates; use Priority > Explain for evidence.",
        notes_mode_tooltip="Switch to Cards mode for per-card priorities.",
    )


def _will_search(context) -> None:
    if context.ids is not None or getattr(context.order, "key", None) != KEY:
        return
    context.addon_metadata[KEY] = context.reverse
    context.order = False  # Rust does not know our custom column key.


def _did_search(context) -> None:
    if KEY in context.addon_metadata and not context.browser.table.is_notes_mode():
        valid = {}
        for cid in context.ids:
            value = _current_score(cid)
            if value:
                valid[str(cid)] = value
        context.ids = sort_ids(context.ids, valid, context.addon_metadata[KEY])


def _current_score(cid):
    value = _scores.get(str(cid))
    if not value:
        return None
    try:
        card = aqt.mw.col.get_card(cid)
    except NotFoundError:
        return None
    if card.queue != -1:
        return None
    current = CardText.from_html(cid, card.nid, card.question(), card.answer())
    if current.fingerprint != value["fingerprint"]:
        return dict(value, score=None, stale=True)
    return value


def _row(cid, is_note, row, columns) -> None:
    if KEY not in columns:
        return
    value = _current_score(cid) if not is_note else None
    text = ""
    if value:
        text = (
            "Stale"
            if value.get("stale")
            else ("Review" if value["score"] is None else str(value["score"]))
        )
    row.cells[columns.index(KEY)].text = text


def _show(browser) -> None:
    if browser.table.is_notes_mode():
        showInfo(
            "Switch the Browser to Cards mode to use per-card priorities.",
            parent=browser,
        )
        return
    if browser.table._model.active_column_index(KEY) is None:
        browser.table._on_column_toggled(True, KEY)
    browser.search_for(_config().get("search", "deck:math is:suspended"))
    section = browser.table._model.active_column_index(KEY)
    browser.table._on_sort_column_changed(section, Qt.SortOrder.DescendingOrder)


def _explain(browser) -> None:
    card = browser.table.get_current_card()
    value = _current_score(card.id) if card else None
    if not value:
        showInfo(
            "This card has not been scored. Use Priority > Score next batch.",
            parent=browser,
        )
        return
    if value.get("stale"):
        showInfo(
            "This card changed since it was scored. Use Priority > Re-score selected cards.",
            parent=browser,
        )
        return
    score = value["score"]
    label = "Needs review" if score is None else f"Priority {score}/100"
    evidence = (
        ", ".join(f"cid:{cid}" for cid in value["matches"])
        or "No overlapping target identified in retrieved references."
    )
    showInfo(
        f"<b>{label}</b><p>{html.escape(value['reason'])}</p>"
        f"<p>Conceptual: {value['conceptual']}/4 · Computation: {value['computation']}/4 · "
        f"Overlap: {value['overlap']}/4 · Confidence: {html.escape(value['confidence'])}</p>"
        f"<p>Matching unsuspended cards: {html.escape(evidence)}</p>"
        f"<p>Scored: {html.escape(value.get('scored_at', 'unknown'))}</p>"
        "<p>Snapshot estimate from retrieved references, not an exhaustive novelty guarantee. "
        "Re-score after editing cards or changing which cards are suspended.</p>",
        parent=browser,
        textFormat="rich",
    )


def _matches(browser) -> None:
    card = browser.table.get_current_card()
    matches = _scores.get(str(card.id), {}).get("matches", []) if card else []
    if matches:
        browser.search_for("cid:" + ",".join(map(str, matches)))
    else:
        showInfo(
            "No overlapping reference cards recorded for this card.", parent=browser
        )


def _snapshot(col, query: str, selected: set[int] | None):
    ids = col.find_cards(query)
    if selected is not None:
        ids = [cid for cid in ids if cid in selected]

    def read(cid):
        card = col.get_card(cid)
        return CardText.from_html(cid, card.nid, card.question(), card.answer())

    candidates = [read(cid) for cid in ids]
    references = [read(cid) for cid in col.find_cards("-is:suspended")]
    return candidates, references


def _start(browser, all_batches: bool = False, selected: bool = False) -> None:
    global _running
    if _running:
        showInfo(
            "Priority scoring is already running. Use Priority > Stop scoring to stop it.",
            parent=browser,
        )
        return
    if _path is None:
        _load()
    config = _config()
    batch_size = max(1, min(30, int(config.get("batch_size", 12))))
    selected_ids = set(browser.selected_cards()) if selected else None
    query = browser.current_search().strip()
    query = f"({query}) is:suspended" if query else "is:suspended"
    path = _path
    old_scores = dict(_scores)
    _stop.clear()
    _running = True

    def failure(exc):
        global _running
        _running = False
        if path == _path:
            _load()
            showWarning(str(exc), parent=None if sip.isdeleted(browser) else browser)

    def ready(snapshot):
        if _stop.is_set() or path != _path:
            failure(InterruptedError("Priority scoring stopped."))
            return

        def work():
            candidates, references = snapshot
            index = ReferenceIndex(references)
            pending = [
                c
                for c in candidates
                if selected
                or old_scores.get(str(c.card_id), {}).get("fingerprint")
                != c.fingerprint
                or old_scores.get(str(c.card_id), {}).get("reference_version")
                != index.version
            ]
            if not all_batches:
                pending = pending[:batch_size]
            count = 0
            for start in range(0, len(pending), batch_size):
                if _stop.is_set():
                    break
                batch = pending[start : start + batch_size]
                result = score_batch(batch, index, config, _stop.is_set)
                old_scores.update(result)
                save_scores(path, old_scores)
                count += len(batch)
                current = dict(old_scores)
                completed, total = count, len(pending)
                aqt.mw.taskman.run_on_main(
                    lambda scores=current, n=completed, total=total: publish(
                        scores, n, total
                    )
                )
            return count

        def publish(scores, count, total):
            global _scores
            if path != _path:
                return
            _scores = scores
            if not sip.isdeleted(browser):
                browser.search()
                tooltip(
                    f"Priority scoring: {count}/{total} cards saved.", parent=browser
                )

        def done(future):
            global _running
            _running = False
            if path != _path:
                return
            _load()
            try:
                count = future.result()
                if not sip.isdeleted(browser):
                    browser.search()
                    tooltip(f"Saved priorities for {count} cards.", parent=browser)
            except Exception as exc:
                showWarning(
                    str(exc), parent=None if sip.isdeleted(browser) else browser
                )

        tooltip(
            "Scoring with Codex. You can keep browsing; Priority > Stop scoring cancels the run.",
            period=6000,
            parent=browser,
        )
        aqt.mw.taskman.run_in_background(work, done, uses_collection=False)

    QueryOp(
        parent=browser,
        op=lambda col: _snapshot(col, query, selected_ids),
        success=ready,
    ).failure(failure).with_progress(
        "Reading cards for priority scoring…"
    ).run_in_background()


def _menus(browser) -> None:
    menu = browser.form.menubar.addMenu("Priority")
    for label, callback in [
        ("Show Math priority queue", lambda: _show(browser)),
        ("Score next batch", lambda: _start(browser)),
        (
            "Score all remaining in this search",
            lambda: _start(browser, all_batches=True),
        ),
        (
            "Re-score selected cards",
            lambda: _start(browser, all_batches=True, selected=True),
        ),
        ("Explain current priority", lambda: _explain(browser)),
        ("Show overlapping cards", lambda: _matches(browser)),
        ("Stop scoring", _stop.set),
    ]:
        action = QAction(label, browser)
        action.triggered.connect(lambda _checked=False, cb=callback: cb())
        menu.addAction(action)


def _close() -> None:
    global _path, _scores
    _stop.set()
    _path = None
    _scores = {}


def install() -> None:
    gui_hooks.profile_did_open.append(_load)
    gui_hooks.profile_will_close.append(_close)
    gui_hooks.browser_did_fetch_columns.append(_columns)
    gui_hooks.browser_will_search.append(_will_search)
    gui_hooks.browser_did_search.append(_did_search)
    gui_hooks.browser_did_fetch_row.append(_row)
    gui_hooks.browser_menus_did_init.append(_menus)
