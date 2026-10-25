# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import html
import json
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from anki.collection import Collection, OpChanges
from anki.decks import DeckId
from anki.notes import NoteId

from .core import (
    CLOZE,
    EXCLUDE_TAG,
    GENERATED_TAG,
    MODEL_NAME,
    Candidate,
    Contrast,
    DeckBudget,
    Source,
    Validated,
    ValidationError,
    admission_slots,
    cloze_words,
    effective_limit,
    fingerprint,
    lookahead_days,
    plain_text,
    presentation_fingerprint,
    regular_limit,
    successful_days,
    validate_candidate,
)
from .generation import GENERATION_POLICY, Draft, validate_audit
from .lexicon import Lexicon, is_word

SETTINGS_KEY = "chineseFrontier"
LEGACY_FRONT = '<div class="sentence">{{cloze:Text}}</div>'
TRANSLATED_FRONT = LEGACY_FRONT + '<div class="translation">{{Translation}}</div>'
KIND_LABEL = '{{#CardKind}}<div class="card-kind">{{CardKind}}</div>{{/CardKind}}'
CURRENT_FRONT = KIND_LABEL + TRANSLATED_FRONT
DEFAULT_BACK = (
    '<div class="sentence">{{cloze:Text}}</div><hr id="answer">'
    "<div>{{Target}} · {{Pinyin}}</div><div>{{Meaning}}</div>"
    '<div class="translation">{{Translation}}</div>'
)

