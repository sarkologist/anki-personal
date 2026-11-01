# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import hashlib
import html
import json
import re
import time
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, field
from typing import Any

from anki.cards import CardId
from anki.collection import Collection, OpChanges
from anki.decks import DeckId
from anki.notes import NoteId

from .. import categories
from ..collection import (
    MAX_REPAIRS,
    SEGMENT_BREAK,
    FeedbackResult,
    _copy_scheduling,
    _descendants,
    _hide_metadata,
    _ids,
    _learner_feedback,
    _read_budgets,
    _save_feedback,
    _scheduling,
    _unbury,
)
from ..core import (
    CLOZE,
    DeckBudget,
    ValidationError,
    admission_slots,
    cloze_field,
    fingerprint,
    lookahead_days,
    plain_text,
    successful_days,
)
from .core import (
    EVIDENCE_POLICY,
    KEY_TAG,
    MODEL,
    POLICY,
    TAG,
    Knowledge,
    Target,
    spelled,
    validate,
)
from .generation import Draft
from .lexicon import Lexicon, normalize, tokens

SETTINGS = "frenchFrontier"
FIELDS = (
    "Text",
    "Translation",
    "Cue",
    "Target",
    "Meaning",
    "Extra",
    "CardKind",
    "Source",
    "Dependencies",
    "Disambiguation",
)
LABEL = '{{#CardKind}}<div class="kind">{{CardKind}}</div>{{/CardKind}}'
FRONT = (
    LABEL
    + '<div class="sentence">{{cloze:Text}}</div><div class="translation">{{Translation}}</div>{{#Cue}}<div class="cue">Verb: {{Cue}}</div>{{/Cue}}'
)
BACK = (
    FRONT
    + '<hr id="answer"><div>{{Target}}</div><div>{{Meaning}}</div><div>{{Extra}}</div>'
)


def json_field(value: str) -> dict[str, Any]:
    try:
        result = json.loads(html.unescape(value))
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


REPAIR_TAG = "french-frontier::repair"
# The learner hands over a note that kept failing repair; automation leaves it alone.
MANUAL_TAG = "french-frontier::manual"
FEEDBACK_TAG = "french-frontier::feedback::"


