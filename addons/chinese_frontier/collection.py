# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from anki.collection import Collection, OpChanges
from anki.decks import DeckId
from anki.notes import NoteId

from .core import (
    CLOZE,
    EXCLUDE_TAG,
    GENERATED_TAG,
    MODEL_NAME,
    DeckBudget,
    Source,
    admission_slots,
    cloze_words,
    effective_limit,
    fingerprint,
    plain_text,
    successful_days,
    validate_candidate,
)
from .generation import Draft
from .lexicon import Lexicon, is_word

SETTINGS_KEY = "chineseFrontier"
LEGACY_FRONT = '<div class="sentence">{{cloze:Text}}</div>'
TRANSLATED_FRONT = LEGACY_FRONT + '<div class="translation">{{Translation}}</div>'

FIELDS = (
    "Text",
    "Target",
    "Pinyin",
    "Meaning",
    "Translation",
    "Dependencies",
    "Source",
)
KEY_PREFIX = "chinese-frontier::key::"
REPAIR_TAG = "chinese-frontier::repair"


@dataclass
class Snapshot:
    day: int
    known: dict[str, str]
    evidence: dict[str, list[int]]
    sources: list[Source]
    budgets: dict[int, list[DeckBudget]]
    ignore_review_limit: bool
    existing_keys: set[str]
    covered_words: set[str]
    skipped: dict[str, int] = field(default_factory=dict)
    repairs: dict[str, int] = field(default_factory=dict)

    def slots(self, deck_id: int) -> int:
        return admission_slots(
            self.budgets.get(deck_id, []), ignore_review_limit=self.ignore_review_limit
        )


def _ids(ids: set[int]) -> str:
    # Only internal numeric Anki IDs are interpolated into SQL.
    return (
        "(" + ",".join(str(int(value)) for value in sorted(ids)) + ")" if ids else "(0)"
    )


def _descendants(decks: dict[int, dict[str, Any]], deck_id: int) -> set[int]:
    name = decks[deck_id]["name"]
    return {
        did
        for did, deck in decks.items()
        if deck["name"] == name or deck["name"].startswith(name + "::")
    }


def _ancestors(decks: dict[int, dict[str, Any]], deck_id: int) -> list[int]:
    name = decks[deck_id]["name"]
    return [
        did
        for did, deck in decks.items()
        if not deck.get("dyn")
        and (name == deck["name"] or name.startswith(deck["name"] + "::"))
    ]


def _template_tests_field(template: dict[str, Any], field_name: str) -> bool:
    return bool(
        re.search(
            r"\{\{(?:[^{}:]+:)*" + re.escape(field_name) + r"\}\}",
            template.get("qfmt", ""),
        )
    )


def initial_scope_settings(
    col: Collection, config: dict[str, Any]
) -> dict[str, Any] | None:
    """Apply installer scope once, to its exact profile, preserving saved choices."""
    if config.get("initial_collection_path") != col.path:
        return None
    current = col.get_config(SETTINGS_KEY, {})
    updated = dict(current)
    if not updated:
        deck = col.decks.by_name(str(config.get("initial_deck_name", "")))
        if not deck or deck.get("dyn"):
            raise ValueError("Configured Chinese source deck no longer exists")
        updated = {"deck_id": int(deck["id"]), "enabled": True}
    for field, option in (
        ("source_deck_ids", "initial_source_deck_names"),
        ("vocabulary_deck_ids", "initial_vocabulary_deck_names"),
    ):
        names = config.get(option, [])
        if field not in updated and names:
            ids = []
            for name in names:
                deck = col.decks.by_name(str(name))
                if not deck or deck.get("dyn"):
                    raise ValueError(f"Configured extra deck no longer exists: {name}")
                ids.append(int(deck["id"]))
            updated[field] = ids
    return updated if updated != current else None