FIELDS = (
    "Text",
    "Target",
    "Pinyin",
    "Meaning",
    "Translation",
    "Dependencies",
    "Source",
    "Disambiguation",
    "CardKind",
)
METADATA_FIELDS = ("Source", "Dependencies", "Disambiguation")
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
    contexts: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lookahead_days: int = 0

    def slots(self, deck_id: int) -> int:
        return self.budget_slots(self.budgets.get(deck_id, []))

    def budget_slots(self, budgets: list[DeckBudget]) -> int:
        return admission_slots(
            budgets,
            ignore_review_limit=self.ignore_review_limit,
            lookahead_days=self.lookahead_days,
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


def _json_field(value: str) -> dict[str, Any]:
    try:
        result = json.loads(html.unescape(value))
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def existing_candidate(fields: dict[str, str], word: str) -> Candidate:
    """Read the actual visible examples, not a stale saved generator proposal."""
    parts = re.split(r"<br\s*/?>", fields["Text"], flags=re.IGNORECASE)
    parts = [part for part in parts if plain_text(part)]
    translations = re.split(r"<br\s*/?>", fields["Translation"], flags=re.IGNORECASE)
    if len(translations) == 1:
        divs = re.findall(r"<div[^>]*>(.*?)</div>", fields["Translation"], re.DOTALL)
        if len(divs) > 1:
            translations = divs
    translations = [plain_text(part) for part in translations if plain_text(part)]
    pinyins = [plain_text(part) for part in fields["Pinyin"].split("/")]

    def unfold(text: str) -> str:
        return plain_text(CLOZE.sub(lambda m: m[2].split("::", 1)[0], text))

    ordinals = {int(m[1]) for m in CLOZE.finditer(fields["Text"])}
    if len(set(word)) >= 2 and (
        len(ordinals) > 1
        or _json_field(fields.get("Source", "")).get("mode") == "characters"
    ):
        return Candidate(
            unfold(fields["Text"]),
            plain_text(fields["Translation"]),
            plain_text(fields["Pinyin"]),
            "",
            "characters",
            explanation=plain_text(fields["Meaning"]),
        )
    if len(parts) == 2 and len(translations) == 2 and len(pinyins) == 2:
        answers = cloze_words(parts[1], 0)
        if len(answers) != 1:
            raise ValidationError("Could not read existing contrast answer")
        return Candidate(
            unfold(parts[0]),
            translations[0],
            pinyins[0],
            "",
            "contrast",
            Contrast(
                answers[0],
                unfold(parts[1]),
                translations[1],
                pinyins[1],
                plain_text(fields["Meaning"]),
            ),
            plain_text(fields["Meaning"]),
        )
    if len(parts) != 1 or ordinals != {1}:
        raise ValidationError("Existing generated card has an unsupported cloze layout")
    return Candidate(
        unfold(parts[0]),
        plain_text(fields["Translation"]),
        plain_text(fields["Pinyin"]),
        "",
    )


def reported_confusions(fields: dict[str, str]) -> tuple[str, ...]:
    """Words the learner answered instead, recorded in the Source metadata."""
    reported = _json_field(fields.get("Source", "")).get("disambiguation", {})
    words = reported.get("with", ()) if isinstance(reported, dict) else ()
    words = (words,) if isinstance(words, str) else words
    return tuple(dict.fromkeys(w for w in words if isinstance(w, str) and is_word(w)))


def needs_audit(fields: dict[str, str]) -> bool:
    audit = _json_field(fields.get("Disambiguation", ""))
    return (
        audit.get("version") != GENERATION_POLICY
        or audit.get("status") != "applied"
        or audit.get("presentation_fingerprint") != presentation_fingerprint(fields)
    )


def _basic_words(
    fields: dict[str, str],
    model: dict[str, Any],
    ordinal: int,
    deck_name: str,
    front_field: str,
) -> tuple[tuple[str, ...], int]:
    template = model["tmpls"][ordinal] if ordinal < len(model["tmpls"]) else {}
    word = plain_text(fields.get(front_field, ""))
    hsk_level = 0
    # Quizlet imports annotate Front with a space and the HSK level.
    # Restrict stripping to HSK-labelled decks/models, not arbitrary text.
    hsk_context = deck_name + " " + model["name"]
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
    return words, hsk_level


def _repair_source(col: Collection, source: Source, note_id: int) -> Source:
    old_note = col.get_note(NoteId(note_id))
    previous = CLOZE.sub(lambda match: match[2].split("::", 1)[0], old_note["Text"])
    fields = dict(old_note.items())
    try:
        previous_candidate = asdict(existing_candidate(fields, source.word))
    except ValidationError:
        previous_candidate = None
    return replace(
        source,
        avoid_sentence=plain_text(previous),
        existing_candidate=previous_candidate,
        existing_fingerprint=presentation_fingerprint(fields),
        existing_cards=len(old_note.cards()),
        # A one-card pair cannot replace character siblings without removing one.
        reported_confusions=tuple(
            w for w in reported_confusions(fields) if w != source.word
        )
        if len(old_note.cards()) <= 1
        else (),
        force_repair=REPAIR_TAG in old_note.tags,
    )


def _establish_character_words(
    known: dict[str, str],
    evidence: dict[str, list[int]],
    character_words: dict[int, tuple[str, str]],
    character_evidence: dict[int, list[tuple[int, bool]]],
) -> None:
    for nid, items in character_evidence.items():
        word, definition = character_words[nid]
        if len(items) == len(set(word)) and all(learned for _cid, learned in items):
            known[word] = definition
            evidence.setdefault(word, []).extend(cid for cid, _learned in items)


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
    contexts: dict[str, list[dict[str, Any]]] = {}
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
    audit_notes: dict[str, tuple[int, dict[str, str], list[str]]] = {}
    character_evidence: dict[int, list[tuple[int, bool]]] = {}
    character_words: dict[int, tuple[str, str]] = {}
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
        generated = GENERATED_TAG in tags and model["name"] == MODEL_NAME
        source_data = _json_field(fields.get("Source", "")) if generated else {}
        key = source_data.get("key")
        in_audit_scope = (
            generated and home in primary_scope and nid in active_notes and not odid
        )
        repairing = generated and (
            REPAIR_TAG in tags or (in_audit_scope and needs_audit(fields))
        )
        if (
            in_audit_scope
            and repairing
            and isinstance(key, str)
            and not any(tag.casefold() in ("leech", EXCLUDE_TAG) for tag in tags)
        ):
            audit_notes[key] = (nid, fields, tags)
        characters = generated and source_data.get("mode") == "characters"
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
            if characters:
                words = ()
                word = str(source_data.get("word", ""))
                if is_word(word) and home in vocabulary_scope:
                    character_words[nid] = (
                        word,
                        str(source_data.get("definition", "")),
                    )
                    character_evidence.setdefault(nid, []).append(
                        (
                            cid,
                            queue != -1
                            and not repairing
                            and reps > 0
                            and successful_days(
                                review_logs.get(cid, []), timing.next_day_at
                            )
                            >= minimum_success_days,
                        )
                    )
                    if queue != -1 and not repairing:
                        covered_words.add(word)
            if (
                home in vocabulary_scope
                and queue != -1
                and not repairing
                and not characters
            ):
                # A clozed dictionary entry is not already a sentence card.
                for name, value in fields.items():
                    if "{{cloze:" + name + "}}" not in template["qfmt"]:
                        continue
                    context = CLOZE.sub("", plain_text(value))
                    if sum(is_word(char) for char in context) >= 3:
                        covered_words.update(cloze_words(value, ordinal))
        else:
            words, hsk_level = _basic_words(
                fields, model, ordinal, decks[home]["name"], front_field
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
                    known[word] = (
                        source_data.get("tested_words", {}).get(word, meaning)
                        if generated
                        else meaning
                    )
                elif meaning and meaning not in known[word]:
                    known[word] += "; " + meaning
                evidence.setdefault(word, []).append(cid)
                text = plain_text(
                    " ".join(
                        fields.get(name, "")
                        for name in ("Text", "Front", "Translation", "Extra")
                    )
                )
                text = CLOZE.sub(lambda m: m[2].split("::", 1)[0], text)
                record = {"card": cid, "context": text[:800]}
                if text and record not in contexts.setdefault(word, []):
                    contexts[word].append(record)
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
    _establish_character_words(known, evidence, character_words, character_evidence)
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
    repairs.update({key: item[0] for key, item in audit_notes.items()})
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
            source = _repair_source(col, source, repairs[source.key])
        sources.append(source)
    # Reserve capacity through *all* normal ancestors. This intentionally stays
    # within the Chinese parent's budget even when studying a child directly.
    budgets = _read_budgets(col, decks, deck_id, sources, day)
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
        contexts,
        lookahead_days(config),
    )


def _read_budgets(
    col: Collection,
    decks: dict[int, dict[str, Any]],
    deck_id: int,
    sources: list[Source],
    day: int,
) -> dict[int, list[DeckBudget]]:
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
            regular_limit(deck, conf, "new"),
        )
    budgets = {
        did: [budgets_by_id[ancestor] for ancestor in _ancestors(decks, did)]
        for did in {source.deck_id for source in sources} | {deck_id}
    }
    return budgets