def presentation_hash(fields: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                name: fields.get(name, "")
                for name in (
                    "Text",
                    "Translation",
                    "Cue",
                    "Target",
                    "Meaning",
                    "Extra",
                    "CardKind",
                )
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


MOODS = {
    "ind": "INDICATIF",
    "sub": "SUBJONCTIF",
    "cnd": "CONDITIONNEL",
    "imp": "IMPÉRATIF",
}
TENSES = {"pre": "Présent", "imp": "Imparfait", "fut": "Futur", "pas": "Passé simple"}
SUBJECTS = {
    ("1", "s"): "je",
    ("2", "s"): "tu",
    ("3", "s"): "il/elle",
    ("1", "p"): "nous",
    ("2", "p"): "vous",
    ("3", "p"): "ils/elles",
}
# Verbs whose initial h blocks elision (je hais, not j'hais).
ASPIRATED = {
    "hacher", "haler", "haleter", "hanter", "happer", "haranguer", "harasser",
    "harceler", "harnacher", "harponner", "hasarder", "hausser", "haïr", "hennir",
    "heurter", "hisser", "hocher", "honnir", "huer", "hurler", "hâbler", "hâter",
    "héler", "hérisser",
}  # fmt: skip


def _subject(person: tuple[str, str], tense: str, surface: str, lemma: str) -> str:
    if tense.startswith("imp:"):
        return ""
    subject = SUBJECTS[person]
    if subject == "je" and (
        surface[:1] in "aeiouyàâéèêëîïôûœæ"
        or (surface[:1] == "h" and lemma not in ASPIRATED)
    ):
        subject = "j'"
    else:
        subject += " "
    if tense.startswith("sub:"):
        subject = ("qu'" if subject.startswith("il") else "que ") + subject
    return html.escape(subject).replace(" ", "&nbsp;")


def conjugation_tables(lexicon: Lexicon, form_key: str) -> str:
    """Present the tested tense, plus the present indicative for reference."""
    try:
        surface, lemma, pos, features = form_key.split("|")
    except ValueError:
        return ""
    if pos != "VER":
        return ""
    tested = ":".join(features.split(":")[:2])
    tenses = [tested] if tested.split(":")[0] in MOODS else []
    if tested != "ind:pre":
        tenses.append("ind:pre")
    paradigm = lexicon.paradigms.get(lemma, {})
    parts = []
    for tense in tenses:
        rows = []
        for person in SUBJECTS:
            feature = tense + ":" + ":".join(person)
            surfaces = paradigm.get(feature, [])
            if not surfaces:
                continue
            shown = "/".join(
                f'<font color="#ff0000">{html.escape(s)}</font>'
                if feature == features and s == surface
                else html.escape(s)
                for s in surfaces
            )
            rows.append(
                f"<li><i>{_subject(person, tense, surfaces[0], lemma)}</i><i>{shown}</i></li>"
            )
        if rows:
            mood, time = tense.split(":")
            parts.append(
                f"<div>{MOODS[mood]}</div><div><div>{TENSES.get(time, time)}</div>"
                f"<ul>{''.join(rows)}</ul></div>"
            )
    if not parts:
        return ""
    return f"<hr><div><b>{html.escape(lemma)}</b></div>" + "".join(parts)


def extra_field(
    explanation: str, kind: str, form_key: str, mode: str, lexicon: Lexicon
) -> str:
    if mode == "stem":
        explanation = "Recall the missing part using the visible stem."
    tables = (
        conjugation_tables(lexicon, form_key)
        if kind == "conjugation" and mode in ("word", "stem")
        else ""
    )
    return html.escape(explanation) + tables


def refresh_conjugation_tables(
    col: Collection, lexicon: Lexicon, skip: AbstractSet[int] = frozenset()
) -> OpChanges:
    """Fill tables into unmodified generated notes without regenerating them."""
    model = col.models.by_name(MODEL)
    if not model or not lexicon.paradigms:
        # Without paradigms every table would render empty and erase existing ones.
        return OpChanges()
    undo = None
    for nid in col.models.nids(model["id"]):
        if nid in skip:
            # A queued regeneration fingerprints the current hash; leave it intact.
            continue
        note = col.get_note(nid)
        values = {name: note[name] for name in note.keys()}
        data = json_field(values["Source"])
        target = data.get("target")
        candidate = data.get("candidate")
        if (
            TAG not in note.tags
            or REPAIR_TAG in note.tags
            or not isinstance(target, dict)
            or not isinstance(candidate, dict)
            or target.get("kind") != "conjugation"
            or data.get("presentation_hash") != presentation_hash(values)
        ):
            continue
        extra = extra_field(
            str(candidate.get("explanation", "")),
            "conjugation",
            str(data.get("form_key", "")),
            str(data.get("mode", "")),
            lexicon,
        )
        if extra == values["Extra"]:
            continue
        if undo is None:
            undo = col.add_custom_undo_entry("Add French conjugation tables")
        note["Extra"] = values["Extra"] = extra
        data["presentation_hash"] = presentation_hash(values)
        note["Source"] = html.escape(json.dumps(data, ensure_ascii=False))
        col.update_note(note)
    return col.merge_undo_entries(undo) if undo is not None else OpChanges()


def card_kind(mode: str, target_kind: str) -> str:
    # A visible stem is a cue, not a confusion, so it is labelled like a word.
    if mode == "contrast":
        return "Disambiguation"
    return "Conjugation" if target_kind == "conjugation" else ""


def drop_cue_labels(col: Collection, skip: AbstractSet[int] = frozenset()) -> OpChanges:
    """Unlabel cards admitted while every fallback was called Disambiguation."""
    model = col.models.by_name(MODEL)
    if not model:
        return OpChanges()
    undo = None
    for nid in col.models.nids(model["id"]):
        if nid in skip:
            # A queued regeneration fingerprints the current hash; leave it intact.
            continue
        note = col.get_note(nid)
        values = {name: note[name] for name in note.keys()}
        data = json_field(values["Source"])
        target = data.get("target")
        mode = data.get("mode")
        if TAG not in note.tags or mode == "contrast" or not isinstance(target, dict):
            continue
        kind = values["CardKind"]
        if kind == "Disambiguation":
            kind = card_kind(str(mode), str(target.get("kind", "")))
        tags = [t for t in note.tags if t != "disambiguation"]
        if kind == values["CardKind"] and tags == note.tags:
            continue
        if undo is None:
            undo = col.add_custom_undo_entry("Unlabel French stem cards")
        unchanged = data.get("presentation_hash") == presentation_hash(values)
        note["CardKind"] = values["CardKind"] = kind
        note.tags = tags
        if unchanged:
            data["presentation_hash"] = presentation_hash(values)
            note["Source"] = html.escape(json.dumps(data, ensure_ascii=False))
        col.update_note(note)
    return col.merge_undo_entries(undo) if undo is not None else OpChanges()


def request_repair(col: Collection, note_id: int) -> OpChanges:
    note = col.get_note(note_id)
    if TAG not in note.tags or note.note_type()["name"] != MODEL:
        raise ValueError("Only generated French notes can be repaired")
    note.add_tag(REPAIR_TAG)
    return col.update_note(note)


def repair_count(note: Any) -> int:
    entries = json_field(note["Source"]).get("feedback", [])
    return len(entries) if isinstance(entries, list) else 0


def confusions(data: dict[str, Any]) -> tuple[str, ...]:
    """Words the learner said they confuse with the target, oldest first."""
    entries = data.get("feedback", [])
    return tuple(
        dict.fromkeys(
            str(e["answered"])
            for e in (entries if isinstance(entries, list) else [])
            if isinstance(e, dict) and e.get("answered")
        )
    )


def _other_words(words: Any, answer: AbstractSet[str]) -> list[str]:
    """The words that are not French words other than the answer."""
    return [w for w in words if not tokens(w) or answer & spelled(w)]


def record_feedback(
    col: Collection,
    note_id: int,
    *,
    reasons: tuple[str, ...] = (),
    note: str = "",
    avoid_words: tuple[str, ...] = (),
    answered: str = "",
    values: dict[str, str] | None = None,
) -> FeedbackResult:
    """Keep the learner's complaint on the note and queue a repair that must fix it.

    With no reasons, note or words, this is a plain reroll that still avoids the
    current sentence."""
    target = col.get_note(NoteId(note_id))
    if TAG not in target.tags or target.note_type()["name"] != MODEL:
        raise ValueError("Only generated French notes can be repaired")
    data = json_field(target["Source"])
    answer = spelled(str(data.get("answer", "")))
    answer.add(normalize(str(data.get("target", {}).get("lemma", ""))))
    learnt = categories.structured(
        col, reasons, values or {}, lambda ws: _other_words(ws, answer), normalize
    )
    avoid_words = tuple(dict.fromkeys(normalize(w) for w in avoid_words if w.strip()))
    if bad := _other_words(avoid_words, answer):
        raise ValueError(
            "Words to avoid must be French words other than the answer: "
            + ", ".join(bad)
        )
    answered = normalize(answered)
    if answered and answer & spelled(answered):
        raise ValueError(
            f"{answered} is this card's answer. Enter the other word you give "
            'instead, or tick "Tests a different sense of the word".'
        )
    if answered and len(tokens(answered)) != 1:
        raise ValueError("Enter one other French word you confuse it with")
    handed_over = repair_count(target) >= MAX_REPAIRS
    candidate = data.get("candidate")
    sentence = (
        str(candidate.get("sentence", ""))
        if isinstance(candidate, dict)
        else plain_text(CLOZE.sub(lambda m: m[2].split("::", 1)[0], target["Text"]))
    )
    entries = data.get("feedback")
    entries = entries if isinstance(entries, list) else []
    entries.append(
        {
            "reasons": list(reasons),
            "note": note.strip()[:500],
            "avoid_words": list(avoid_words),
            "answered": answered,
            "sentence": " ".join(sentence.split()),
            "rejected": True,
            **learnt,
        }
    )
    data["feedback"] = entries
    target["Source"] = html.escape(json.dumps(data, ensure_ascii=False))
    tags = [t for t in target.tags if t != REPAIR_TAG]
    tags.append(MANUAL_TAG if handed_over else REPAIR_TAG)
    tags.extend(categories.feedback_tags(FEEDBACK_TAG, entries[-1]))
    target.tags = list(dict.fromkeys(tags))
    return FeedbackResult(_save_feedback(col, target, handed_over), handed_over)


# Fixing the learner's own cloze cards: the card is suspended and a new French
# Frontier note, written to the learner's feedback, takes over its schedule.
FIXES = "frenchFrontierFixes"
PENDING_TAG = "french-frontier::fix-pending"
ADOPTED_TAG = "french-frontier::adopted"
RESTORED_TAG = "french-frontier::restored"
# A fixed card tests the meaning the learner gave, not a dictionary sense.
LEARNER_SENSE = "learner card"


@dataclass(frozen=True)
class FixTarget:
    answer: str
    sentence: str
    # The rest of the cloze field, usually the translation.
    translation: str


def is_generated(note: Any) -> bool:
    return TAG in note.tags and note.note_type()["name"] == MODEL


def adopted_original(note: Any) -> dict[str, Any] | None:
    """The learner card an adopted note replaced, unless it was restored."""
    if not is_generated(note) or RESTORED_TAG in note.tags:
        return None
    adopted = json_field(note["Source"]).get("adopted")
    return adopted if isinstance(adopted, dict) else None


def _scope(col: Collection) -> set[int]:
    deck_id = int(col.get_config(SETTINGS, {}).get("deck_id", 0))
    decks = {int(d["id"]): d for d in col.decks.all()}
    return _descendants(decks, deck_id) if deck_id in decks else set()


def fix_target(
    fields: dict[str, str], model: dict[str, Any], ordinal: int
) -> FixTarget | None:
    """The one French word this cloze card hides, with its sentence."""
    if model["type"] != 1:
        return None
    qfmt = model["tmpls"][0]["qfmt"]
    answers: list[str] = []
    sentences: list[str] = []
    other: list[str] = []
    for name, value in fields.items():
        if not cloze_field(qfmt, name):
            continue
        for part in SEGMENT_BREAK.split(value):
            hidden = [
                plain_text(m[2].split("::", 1)[0])
                for m in CLOZE.finditer(part)
                if int(m[1]) == ordinal + 1
            ]
            answers += hidden
            (sentences if hidden else other).append(plain_text(_unfold(part)))
    answers = list(dict.fromkeys(normalize(a) for a in answers if a))
    if len(answers) != 1 or len(tokens(answers[0])) != 1:
        return None
    translation = " ".join(dict.fromkeys(part for part in other if part))
    return FixTarget(answers[0], " ".join(sentences), translation[:500])


def _unfold(text: str) -> str:
    return CLOZE.sub(lambda m: m[2].split("::", 1)[0], text)


def fix_target_for_card(col: Collection, card_id: int) -> FixTarget | None:
    """Fixable cards are the learner's own, in the French deck tree, outside automation."""
    card = col.get_card(CardId(card_id))
    note = card.note()
    if (
        (card.odid or card.did) not in _scope(col)
        or is_generated(note)
        or any(t.casefold() in ("leech", "french-frontier::exclude") for t in note.tags)
    ):
        return None
    return fix_target(dict(note.items()), note.note_type(), card.ord)


def fix_readings(lexicon: Lexicon, answer: str) -> list[tuple[str, str]]:
    """Lemmas the answer can be, most frequent first."""
    forms = sorted(lexicon.analyses(answer), key=lambda f: -f.frequency)
    return list(dict.fromkeys((f.lemma, f.pos) for f in forms))


def _entry(lexicon: Lexicon, lemma: str, pos: str) -> tuple[int, float]:
    for entry in lexicon.entries:
        if (entry.lemma, entry.pos) == (lemma, pos):
            return entry.level, entry.frequency
    return 0, max((f.frequency for f in lexicon.lemmas[(lemma, pos)]), default=0)


@dataclass
class FixResult:
    changes: OpChanges
    # The replacement's target key.
    key: str


def request_fix(
    col: Collection,
    card_id: int,
    *,
    lemma: str,
    definition: str,
    lexicon: Lexicon,
    pos: str = "",
    reasons: tuple[str, ...] = (),
    note: str = "",
    avoid_words: tuple[str, ...] = (),
    answered: str = "",
    values: dict[str, str] | None = None,
) -> FixResult:
    """Suspend a learner card that tests the wrong thing and queue its replacement.

    Only the card's queue and one tag on its note change; the fields never do."""
    fix = fix_target_for_card(col, card_id)
    if fix is None:
        raise ValueError(
            "Only your own French cloze cards in the French deck can be fixed here"
        )
    lemma = normalize(lemma)
    # The answer's own readings first, then any dictionary lemma typed instead.
    readings = [
        (lem, p)
        for lem, p in fix_readings(lexicon, fix.answer)
        + sorted(
            (k for k in lexicon.lemmas if k[0] == lemma),
            key=lambda k: -_entry(lexicon, *k)[1],
        )
        if lem == lemma and (not pos or p == pos)
    ]
    if not readings:
        raise ValueError(f"{lemma or 'The target'} is not in the French dictionary")
    lemma, pos = readings[0]
    definition = " ".join(definition.split())
    if not definition:
        raise ValueError("Enter the meaning this card tests")
    answer = spelled(fix.answer) | {lemma}
    learnt = categories.structured(
        col, reasons, values or {}, lambda ws: _other_words(ws, answer), normalize
    )
    avoid_words = tuple(dict.fromkeys(normalize(w) for w in avoid_words if w.strip()))
    if bad := _other_words(avoid_words, answer):
        raise ValueError(
            "Words to avoid must be French words other than the answer: "
            + ", ".join(bad)
        )
    answered = normalize(answered)
    if answered and answer & spelled(answered):
        raise ValueError(
            f"{answered} is this card's answer. Enter the other word you give instead."
        )
    if answered and len(tokens(answered)) != 1:
        raise ValueError("Enter one other French word you confuse it with")
    level, frequency = _entry(lexicon, lemma, pos)
    target = Target(
        lemma, pos, LEARNER_SENSE, "vocabulary", definition, level, frequency
    )
    card = col.get_card(CardId(card_id))
    original = card.note()
    fixes = dict(col.get_config(FIXES, {}))
    if any(
        isinstance(other, dict) and other.get("key") == target.key
        for other_id, other in fixes.items()
        if other_id != str(card_id)
    ) or col.find_notes(f"tag:{KEY_TAG}{target.key} -tag:{RESTORED_TAG}"):
        raise ValueError(f"Another of your cards already has a fix for {lemma}")
    record = dict(fixes.get(str(card_id), {}))
    entries = record.get("feedback")
    entries = list(entries) if isinstance(entries, list) else []
    entries.append(
        {
            "reasons": list(reasons),
            "note": note.strip()[:500],
            "avoid_words": list(avoid_words),
            "answered": answered,
            "sentence": fix.sentence,
            "rejected": True,
            **learnt,
        }
    )
    record.update(
        note_id=int(original.id),
        ordinal=int(card.ord),
        deck_id=int(card.odid or card.did),
        target=target.stored(),
        key=target.key,
        sentence=fix.sentence,
        # Fields only: the pending tag must not make the request stale.
        fingerprint=fingerprint(dict(original.items()), []),
        requested_at=int(record.get("requested_at", time.time())),
        feedback=entries,
    )
    fixes[str(card_id)] = record
    undo = col.add_custom_undo_entry("Fix French sentence")
    col.sched.suspend_cards([CardId(card_id)])
    if PENDING_TAG not in original.tags:
        original.tags.append(PENDING_TAG)
        col.update_note(original)
    col.set_config(FIXES, fixes, undoable=True)
    return FixResult(col.merge_undo_entries(undo), target.key)


def _take_over(col: Collection, card_id: int, card: Any, fixes: dict[str, Any]) -> None:
    """The new card continues the learner's card; the fix is done."""
    _copy_scheduling(col.get_card(CardId(card_id)), card)
    col.update_card(card)
    if _scheduling(col.get_card(card.id)) != _scheduling(card):
        raise ValueError("Copying the original card's schedule failed")
    record = fixes.pop(str(card_id))
    _untag_pending(col, int(record.get("note_id", 0)), fixes)
    col.set_config(FIXES, fixes, undoable=True)


def _untag_pending(col: Collection, note_id: int, fixes: dict[str, Any]) -> None:
    if any(
        isinstance(r, dict) and int(r.get("note_id", 0)) == note_id
        for r in fixes.values()
    ) or not col.db.scalar("select count() from notes where id=?", note_id):
        return
    note = col.get_note(NoteId(note_id))
    if PENDING_TAG in note.tags:
        note.tags = [t for t in note.tags if t != PENDING_TAG]
        col.update_note(note)


def cancel_fix(col: Collection, card_id: int) -> OpChanges:
    """Put a learner card waiting for a fix back into review, unchanged."""
    fixes = dict(col.get_config(FIXES, {}))
    record = fixes.pop(str(card_id), None)
    if not isinstance(record, dict):
        raise ValueError("No fix is pending for this card")
    undo = col.add_custom_undo_entry("Cancel French sentence fix")
    if col.db.scalar("select count() from cards where id=?", card_id):
        col.sched.unsuspend_cards([CardId(card_id)])
    _untag_pending(col, int(record.get("note_id", 0)), fixes)
    col.set_config(FIXES, fixes, undoable=True)
    return col.merge_undo_entries(undo)


def restore_original(col: Collection, note_id: int) -> OpChanges:
    """Return to the learner's card with the adopted card's progress; the
    adopted note stays, suspended and tagged, so its feedback remains readable."""
    note = col.get_note(NoteId(note_id))
    adopted = adopted_original(note)
    if adopted is None:
        raise ValueError("This note did not replace one of your cards, or was restored")
    card_id = int(adopted.get("card_id", 0))
    if not col.db.scalar("select count() from cards where id=?", card_id):
        raise ValueError("The original card no longer exists")
    original = col.get_card(CardId(card_id))
    if original.queue != -1:
        raise ValueError(
            "The original card is already back in review; restoring would "
            "overwrite its progress"
        )
    cards = note.cards()
    undo = col.add_custom_undo_entry("Restore original French card")
    _copy_scheduling(cards[0], original)
    col.update_card(original)
    col.sched.suspend_cards([card.id for card in cards])
    note = col.get_note(note.id)
    data = json_field(note["Source"])
    data["adopted"] = {**adopted, "restored_at": int(time.time())}
    note["Source"] = html.escape(json.dumps(data, ensure_ascii=False))
    note.tags = [t for t in note.tags if t != REPAIR_TAG] + [RESTORED_TAG]
    col.update_note(note)
    return col.merge_undo_entries(undo)


def _fix_problem(
    col: Collection, card_id: int, record: dict[str, Any], scope: set[int]
) -> str:
    """Why a pending fix cannot proceed; empty when it can."""
    row = col.db.first(
        "select c.did,c.odid,c.queue,n.mid,n.flds from cards c join notes n on n.id=c.nid "
        "where c.id=?",
        card_id,
    )
    if not row:
        return "original card was deleted"
    did, odid, queue, mid, joined = row
    if (odid or did) not in scope:
        return "original card left the French deck"
    if queue != -1:
        # Swapping now would leave both cards in review.
        return "original card was unsuspended"
    model = col.models.get(mid)
    values = {
        f["name"]: v for f, v in zip(model["flds"], joined.split("\x1f"), strict=True)
    }
    if fingerprint(values, []) != record.get("fingerprint"):
        return "original note was edited"
    return ""


def _fix_target(record: dict[str, Any]) -> Target:
    complaints, avoid, rejected = _learner_feedback(record)
    return Target(
        **record["target"],
        learner_feedback=complaints,
        avoid_words=avoid,
        rejected_sentences=rejected,
        reported_confusions=confusions(record),
    )


@dataclass(frozen=True)
class PendingFix:
    card_id: int
    lemma: str
    key: str
    requested_at: int
    problem: str


def pending_fixes(col: Collection) -> list[PendingFix]:
    scope = _scope(col)
    result = []
    keys: set[str] = set()
    for raw_id, record in col.get_config(FIXES, {}).items():
        if not isinstance(record, dict):
            continue
        key = str(record.get("key", ""))
        # The snapshot skips these silently; say why the fix is not moving.
        collision = key in keys or bool(
            col.find_notes(f"tag:{KEY_TAG}{key} -tag:{RESTORED_TAG}")
        )
        keys.add(key)
        result.append(
            PendingFix(
                int(raw_id),
                str(record.get("target", {}).get("lemma", "")),
                str(record.get("key", "")),
                int(record.get("requested_at", 0)),
                _fix_problem(col, int(raw_id), record, scope)
                or (
                    "another French Frontier note already tests this"
                    if collision
                    else ""
                ),
            )
        )
    return sorted(result, key=lambda item: item.requested_at)


QUESTION_QUE = re.compile(r"(?:\bqu['’ʼ‘]\s*)?\best-ce\s+(?:que\b|qu['’ʼ‘])", re.I)
# que governs most subjunctives; a relative (où, dont, qui, lequel) after an
# indefinite or superlative antecedent can be subjunctive without one.
SUBJUNCTIVE_TRIGGER = re.compile(
    r"\b(?:(?:quoi)?(?:que\b|qu['’ʼ‘])|où\b|dont\b|qui\b"
    r"|(?:le|la|les|au|aux|du|des)quel(?:le)?s?\b)",
    re.I,
)


def contextual_analyses(value: str, match: Any, lexicon: Lexicon) -> tuple[str, ...]:
    answer = plain_text(match[2].split("::", 1)[0])
    forms = lexicon.analyses(answer)
    before = plain_text(
        CLOZE.sub(lambda m: m[2].split("::", 1)[0], value[: match.start()])
    )
    hint = normalize(match[2].split("::", 1)[1]) if "::" in match[2] else ""
    if hint:
        filtered = [f for f in forms if f.lemma == hint]
        if filtered:
            forms = filtered
    subject = re.search(
        r"(?:\b(je|tu|il|elle|on|nous|vous|ils|elles)|\b(j['’]))\s*$", before, re.I
    )
    if subject:
        pronoun = normalize(subject[1] or subject[2])
        feature = {
            "je": "1:s",
            "j'": "1:s",
            "tu": "2:s",
            "il": "3:s",
            "elle": "3:s",
            "on": "3:s",
            "nous": "1:p",
            "vous": "2:p",
            "ils": "3:p",
            "elles": "3:p",
        }[pronoun]
        finite = [
            f
            for f in forms
            if f.pos == "VER"
            and f.features.endswith(":" + feature)
            and f.features.startswith(("ind:", "sub:", "cnd:"))
        ]
        if finite:
            # A subject with no que or relative before it (est-ce que only
            # marks a question) is not subjunctive.
            if not SUBJUNCTIVE_TRIGGER.search(
                QUESTION_QUE.sub(" ", before[: subject.start()])
            ):
                finite = [
                    f for f in finite if not f.features.startswith("sub:")
                ] or finite
            return tuple(f.key for f in finite)
    if re.search(
        r"\b(ai|as|a|avons|avez|ont|avais|avait|avaient|aviez|avions)\s*$", before, re.I
    ):
        participles = [
            f.key for f in forms if f.pos == "VER" and f.features.startswith("par:pas:")
        ]
        if participles:
            return tuple(participles)
    # A following infinitive identifies a finite verb, even when the subject
    # is a noun phrase and the dictionary also lists a nominal homograph.
    # This selects POS only: syncretic verb persons/tenses remain unresolved.
    after = plain_text(
        CLOZE.sub(lambda m: m[2].split("::", 1)[0], value[match.end() :])
    )
    following = tokens(after)
    if following and after.lstrip().casefold().startswith(following[0]):
        if any(
            f.pos == "VER" and f.features == "inf"
            for f in lexicon.analyses(following[0])
        ):
            finite = [
                f.key
                for f in forms
                if f.pos == "VER" and f.features.startswith(("ind:", "sub:", "cnd:"))
            ]
            if finite:
                return tuple(finite)
    return tuple(f.key for f in forms) if hint else ()


@dataclass
class Snapshot:
    day: int
    deck_id: int
    known: Knowledge
    represented: set[tuple[str, str]]
    keys: set[str]
    budgets: list[DeckBudget]
    ignore_review_limit: bool
    repairs: dict[str, tuple[int, str, Target]] = field(default_factory=dict)
    covered: set[tuple[str, str]] = field(default_factory=set)
    covered_forms: set[str] = field(default_factory=set)
    lookahead_days: int = 0
    # Learner cards awaiting a replacement: key -> (card id, fingerprint, target).
    fixes: dict[str, tuple[int, str, Target]] = field(default_factory=dict)

    @property
    def slots(self) -> int:
        return admission_slots(
            self.budgets,
            ignore_review_limit=self.ignore_review_limit,
            lookahead_days=self.lookahead_days,
        )


def snapshot(
    col: Collection, settings: dict[str, Any], config: dict[str, Any], lexicon: Lexicon
) -> Snapshot:
    deck_id = int(settings.get("deck_id", 0))
    decks = {int(d["id"]): d for d in col.decks.all()}
    if deck_id not in decks or decks[deck_id].get("dyn"):
        raise ValueError("Select a normal French deck in French Frontier settings")
    scope = _ids(_descendants(decks, deck_id))
    timing = col.sched._timing_today()
    known = Knowledge()
    keys = set()
    represented = set()
    repairs = {}
    covered = set()
    covered_forms = set()
    logs = {}
    for cid, stamp, ease in col.db.all(
        "select r.cid,r.id,r.ease from revlog r join cards c on c.id=r.cid "
        f"where (c.did in {scope} or c.odid in {scope}) and r.type in (0,1,2,3) and r.ease between 1 and 4 order by r.id"
    ):
        logs.setdefault(cid, []).append((stamp, ease))
    for cid, nid, ordinal, queue, reps, mid, fields, tags in col.db.all(
        "select c.id,c.nid,c.ord,c.queue,c.reps,n.mid,n.flds,n.tags from cards c join notes n on n.id=c.nid "
        f"where c.did in {scope} or c.odid in {scope}"
    ):
        model = col.models.get(mid)
        if not model:
            continue
        values = {
            f["name"]: v
            for f, v in zip(model["flds"], fields.split("\x1f"), strict=True)
        }
        own = TAG in tags.split() and model["name"] == MODEL
        metadata = json_field(values.get("Source", "")) if own else {}
        adopted = metadata.get("adopted")
        card_logs = logs.get(cid, [])
        if isinstance(adopted, dict):
            # The replaced learner card's history is this card's history too.
            card_logs = sorted(card_logs + logs.get(int(adopted.get("card_id", 0)), []))
        if own and isinstance(metadata.get("target"), dict):
            target = metadata["target"]
            represented.add((str(target.get("lemma", "")), str(target.get("pos", ""))))
            if queue != -1 and not any(
                t.casefold() in ("leech", "french-frontier::exclude", MANUAL_TAG)
                for t in tags.split()
            ):
                if (
                    REPAIR_TAG in tags.split()
                    or metadata.get("policy") != POLICY
                    or metadata.get("presentation_hash") != presentation_hash(values)
                ):
                    try:
                        complaints, avoid, rejected = _learner_feedback(metadata)
                        repair_target = Target(
                            **target,
                            learner_feedback=complaints,
                            avoid_words=avoid,
                            rejected_sentences=rejected,
                            reported_confusions=confusions(metadata),
                        )
                        repairs[repair_target.key] = (
                            int(nid),
                            presentation_hash(values),
                            repair_target,
                        )
                    except (TypeError, ValueError):
                        pass
        if metadata.get("key") in repairs:
            continue
        if queue == -1 or model["type"] != 1:
            continue
        established = (
            bool(reps)
            and not any(
                t.casefold() in ("leech", "french-frontier::exclude")
                for t in tags.split()
            )
            and successful_days(card_logs, timing.next_day_at)
            >= max(1, int(config.get("minimum_success_days", 2)))
        )
        qfmt = model["tmpls"][0]["qfmt"]
        for name, value in values.items():
            if not cloze_field(qfmt, name):
                continue
            # Place markers around this answer after stripping HTML, so partial
            # word cards cannot silently establish the complete lexical item.
            for match in CLOZE.finditer(value):
                if int(match[1]) != ordinal + 1:
                    continue
                answer = plain_text(match[2].split("::", 1)[0])
                marked = plain_text(
                    value[: match.start()] + "§" + answer + "§" + value[match.end() :]
                )
                before, _, after = marked.partition("§" + answer + "§")
                partial = bool(
                    (before and before[-1].isalpha())
                    or (
                        after
                        and (after[0].isalpha() or after[0] in "'’")
                        and not answer.endswith(("'", "’"))
                    )
                )
                form_key = ""
                if own:
                    audit = json_field(values.get("Disambiguation", ""))
                    policy = audit.get("policy")
                    if (
                        not isinstance(policy, int)
                        or not EVIDENCE_POLICY <= policy <= POLICY
                        or audit.get("status") != "applied"
                    ):
                        continue
                    partial |= metadata.get("mode") == "stem"
                    form_key = (
                        metadata.get("form_key", "")
                        if normalize(answer) == normalize(metadata.get("answer", ""))
                        else ""
                    )
                selected_keys = (
                    (form_key,)
                    if form_key
                    else contextual_analyses(value, match, lexicon)
                )
                if not partial:
                    analyses = lexicon.analyses(answer)
                    if selected_keys:
                        analyses = [f for f in analyses if f.key in selected_keys]
                    # Coverage only prevents duplicate introductions. Ambiguous,
                    # unseen or struggling cards still cannot establish context.
                    covered.update((f.lemma, f.pos) for f in analyses)
                    covered_forms.update(f.key for f in analyses if f.pos == "VER")
                    # A phrase cloze also reserves its component words, without
                    # treating visible words elsewhere as tested or as known.
                    if len(tokens(answer)) > 1:
                        for word in tokens(answer):
                            parts = lexicon.analyses(word)
                            covered.update((f.lemma, f.pos) for f in parts)
                            covered_forms.update(f.key for f in parts if f.pos == "VER")
                if not established:
                    continue
                context = plain_text(CLOZE.sub(lambda m: m[2].split("::", 1)[0], value))
                known.learn(
                    answer,
                    lexicon,
                    cid,
                    context,
                    partial=partial,
                    form_key=form_key,
                    allowed_keys=contextual_analyses(value, match, lexicon)
                    if not own
                    else (),
                )
                if not partial:
                    selected_keys = (
                        (form_key,)
                        if form_key
                        else contextual_analyses(value, match, lexicon)
                    )
                    if selected_keys and any(
                        "|VER|par:pas:" in key for key in selected_keys
                    ):
                        before = plain_text(
                            CLOZE.sub(
                                lambda m: m[2].split("::", 1)[0], value[: match.start()]
                            )
                        )
                        if re.search(
                            r"\b(ai|as|a|avons|avez|ont|avais|avait|avaient|aviez|avions)\s*$",
                            before,
                            re.I,
                        ):
                            known.constructions.add("compound:avoir")
                    if re.search(r"\bne\b|\bn['’]", context, re.I) and re.search(
                        r"\bpas\b", context, re.I
                    ):
                        known.constructions.add("negation")
                if own and not partial:
                    known.constructions.update(metadata.get("constructions", []))
    # Keys across the collection prevent duplicates after notes move decks or
    # are suspended. Legacy notes and their tags are never modified.
    for tag_string in col.db.list(
        "select tags from notes where tags like ?", "%" + KEY_TAG + "%"
    ):
        # A restored note no longer stands for its target.
        if RESTORED_TAG in tag_string.split():
            continue
        keys.update(
            t[len(KEY_TAG) :] for t in tag_string.split() if t.startswith(KEY_TAG)
        )
    fixes = {}
    fix_scope = _descendants(decks, deck_id)
    for raw_id, record in col.get_config(FIXES, {}).items():
        if not isinstance(record, dict) or _fix_problem(
            col, int(raw_id), record, fix_scope
        ):
            # A stalled fix waits, suspended, until the learner cancels it.
            continue
        try:
            fix_target = _fix_target(record)
        except (KeyError, TypeError, ValueError):
            continue
        if fix_target.key not in keys and fix_target.key not in fixes:
            fixes[fix_target.key] = (
                int(raw_id),
                str(record["fingerprint"]),
                fix_target,
            )
            represented.add((fix_target.lemma, fix_target.pos))
    budgets = _read_budgets(col, decks, deck_id, [], timing.days_elapsed)[deck_id]
    return Snapshot(
        timing.days_elapsed,
        deck_id,
        known,
        represented,
        keys,
        budgets,
        bool(col.get_config("newCardsIgnoreReviewLimit", False)),
        repairs,
        covered,
        covered_forms,
        lookahead_days(config),
        fixes,
    )


def ensure_model(col: Collection) -> dict[str, Any]:
    model = col.models.by_name(MODEL)
    if model:
        if (
            model["type"] != 1
            or tuple(f["name"] for f in model["flds"]) != FIELDS
            or len(model["tmpls"]) != 1
        ):
            raise ValueError("French Frontier note type has incompatible fields")
        if _hide_metadata(model):
            col.models.update_dict(model)
        return model
    model = col.models.new(MODEL)
    model["type"] = 1
    for name in FIELDS:
        col.models.add_field(model, col.models.new_field(name))
    _hide_metadata(model)
    template = col.models.new_template("Sentence")
    template["qfmt"] = FRONT
    template["afmt"] = BACK
    col.models.add_template(model, template)
    model["css"] = (
        ".card {font-family: sans-serif; font-size:23px; text-align:center;} .sentence {font-size:30px;line-height:1.5} .cloze {font-weight:bold;color:#2677bb} .translation {font-size:20px;margin-top:1em} .kind,.cue {font-size:16px;color:#777;margin:1em} .nightMode .cloze {color:#74b8ef}"
    )
    result = col.models.add_dict(model)
    return col.models.get(result.id)


@dataclass
class Admission:
    changes: OpChanges
    admitted: list[int]
    remaining: list[Draft]
    rejected: dict[str, str]
    added_cards: int = 0
    repaired_notes: int = 0


def admit(
    col: Collection,
    settings: dict[str, Any],
    config: dict[str, Any],
    drafts: list[Draft],
    lexicon: Lexicon,
) -> Admission:
    if col.get_config(SETTINGS, {}) != settings or not settings.get("enabled"):
        return Admission(OpChanges(), [], drafts, {})
    current = snapshot(col, settings, config, lexicon)
    capacity = current.slots
    admitted = []
    added_cards = repaired_notes = 0
    remaining = []
    rejected = {}
    undo = None
    fixes = dict(col.get_config(FIXES, {}))
    for draft in drafts:
        if draft.target.key in current.keys and not draft.repair_note:
            continue
        # A fix takes over the learner's card, so it needs no new-card slot.
        if capacity <= 0 and not (draft.repair_note or draft.fix_card):
            remaining.append(draft)
            continue
        try:
            if draft.fix_card:
                fix = current.fixes.get(draft.target.key)
                if not fix or fix[:2] != (draft.fix_card, draft.old_fingerprint):
                    raise ValidationError("Your card changed while its fix was written")
                if fix[2] != draft.target:
                    # Written before the learner's latest feedback.
                    continue
            if draft.repair_note:
                repair = current.repairs.get(draft.target.key)
                if not repair or repair[:2] != (
                    draft.repair_note,
                    draft.old_fingerprint,
                ):
                    raise ValidationError("Generated note changed during repair")
                if repair[2] != draft.target:
                    # Written before the learner's latest feedback: drop it
                    # without a backoff, so the repair is written again now.
                    continue
            draft.verify()
            for word, records in draft.audit.get("evidence", {}).items():
                if any(
                    record not in current.known.surfaces.get(word, [])
                    for record in records
                ):
                    raise ValidationError("Context evidence changed during generation")
            valid = validate(draft.candidate, draft.target, current.known, lexicon)
            if not (draft.repair_note or draft.fix_card) and (
                (
                    draft.target.kind == "vocabulary"
                    and (draft.target.lemma, draft.target.pos) in current.covered
                )
                or (
                    draft.target.kind == "conjugation"
                    and draft.target.form_key in current.covered_forms
                )
            ):
                continue
        except (ValidationError, KeyError, TypeError) as exc:
            rejected[draft.target.key] = str(exc)
            continue
        if undo is None:
            undo = col.add_custom_undo_entry("Add French sentence cards")
        model = ensure_model(col)
        note = (
            col.get_note(draft.repair_note)
            if draft.repair_note
            else col.new_note(model)
        )
        c = draft.candidate
        data: dict[str, Any] = {
            "policy": POLICY,
            "target": draft.target.stored(),
            "key": draft.target.key,
            "candidate": asdict(c),
            "answer": c.answer,
            "form_key": valid.form_key,
            "mode": c.mode,
            "constructions": list(c.constructions),
        }
        if draft.repair_note:
            # A repair keeps the complaints and the learner card it replaced.
            old = json_field(note["Source"])
            data.update({k: old[k] for k in ("feedback", "adopted") if old.get(k)})
        record = fixes.get(str(draft.fix_card), {}) if draft.fix_card else {}
        if record:
            original = col.get_card(CardId(draft.fix_card))
            data["adopted"] = {
                "card_id": draft.fix_card,
                "note_id": int(record.get("note_id", 0)),
                "ordinal": int(original.ord),
                "notetype": original.note().note_type()["name"],
                "sentence": str(record.get("sentence", "")),
            }
            data["feedback"] = record.get("feedback", [])
        audit = {**draft.audit, "status": "applied"}
        kind = card_kind(c.mode, draft.target.kind)
        values = {
            "Text": valid.text,
            "Translation": valid.translation,
            "Cue": html.escape(c.cue),
            "Target": html.escape(draft.target.lemma),
            "Meaning": html.escape(c.meaning),
            "Extra": extra_field(
                c.explanation, draft.target.kind, valid.form_key, c.mode, lexicon
            ),
            "CardKind": kind,
            "Source": html.escape(json.dumps(data, ensure_ascii=False)),
            "Dependencies": html.escape(
                json.dumps(
                    {w: current.known.surfaces[w] for w in valid.dependencies},
                    ensure_ascii=False,
                )
            ),
            "Disambiguation": html.escape(json.dumps(audit, ensure_ascii=False)),
        }
        data["presentation_hash"] = presentation_hash(values)
        values["Source"] = html.escape(json.dumps(data, ensure_ascii=False))
        for name, value in values.items():
            note[name] = value
        note.tags = (
            [t for t in note.tags if t not in (REPAIR_TAG, "disambiguation")]
            if draft.repair_note
            else [
                TAG,
                KEY_TAG + draft.target.key,
                "french-frontier::" + draft.target.kind,
            ]
        )
        if kind == "Disambiguation":
            note.tags.append("disambiguation")
        if record:
            note.tags += [ADOPTED_TAG] + [
                tag
                for entry in data["feedback"]
                if isinstance(entry, dict)
                for tag in categories.feedback_tags(FEEDBACK_TAG, entry)
            ]
            note.tags = list(dict.fromkeys(note.tags))
        if draft.repair_note:
            col.update_note(note)
            _unbury(col, note.cards())
        else:
            # A fix stays in its card's deck, which the snapshot found in scope.
            home = original.odid or original.did if record else current.deck_id
            col.add_note(note, DeckId(home))
        if record:
            try:
                if len(note.cards()) != 1:
                    raise ValueError("French proposal unexpectedly created extra cards")
                _take_over(col, draft.fix_card, note.cards()[0], fixes)
            except ValueError as exc:
                # Never leave a half-adopted note holding the fix's key.
                col.remove_notes([note.id])
                rejected[draft.target.key] = str(exc)
                continue
            current.fixes.pop(draft.target.key, None)
        elif len(note.cards()) != 1:
            raise ValueError("French proposal unexpectedly created extra cards")
        added_cards += 0 if draft.repair_note or record else 1
        repaired_notes += 1 if draft.repair_note else 0
        admitted.append(int(note.id))
        current.keys.add(draft.target.key)
        capacity -= 0 if draft.repair_note or record else 1
    changes = col.merge_undo_entries(undo) if undo is not None else OpChanges()
    return Admission(
        changes, admitted, remaining, rejected, added_cards, repaired_notes
    )
