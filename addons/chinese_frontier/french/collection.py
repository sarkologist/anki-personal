# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from anki.collection import Collection, OpChanges
from anki.decks import DeckId

from ..collection import _descendants, _ids, _read_budgets
from ..core import (
    CLOZE,
    DeckBudget,
    ValidationError,
    admission_slots,
    plain_text,
    successful_days,
)
from .core import KEY_TAG, MODEL, POLICY, TAG, Knowledge, Target, validate
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


def request_repair(col: Collection, note_id: int) -> OpChanges:
    note = col.get_note(note_id)
    if TAG not in note.tags or note.note_type()["name"] != MODEL:
        raise ValueError("Only generated French notes can be repaired")
    note.add_tag(REPAIR_TAG)
    return col.update_note(note)


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
            if normalize(before) == pronoun:
                indicative = [f for f in finite if f.features.startswith("ind:pre:")]
                if indicative and all(
                    f.features.startswith(("ind:pre:", "sub:pre:")) for f in finite
                ):
                    finite = indicative
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

    @property
    def slots(self) -> int:
        return admission_slots(
            self.budgets, ignore_review_limit=self.ignore_review_limit
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
        if own and isinstance(metadata.get("target"), dict):
            target = metadata["target"]
            represented.add((str(target.get("lemma", "")), str(target.get("pos", ""))))
            if queue != -1 and not any(
                t.casefold() in ("leech", "french-frontier::exclude")
                for t in tags.split()
            ):
                if (
                    REPAIR_TAG in tags.split()
                    or metadata.get("policy") != POLICY
                    or metadata.get("presentation_hash") != presentation_hash(values)
                ):
                    try:
                        repair_target = Target(**target)
                        repairs[repair_target.key] = (
                            int(nid),
                            presentation_hash(values),
                            repair_target,
                        )
                    except (TypeError, ValueError):
                        pass
        if metadata.get("key") in repairs:
            continue
        if (
            queue == -1
            or not reps
            or any(
                t.casefold() in ("leech", "french-frontier::exclude")
                for t in tags.split()
            )
        ):
            continue
        if successful_days(logs.get(cid, []), timing.next_day_at) < max(
            1, int(config.get("minimum_success_days", 2))
        ):
            continue
        if model["type"] != 1:
            continue
        qfmt = model["tmpls"][0]["qfmt"]
        for name, value in values.items():
            if not re.search(
                r"\{\{(?:[^{}:]+:)*cloze:(?:[^{}:]+:)*" + re.escape(name) + r"\}\}",
                qfmt,
            ):
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
                    if (
                        audit.get("policy") != POLICY
                        or audit.get("status") != "applied"
                    ):
                        continue
                    partial |= metadata.get("mode") == "stem"
                    form_key = (
                        metadata.get("form_key", "")
                        if normalize(answer) == normalize(metadata.get("answer", ""))
                        else ""
                    )
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
        keys.update(
            t[len(KEY_TAG) :] for t in tag_string.split() if t.startswith(KEY_TAG)
        )
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
        return model
    model = col.models.new(MODEL)
    model["type"] = 1
    for name in FIELDS:
        col.models.add_field(model, col.models.new_field(name))
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
    remaining = []
    rejected = {}
    undo = None
    for draft in drafts:
        if draft.target.key in current.keys and not draft.repair_note:
            continue
        if capacity <= 0 and not draft.repair_note:
            remaining.append(draft)
            continue
        try:
            if draft.repair_note:
                repair = current.repairs.get(draft.target.key)
                if not repair or repair[:2] != (
                    draft.repair_note,
                    draft.old_fingerprint,
                ):
                    raise ValidationError("Generated note changed during repair")
            draft.verify()
            for word, records in draft.audit.get("evidence", {}).items():
                if any(
                    record not in current.known.surfaces.get(word, [])
                    for record in records
                ):
                    raise ValidationError("Context evidence changed during generation")
            valid = validate(draft.candidate, draft.target, current.known, lexicon)
            if (
                not draft.repair_note
                and draft.target.kind == "vocabulary"
                and (draft.target.lemma, draft.target.pos) in current.known.meanings
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
        data = {
            "policy": POLICY,
            "target": asdict(draft.target),
            "key": draft.target.key,
            "candidate": asdict(c),
            "answer": c.answer,
            "form_key": valid.form_key,
            "mode": c.mode,
            "constructions": list(c.constructions),
        }
        audit = {**draft.audit, "status": "applied"}
        kind = (
            "Disambiguation"
            if c.mode != "word"
            else ("Conjugation" if draft.target.kind == "conjugation" else "")
        )
        values = {
            "Text": valid.text,
            "Translation": valid.translation,
            "Cue": html.escape(c.cue),
            "Target": html.escape(draft.target.lemma),
            "Meaning": html.escape(c.meaning),
            "Extra": html.escape(
                c.explanation
                if c.mode != "stem"
                else "Recall the missing part using the visible stem."
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
            [t for t in note.tags if t != REPAIR_TAG]
            if draft.repair_note
            else [
                TAG,
                KEY_TAG + draft.target.key,
                "french-frontier::" + draft.target.kind,
            ]
        )
        if c.mode != "word":
            note.tags.append("disambiguation")
        if draft.repair_note:
            col.update_note(note)
        else:
            col.add_note(note, DeckId(current.deck_id))
        if len(note.cards()) != 1:
            raise ValueError("French proposal unexpectedly created extra cards")
        admitted.append(int(note.id))
        current.keys.add(draft.target.key)
        capacity -= 0 if draft.repair_note else 1
    changes = col.merge_undo_entries(undo) if undo is not None else OpChanges()
    return Admission(changes, admitted, remaining, rejected)