def _hide_metadata(model: dict[str, Any]) -> bool:
    changed = False
    for item in model["flds"]:
        if item["name"] in METADATA_FIELDS and not (
            item["excludeFromSearch"] and item["collapsed"]
        ):
            item["excludeFromSearch"] = item["collapsed"] = True
            changed = True
    return changed


def ensure_model(col: Collection) -> dict[str, Any]:
    if model := col.models.by_name(MODEL_NAME):
        names = tuple(item["name"] for item in model["flds"])
        if (
            model["type"] != 1
            or names not in (FIELDS, FIELDS[:-2])
            or len(model["tmpls"]) != 1
        ):
            raise ValueError(
                "Chinese Frontier note type was modified; restore its required fields and cloze template"
            )
        changed = False
        if names == FIELDS[:-2]:
            for name in FIELDS[-2:]:
                col.models.add_field(model, col.models.new_field(name))
            changed = True
        template = model["tmpls"][0]
        if template["qfmt"] in (LEGACY_FRONT, TRANSLATED_FRONT):
            template["qfmt"] = CURRENT_FRONT
            changed = True
        if template["afmt"] == DEFAULT_BACK:
            template["afmt"] = KIND_LABEL + DEFAULT_BACK
            changed = True
        changed |= _hide_metadata(model)
        if changed:
            col.models.update_dict(model)
        return model
    model = col.models.new(MODEL_NAME)
    model["type"] = 1
    for name in FIELDS:
        col.models.add_field(model, col.models.new_field(name))
    _hide_metadata(model)
    template = col.models.new_template("Sentence")
    template["qfmt"] = CURRENT_FRONT
    template["afmt"] = KIND_LABEL + DEFAULT_BACK
    col.models.add_template(model, template)
    model["css"] = (
        ".card {font-family: sans-serif; font-size: 23px; text-align: center;} "
        ".sentence {font-size: 30px; line-height: 1.6;} .cloze {font-weight: bold; color: #2677bb;} "
        ".translation {margin-top: 1em; font-size: 19px;} .card-kind {font-size: 15px; color: #777; margin-bottom: 1em;} .nightMode .cloze {color: #74b8ef;}"
    )
    added = col.models.add_dict(model)
    stored = col.models.get(added.id)
    assert stored is not None
    return stored