def snapshot(
    col: Collection, settings: dict[str, Any], config: dict[str, Any]
) -> Snapshot:
    """Read only, on Anki's serialized collection executor."""
    deck_id = int(settings.get("deck_id", 0))
    decks = {int(deck["id"]): deck for deck in col.decks.all()}
    if deck_id not in decks or decks[deck_id].get("dyn"):
        raise ValueError(
            "Select an existing normal Chinese deck in Chinese Frontier settings"
        )
    scope = _descendants(decks, deck_id)
    primary_scope = set(scope)
    for source_deck in settings.get("source_deck_ids", []):
        extra_id = int(source_deck)
        if extra_id not in decks or decks[extra_id].get("dyn"):
            raise ValueError(
                "An additional source deck no longer exists or is filtered"
            )
        scope.update(_descendants(decks, extra_id))
    vocabulary_scope = set(primary_scope)
    for vocabulary_deck in settings.get("vocabulary_deck_ids", []):
        if int(vocabulary_deck) in decks:
            vocabulary_scope.update(_descendants(decks, int(vocabulary_deck)))
    timing = col.sched._timing_today()
    day = timing.days_elapsed
    known: dict[str, str] = {}
    evidence: dict[str, list[int]] = {}
    for word in config.get("known_words", []):
        if not isinstance(word, str) or not is_word(word):
            raise ValueError("known_words must contain whole Chinese words")
        known[word] = "Explicitly marked familiar by the user"
        evidence[word] = []
    skipped: dict[str, int] = {}

    def skip(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    scope_sql = _ids(scope | vocabulary_scope)
    vocabulary_sql = _ids(vocabulary_scope)
    rows = col.db.all(
        f"select c.id,c.nid,c.did,c.odid,c.ord,c.queue,c.type,c.reps,n.mid,n.flds,n.tags "
        f"from cards c join notes n on n.id=c.nid where c.did in {scope_sql} or c.odid in {scope_sql} "
        "order by c.due,c.id"
    )
    review_logs: dict[int, list[tuple[int, int]]] = {}
    # Imported/rescheduled/manual log entries do not demonstrate recall.
    for cid, timestamp, ease in col.db.all(
        "select r.cid,r.id,r.ease from revlog r join cards c on c.id=r.cid "
        f"where (c.did in {vocabulary_sql} or c.odid in {vocabulary_sql}) and r.type in (0,1,2,3) and r.ease between 1 and 4 order by r.id"
    ):
        review_logs.setdefault(cid, []).append((timestamp, ease))
    models = {}
    active_notes = set(
        col.db.list(
            "select distinct nid from cards where queue!=-1 "
            f"and nid in (select nid from cards where did in {scope_sql} or odid in {scope_sql})"
        )
    )
    covered_words: set[str] = set()
    potential: list[Source] = []
    seen_notes: set[int] = set()
    front_field = str(config.get("front_field", "Front"))
    back_field = str(config.get("back_field", "Back"))
    minimum_success_days = max(1, int(config.get("minimum_success_days", 2)))
    for (
        cid,
        nid,
        did,
        odid,
        ordinal,
        queue,
        card_type,
        reps,
        mid,
        joined,
        tag_string,
    ) in rows:
        home = odid or did
        if mid not in models:
            models[mid] = col.models.get(mid)
        model = models[mid]
        if not model:
            continue
        fields = {
            item["name"]: value
            for item, value in zip(model["flds"], joined.split("\x1f"), strict=True)
        }
        tags = tag_string.split()
        repairing = REPAIR_TAG in tags and GENERATED_TAG in tags
        excluded = any(tag.casefold() in ("leech", EXCLUDE_TAG) for tag in tags)
        if excluded:
            if queue == -1 and nid not in seen_notes and home in scope:
                skip("excluded or leech")
                seen_notes.add(nid)
            continue
        if model["type"] == 1:
            template = model["tmpls"][0]
            words = tuple(
                word
                for name, value in fields.items()
                if "{{cloze:" + name + "}}" in template["qfmt"]
                for word in cloze_words(value, ordinal)
            )
            if home in vocabulary_scope and queue != -1 and not repairing:
                # A clozed dictionary entry is not already a sentence card.
                for name, value in fields.items():
                    if "{{cloze:" + name + "}}" not in template["qfmt"]:
                        continue
                    context = CLOZE.sub("", plain_text(value))
                    if sum(is_word(char) for char in context) >= 3:
                        covered_words.update(cloze_words(value, ordinal))
        else:
            template = model["tmpls"][ordinal] if ordinal < len(model["tmpls"]) else {}
            word = plain_text(fields.get(front_field, ""))
            hsk_level = 0
            # Quizlet imports annotate Front with a space and the HSK level.
            # Restrict stripping to HSK-labelled decks/models, not arbitrary text.
            hsk_context = decks[home]["name"] + " " + model["name"]
            if re.search(r"HSK\s*[1-6]", hsk_context, re.IGNORECASE):
                if marker := re.fullmatch(r"([\u3400-\u9fff]+)\s+([1-6])", word):
                    word, hsk_level = marker[1], int(marker[2])
                elif level := re.search(r"HSK\s*([1-6])", model["name"], re.IGNORECASE):
                    hsk_level = int(level[1])
            words = (
                (word,)
                if is_word(word) and _template_tests_field(template, front_field)
                else ()
            )
        meaning = plain_text(fields.get("Meaning", fields.get(back_field, "")))
        if not meaning and model["type"] == 1:
            meaning = plain_text(
                " ".join(
                    CLOZE.sub(lambda match: match[2].split("::", 1)[0], value)
                    for name, value in fields.items()
                    if name in ("Text", "Text2", "Text3", "Text4", "Extra")
                )
            )
        if (
            home in vocabulary_scope
            and queue != -1
            and not repairing
            and reps > 0
            and successful_days(review_logs.get(cid, []), timing.next_day_at)
            >= minimum_success_days
        ):
            for word in words:
                if word not in known:
                    known[word] = meaning
                elif meaning and meaning not in known[word]:
                    known[word] += "; " + meaning
                evidence.setdefault(word, []).append(cid)
        if home not in scope or queue != -1 or nid in seen_notes:
            continue
        if model["type"] != 0 or not words or not meaning:
            skip("not a supported single-word note")
            seen_notes.add(nid)
            continue
        if nid in active_notes or odid:
            skip("active sibling or filtered deck")
            seen_notes.add(nid)
            continue
        seen_notes.add(nid)
        potential.append(
            Source(
                cid,
                nid,
                home if home in primary_scope else deck_id,
                words[0],
                meaning,
                fingerprint(fields, tags),
                hsk_level=hsk_level,
            )
        )
    tagged_notes = col.db.all(
        "select id,tags from notes where tags like ?", "%" + KEY_PREFIX + "%"
    )
    repairs = {
        tag[len(KEY_PREFIX) :]: nid
        for nid, tag_string in tagged_notes
        if REPAIR_TAG in tag_string.split()
        for tag in tag_string.split()
        if tag.startswith(KEY_PREFIX)
    }
    existing_keys = {
        tag[len(KEY_PREFIX) :]
        for _nid, tag_string in tagged_notes
        for tag in tag_string.split()
        if tag.startswith(KEY_PREFIX)
    }
    sources: list[Source] = []
    keys = set(existing_keys)
    # Stable ties retain the old due/card ordering. Explicit HSK metadata is a
    # coarse curriculum proxy; it does not claim to measure individual difficulty.
    potential.sort(
        key=lambda source: (source.key not in repairs, source.hsk_level or 7)
    )
    for source in potential:
        if source.key not in repairs and (
            source.key in keys or source.word in covered_words or source.word in known
        ):
            skip("already represented or duplicate")
            continue
        keys.add(source.key)
        if source.key in repairs:
            old_note = col.get_note(NoteId(repairs[source.key]))
            previous = CLOZE.sub(
                lambda match: match[2].split("::", 1)[0], old_note["Text"]
            )
            source = replace(source, avoid_sentence=plain_text(previous))
        sources.append(source)
    # Reserve capacity through *all* normal ancestors. This intentionally stays
    # within the Chinese parent's budget even when studying a child directly.
    budget_ids = {
        ancestor for source in sources for ancestor in _ancestors(decks, source.deck_id)
    }
    budget_ids.update(_ancestors(decks, deck_id))
    budgets_by_id: dict[int, DeckBudget] = {}
    for budget_id in budget_ids:
        deck = decks[budget_id]
        descendants = _ids(_descendants(decks, budget_id))
        pending = col.db.scalar(
            f"select count(*) from cards where (did in {descendants} or odid in {descendants}) "
            "and type=0 and queue in (0,-2,-3)"
        )
        due_reviews = col.db.scalar(
            f"select count(*) from cards where (did in {descendants} or odid in {descendants}) "
            "and queue in (2,3) and due<=?",
            day,
        )
        conf = col.decks.config_dict_for_deck_id(DeckId(budget_id))
        new_today = deck.get("newToday", [day, 0])
        rev_today = deck.get("revToday", [day, 0])
        budgets_by_id[budget_id] = DeckBudget(
            budget_id,
            effective_limit(deck, conf, "new", day),
            int(new_today[1]) if new_today[0] == day else 0,
            int(pending),
            effective_limit(deck, conf, "review", day),
            int(rev_today[1]) if rev_today[0] == day else 0,
            int(due_reviews),
        )
    budgets = {
        did: [budgets_by_id[ancestor] for ancestor in _ancestors(decks, did)]
        for did in {source.deck_id for source in sources} | {deck_id}
    }
    return Snapshot(
        day,
        known,
        evidence,
        sources,
        budgets,
        bool(col.get_config("newCardsIgnoreReviewLimit", False)),
        existing_keys,
        covered_words,
        skipped,
        repairs,
    )


def ensure_model(col: Collection) -> dict[str, Any]:
    if model := col.models.by_name(MODEL_NAME):
        if (
            model["type"] != 1
            or tuple(item["name"] for item in model["flds"]) != FIELDS
            or len(model["tmpls"]) != 1
        ):
            raise ValueError(
                "Chinese Frontier note type was modified; restore its required fields and cloze template"
            )
        if model["tmpls"][0]["qfmt"] == LEGACY_FRONT:
            model["tmpls"][0]["qfmt"] = TRANSLATED_FRONT
            col.models.update_dict(model)
        return model
    model = col.models.new(MODEL_NAME)
    model["type"] = 1
    for name in FIELDS:
        col.models.add_field(model, col.models.new_field(name))
    template = col.models.new_template("Sentence")
    template["qfmt"] = TRANSLATED_FRONT
    template["afmt"] = (
        '<div class="sentence">{{cloze:Text}}</div><hr id="answer">'
        "<div>{{Target}} · {{Pinyin}}</div><div>{{Meaning}}</div>"
        '<div class="translation">{{Translation}}</div>'
    )
    col.models.add_template(model, template)
    model["css"] = (
        ".card {font-family: sans-serif; font-size: 23px; text-align: center;} "
        ".sentence {font-size: 30px; line-height: 1.6;} .cloze {font-weight: bold; color: #2677bb;} "
        ".translation {margin-top: 1em; font-size: 19px;} .nightMode .cloze {color: #74b8ef;}"
    )
    added = col.models.add_dict(model)
    stored = col.models.get(added.id)
    assert stored is not None
    return stored


def upgrade_presentation(col: Collection) -> OpChanges:
    """Upgrade generated notes without changing card IDs or scheduling."""
    model = col.models.by_name(MODEL_NAME)
    if model is None:
        return OpChanges()
    notes = []
    for nid in col.find_notes(f'"note:{MODEL_NAME}" tag:{GENERATED_TAG}'):
        note = col.get_note(nid)
        text = CLOZE.sub(
            lambda match: "{{c" + match[1] + "::" + match[2].split("::", 1)[0] + "}}",
            note["Text"],
        )
        if text != note["Text"]:
            note["Text"] = text
            notes.append(note)
    if not notes and model["tmpls"][0]["qfmt"] != LEGACY_FRONT:
        return OpChanges()
    undo = col.add_custom_undo_entry("Show translations on Chinese sentence cards")
    ensure_model(col)
    if notes:
        col.update_notes(notes)
    return col.merge_undo_entries(undo)


@dataclass
class AdmissionResult:
    changes: OpChanges
    admitted: list[int]
    remaining: list[Draft]
    rejected: dict[str, str]
    updated: list[int] = field(default_factory=list)


def request_repair(col: Collection, note_id: int) -> OpChanges:
    note = col.get_note(NoteId(note_id))
    if GENERATED_TAG not in note.tags or note.note_type()["name"] != MODEL_NAME:
        raise ValueError(
            "Only generated Chinese Frontier notes can be replaced automatically"
        )
    if REPAIR_TAG not in note.tags:
        note.tags.append(REPAIR_TAG)
    return col.update_note(note)


def admit(
    col: Collection,
    settings: dict[str, Any],
    config: dict[str, Any],
    drafts: list[Draft],
    lexicon: Lexicon,
) -> AdmissionResult:
    """Re-read everything after generation, then write through native undoable APIs."""
    result: AdmissionResult | None = None

    def transaction() -> None:
        nonlocal result
        result = _admit(col, settings, config, drafts, lexicon)

    try:
        col.db.transact(transaction)
    except Exception:
        col.models._clear_cache()
        raise
    assert result is not None
    return result


def _admit(
    col: Collection,
    settings: dict[str, Any],
    config: dict[str, Any],
    drafts: list[Draft],
    lexicon: Lexicon,
) -> AdmissionResult:
    fresh_settings = col.get_config(SETTINGS_KEY, {})
    if not fresh_settings.get("enabled") or fresh_settings.get(
        "deck_id"
    ) != settings.get("deck_id"):
        return AdmissionResult(OpChanges(), [], drafts, {})
    current = snapshot(col, fresh_settings, config)
    sources = {source.card_id: source for source in current.sources}
    remaining: list[Draft] = []
    rejected: dict[str, str] = {}
    admitted: list[int] = []
    updated: list[int] = []
    changes = OpChanges()
    model = None
    undo = None
    reserved: dict[int, int] = {}
    for draft in drafts:
        source = sources.get(draft.source.card_id)
        if (
            source is None
            or source.fingerprint != draft.source.fingerprint
            or source.key != draft.source.key
        ):
            rejected[draft.source.key] = (
                "Source changed, was unsuspended, or is already represented"
            )
            continue
        try:
            valid = validate_candidate(
                draft.candidate, source, set(current.known), lexicon
            )
        except ValueError as exc:
            rejected[source.key] = str(exc)
            continue
        budgets = current.budgets[source.deck_id]
        adjusted = [
            DeckBudget(
                budget.deck_id,
                budget.new_limit,
                budget.introduced,
                budget.pending + reserved.get(budget.deck_id, 0),
                budget.review_limit,
                budget.reviewed,
                budget.due_reviews,
            )
            for budget in budgets
        ]
        repair_id = current.repairs.get(source.key)
        if not repair_id and (
            admission_slots(adjusted, ignore_review_limit=current.ignore_review_limit)
            <= 0
        ):
            remaining.append(draft)
            continue
        if model is None:
            model = ensure_model(col)
            undo = col.add_custom_undo_entry("Introduce Chinese sentence cards")
        note = col.get_note(NoteId(repair_id)) if repair_id else col.new_note(model)
        note["Text"] = valid.text
        note["Target"] = source.word
        note["Pinyin"] = html.escape(valid.candidate.pinyin)
        note["Meaning"] = html.escape(valid.candidate.hint or source.definition)
        note["Translation"] = html.escape(valid.candidate.translation)
        note["Dependencies"] = html.escape(
            json.dumps(
                {word: current.evidence.get(word, []) for word in valid.dependencies},
                ensure_ascii=False,
            )
        )
        note["Source"] = html.escape(
            json.dumps(
                {
                    "card_id": source.card_id,
                    "note_id": source.note_id,
                    "key": source.key,
                    "word": source.word,
                    "definition": source.definition,
                    "fingerprint": source.fingerprint,
                },
                ensure_ascii=False,
            )
        )
        note.tags = (
            [tag for tag in note.tags if tag != REPAIR_TAG]
            if repair_id
            else [
                GENERATED_TAG,
                KEY_PREFIX + source.key,
                f"chinese-frontier::source::{source.card_id}",
            ]
        )
        if repair_id:
            col.update_note(note)
        else:
            col.add_note(note, DeckId(source.deck_id))
        stored = col.get_note(NoteId(note.id))
        cards = stored.cards()
        # Read back both the stored note and rendered card before committing the batch.
        if (
            stored["Text"] != valid.text
            or len(cards) != 1
            or (not repair_id and cards[0].type != 0)
        ):
            raise ValueError("Replacement did not produce exactly one new cloze card")
        question = cards[0].question()
        answer = cards[0].answer()
        if (
            'class="cloze"' not in question
            or source.word not in answer
            or html.escape(valid.candidate.translation) not in question
        ):
            raise ValueError("Replacement card rendering failed verification")
        (updated if repair_id else admitted).append(int(note.id))
        current.existing_keys.add(source.key)
        current.covered_words.add(source.word)
        # The same batch can contain duplicate source keys after crash recovery.
        sources = {
            cid: value
            for cid, value in sources.items()
            if value.key != source.key and value.word != source.word
        }
        if not repair_id:
            for budget in budgets:
                reserved[budget.deck_id] = reserved.get(budget.deck_id, 0) + 1
    if undo is not None:
        changes = col.merge_undo_entries(undo)
    return AdmissionResult(changes, admitted, remaining, rejected, updated)