def _revert_unreported_contrast(note: Any) -> bool:
    """Contrast pairs once papered over ambiguous examples. Without a reported
    confusion, keep only the first example; the changed card is then re-audited."""
    fields = dict(note.items())
    data = _json_field(note["Source"])
    word = data.get("word")
    if not isinstance(word, str) or reported_confusions(fields):
        return False
    try:
        candidate = existing_candidate(fields, word)
    except ValidationError:
        return False
    if candidate.mode != "contrast" or candidate.sentence.count(word) != 1:
        return False
    note["Text"] = html.escape(candidate.sentence).replace(
        word, "{{c1::" + word + "}}", 1
    )
    note["Translation"] = html.escape(candidate.translation)
    note["Pinyin"] = html.escape(candidate.pinyin)
    note["Target"] = word
    note["Meaning"] = html.escape(str(data.get("definition", "")))
    note["CardKind"] = ""
    data["mode"] = "word"
    tested = data.get("tested_words")
    if isinstance(tested, dict):
        data["tested_words"] = {k: v for k, v in tested.items() if k == word}
    note["Source"] = html.escape(json.dumps(data, ensure_ascii=False))
    note.tags = [tag for tag in note.tags if tag != "disambiguation"]
    return True


def _is_character_cue(note: Any) -> bool:
    if note["CardKind"] == "Disambiguation · character recall":
        return True
    data = _json_field(note["Source"])
    if data.get("mode") == "characters":
        return True
    word = data.get("word")
    try:
        return (
            isinstance(word, str)
            and existing_candidate(dict(note.items()), word).mode == "characters"
        )
    except ValidationError:
        return False


def upgrade_presentation(col: Collection) -> OpChanges:
    if col.models.by_name(MODEL_NAME) is None:
        return OpChanges()
    undo = col.add_custom_undo_entry("Upgrade Chinese disambiguation cards")
    ensure_model(col)
    notes = []
    for nid in col.find_notes(f'"note:{MODEL_NAME}" tag:{GENERATED_TAG}'):
        note = col.get_note(nid)
        reverted = _revert_unreported_contrast(note)
        text = CLOZE.sub(
            lambda m: "{{c" + m[1] + "::" + m[2].split("::", 1)[0] + "}}", note["Text"]
        )
        kind = note["CardKind"]
        tags = note.tags
        if _is_character_cue(note):
            # A visible character is a cue, not a confusion; it needs no label.
            kind = ""
            tags = [tag for tag in tags if tag != "disambiguation"]
        elif kind == "Disambiguation · contrast pair" or (
            not kind and "disambiguation" in tags
        ):
            kind = "Disambiguation"
        if (
            reverted
            or text != note["Text"]
            or kind != note["CardKind"]
            or tags != note.tags
        ):
            note["Text"] = text
            note["CardKind"] = kind
            note.tags = tags
            notes.append(note)
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
    added_cards: int = 0


def request_repair(col: Collection, note_id: int) -> OpChanges:
    note = col.get_note(NoteId(note_id))
    if GENERATED_TAG not in note.tags or note.note_type()["name"] != MODEL_NAME:
        raise ValueError(
            "Only generated Chinese Frontier notes can be replaced automatically"
        )
    if REPAIR_TAG not in note.tags:
        note.tags.append(REPAIR_TAG)
    return col.update_note(note)


def report_confusion(col: Collection, note_id: int, word: str) -> OpChanges:
    """Record the word the learner answered instead; the repair contrasts the two."""
    note = col.get_note(NoteId(note_id))
    if GENERATED_TAG not in note.tags or note.note_type()["name"] != MODEL_NAME:
        raise ValueError("Only generated Chinese Frontier notes can record a confusion")
    if len(note.cards()) > 1:
        raise ValueError(
            "This note already has character cards; a contrast pair would remove one"
        )
    word = word.strip()
    data = _json_field(note["Source"])
    if not is_word(word) or word == data.get("word"):
        raise ValueError("Enter the other Chinese word you answered")
    words = tuple(dict.fromkeys((*reported_confusions(dict(note.items())), word)))
    previous = data.get("disambiguation")
    previous = previous if isinstance(previous, dict) else {}
    data["disambiguation"] = {
        **previous,
        "with": words[0] if len(words) == 1 else list(words),
        "feedback": "Learner answered " + ", ".join(words) + ".",
        "previous_text": previous.get("previous_text", note["Text"]),
    }
    note["Source"] = html.escape(json.dumps(data, ensure_ascii=False))
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


def _write_candidate_fields(
    note: Any,
    source: Source,
    valid: Validated,
    text: str,
    evidence: dict[str, list[int]],
    preserve_source: bool,
) -> None:
    note["Text"] = text
    note["Target"] = source.word
    note["Pinyin"] = html.escape(valid.candidate.pinyin)
    note["Meaning"] = html.escape(valid.candidate.explanation or source.definition)
    note["Translation"] = valid.translation
    tested_words = {source.word: source.definition}
    if other := valid.candidate.contrast:
        note["Target"] += " / " + other.word
        note["Pinyin"] += " / " + html.escape(other.pinyin)
        tested_words[other.word] = other.definition
    note["Dependencies"] = html.escape(
        json.dumps(
            {word: evidence.get(word, []) for word in valid.dependencies},
            ensure_ascii=False,
        )
    )
    metadata = _json_field(note["Source"]) if preserve_source else {}
    metadata.update(
        card_id=source.card_id,
        note_id=source.note_id,
        key=source.key,
        word=source.word,
        definition=source.definition,
        fingerprint=source.fingerprint,
        mode=valid.candidate.mode,
        tested_words=tested_words,
    )
    note["Source"] = html.escape(json.dumps(metadata, ensure_ascii=False))


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
    added_cards = 0
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
            or source.existing_fingerprint != draft.source.existing_fingerprint
        ):
            rejected[draft.source.key] = (
                "Source changed, was unsuspended, or is already represented"
            )
            continue
        if draft.audit_only and draft.audit.get("status") == "deferred":
            try:
                validate_audit(draft)
            except ValidationError as exc:
                rejected[source.key] = str(exc)
                continue
            repair_id = current.repairs.get(source.key)
            if not repair_id:
                rejected[source.key] = "Deferred audit has no existing note"
                continue
            if model is None:
                undo = col.add_custom_undo_entry("Audit Chinese sentence cards")
                model = ensure_model(col)
            note = col.get_note(NoteId(repair_id))
            audit = dict(draft.audit)
            audit["presentation_fingerprint"] = presentation_fingerprint(
                dict(note.items())
            )
            note["Disambiguation"] = html.escape(
                json.dumps(audit, ensure_ascii=False, indent=2)
            )
            col.update_note(note)
            if col.get_note(note.id)["Disambiguation"] != note["Disambiguation"]:
                raise ValueError("Deferred audit field failed verification")
            updated.append(int(note.id))
            rejected[source.key] = str(
                audit.get("reason", "No unambiguous fallback passed review")
            )
            continue
        try:
            validate_audit(draft)
            valid = validate_candidate(
                draft.candidate, source, set(current.known), lexicon
            )
            if valid.card_count < source.existing_cards:
                raise ValidationError("Repair would delete an existing sibling card")
        except ValueError as exc:
            rejected[source.key] = str(exc)
            continue
        budgets = current.budgets[source.deck_id]
        adjusted = [
            replace(budget, pending=budget.pending + reserved.get(budget.deck_id, 0))
            for budget in budgets
        ]
        repair_id = current.repairs.get(source.key)
        extra_cards = max(0, valid.card_count - source.existing_cards)
        capacity = current.budget_slots(adjusted)
        staged_character = (
            valid.candidate.mode == "characters"
            and extra_cards > capacity
            and source.existing_cards < valid.card_count
            and (bool(repair_id) or capacity >= 1)
        )
        if extra_cards > capacity and not repair_id and not staged_character:
            remaining.append(draft)
            continue
        expected_count = (
            max(1, source.existing_cards + capacity)
            if staged_character
            else valid.card_count
        )
        rendered_text = valid.text
        if staged_character:
            rendered_text = CLOZE.sub(
                lambda m: m[0] if int(m[1]) <= expected_count else m[2], rendered_text
            )
            extra_cards = max(0, expected_count - source.existing_cards)
        if model is None:
            undo = col.add_custom_undo_entry("Update Chinese sentence cards")
            model = ensure_model(col)
        note = col.get_note(NoteId(repair_id)) if repair_id else col.new_note(model)
        old_cards = (
            {
                int(c.ord): (
                    int(c.id),
                    c.type,
                    c.queue,
                    c.reps,
                    c.lapses,
                    c.ivl,
                    c.due,
                    c.factor,
                )
                for c in note.cards()
            }
            if repair_id
            else {}
        )
        audit = dict(draft.audit)
        if extra_cards > capacity and not staged_character:
            # The audit is visible in the editor immediately; no unaffordable sibling
            # is activated or silently suspended to evade the new-card allowance.
            audit.update(
                status="pending",
                required_new_cards=extra_cards,
                presentation_fingerprint=source.existing_fingerprint,
            )
            note["Disambiguation"] = html.escape(
                json.dumps(audit, ensure_ascii=False, indent=2)
            )
            col.update_note(note)
            remaining.append(draft)
            updated.append(int(note.id))
            continue
        if not draft.audit_only or valid.card_count != source.existing_cards:
            _write_candidate_fields(
                note, source, valid, rendered_text, current.evidence, bool(repair_id)
            )
        # Only a pair answering a reported confusion is labelled; cues are not.
        kind = "Disambiguation" if valid.candidate.mode == "contrast" else ""
        note["CardKind"] = kind
        audit.update(
            status="pending" if staged_character else "applied",
            presentation_fingerprint=presentation_fingerprint(dict(note.items())),
        )
        if staged_character:
            audit["required_new_cards"] = valid.card_count - expected_count
        note["Disambiguation"] = html.escape(
            json.dumps(audit, ensure_ascii=False, indent=2)
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
        note.tags = [tag for tag in note.tags if tag != "disambiguation"]
        if kind:
            note.tags.append("disambiguation")
        if repair_id:
            col.update_note(note)
        else:
            col.add_note(note, DeckId(source.deck_id))
        stored = col.get_note(NoteId(note.id))
        cards = stored.cards()
        if (
            (not draft.audit_only and stored["Text"] != rendered_text)
            or len(cards) != expected_count
            or stored["Disambiguation"] != note["Disambiguation"]
        ):
            raise ValueError("Replacement card count or fields failed verification")
        for card in cards:
            ordinal = int(card.ord)
            if ordinal in old_cards:
                state = (
                    int(card.id),
                    card.type,
                    card.queue,
                    card.reps,
                    card.lapses,
                    card.ivl,
                    card.due,
                    card.factor,
                )
                if state != old_cards[ordinal]:
                    raise ValueError("Repair changed an existing card's scheduling")
            elif card.type != 0:
                raise ValueError("Additional sibling was not a new card")
            question, answer = card.question(), card.answer()
            if (
                'class="cloze"' not in question
                or source.word not in plain_text(answer)
                or valid.candidate.translation not in plain_text(question)
            ):
                raise ValueError("Replacement card rendering failed verification")
            if kind and (kind not in question or kind not in answer):
                raise ValueError("Disambiguation label missing from a card side")
            if _template_tests_field(
                {"qfmt": card.template()["qfmt"]}, "Disambiguation"
            ) or _template_tests_field(
                {"qfmt": card.template()["afmt"]}, "Disambiguation"
            ):
                raise ValueError(
                    "Disambiguation audit must remain hidden during reviews"
                )
        added_cards += extra_cards
        (updated if repair_id else admitted).append(int(note.id))
        if staged_character:
            remaining.append(
                replace(
                    draft,
                    source=replace(
                        source,
                        existing_cards=expected_count,
                        existing_fingerprint=presentation_fingerprint(
                            dict(stored.items())
                        ),
                        existing_candidate=asdict(valid.candidate),
                        force_repair=False,
                    ),
                )
            )
        current.existing_keys.add(source.key)
        current.covered_words.add(source.word)
        # The same batch can contain duplicate source keys after crash recovery.
        sources = {
            cid: value
            for cid, value in sources.items()
            if value.key != source.key and value.word != source.word
        }
        for budget in budgets:
            reserved[budget.deck_id] = reserved.get(budget.deck_id, 0) + extra_cards
    if undo is not None:
        changes = col.merge_undo_entries(undo)
    return AdmissionResult(changes, admitted, remaining, rejected, updated, added_cards)
