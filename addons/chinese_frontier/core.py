# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Any

from .lexicon import Lexicon, is_word

MODEL_NAME = "Chinese Frontier"
GENERATED_TAG = "chinese-frontier::generated"
EXCLUDE_TAG = "chinese-frontier::exclude"
# A learner hands over a note that kept failing repair; automation leaves it alone.
MANUAL_TAG = "chinese-frontier::manual"
FEEDBACK_REASONS = {
    "unnatural": "Sounds unnatural",
    "ambiguous": "Another word fits the blank",
    "too_hard": "Unfamiliar words or grammar",
    "wrong_sense": "Tests a different sense of the word",
    "giveaway": "Gives the answer away",
}
CLOZE = re.compile(r"\{\{c(\d+)::([^{}]*?)\}\}")


class ValidationError(ValueError):
    pass


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "div", "p"):
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("div", "p"):
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _PlainText()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def cloze_words(text: str, ordinal: int) -> tuple[str, ...]:
    return tuple(
        word
        for match in CLOZE.finditer(text)
        if int(match[1]) == ordinal + 1
        if is_word(word := plain_text(match[2].split("::", 1)[0]))
    )


def successful_days(review_log: list[tuple[int, int]], next_day_at: int) -> int:
    """Count Good/Easy on different local study days, since the last Again.

    Use local calendar dates with the scheduler's rollover time, rather than
    dividing Unix timestamps by 24h (which fails near rollover and DST).
    """
    cutoff = datetime.fromtimestamp(next_day_at)
    shift = timedelta(hours=cutoff.hour, minutes=cutoff.minute, seconds=cutoff.second)
    days = set()
    for timestamp_ms, ease in sorted(review_log):
        if ease == 1:
            days.clear()
        elif ease in (3, 4):
            days.add((datetime.fromtimestamp(timestamp_ms / 1000) - shift).date())
    return len(days)


@dataclass(frozen=True)
class Source:
    card_id: int
    note_id: int
    deck_id: int
    word: str
    definition: str
    fingerprint: str
    avoid_sentence: str = ""
    hsk_level: int = 0
    existing_candidate: dict[str, Any] | None = None
    existing_fingerprint: str = ""
    existing_cards: int = 0
    # Words the learner answered instead; the only words a contrast pair may test.
    reported_confusions: tuple[str, ...] = ()
    force_repair: bool = False
    # The learner's complaints about earlier versions of this card.
    learner_feedback: tuple[str, ...] = ()
    avoid_words: tuple[str, ...] = ()
    rejected_sentences: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        sense = " ".join(self.definition.casefold().split())
        return hashlib.sha256(f"{self.word}\n{sense}".encode()).hexdigest()

    def context(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "word": self.word,
            "definition": self.definition,
            "avoid_sentence": self.avoid_sentence,
            "existing_candidate": self.existing_candidate,
            "reported_confusions": self.reported_confusions,
            "learner_feedback": self.learner_feedback,
            "avoid_words": self.avoid_words,
            "rejected_sentences": self.rejected_sentences,
        }


def fingerprint(fields: dict[str, str], tags: list[str]) -> str:
    return hashlib.sha256(
        json.dumps([fields, sorted(tags)], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Contrast:
    word: str
    sentence: str
    translation: str
    pinyin: str
    definition: str


@dataclass(frozen=True)
class Candidate:
    sentence: str
    translation: str
    pinyin: str
    hint: str
    mode: str = "word"
    contrast: Contrast | None = None
    explanation: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> Candidate:
        if not isinstance(value, dict):
            raise ValidationError("Candidate must be an object")
        names = ("sentence", "translation", "pinyin", "hint")
        if any(not isinstance(value.get(name), str) for name in names):
            raise ValidationError("Candidate fields must be strings")
        mode = value.get("mode", "word")
        explanation = value.get("explanation", "")
        if mode not in ("word", "contrast", "characters") or not isinstance(
            explanation, str
        ):
            raise ValidationError("Invalid disambiguation mode or explanation")
        contrast = None
        if item := value.get("contrast"):
            if not isinstance(item, dict) or any(
                not isinstance(item.get(name), str)
                for name in Contrast.__dataclass_fields__
            ):
                raise ValidationError("Malformed contrast example")
            contrast = Contrast(
                **{name: item[name].strip() for name in Contrast.__dataclass_fields__}
            )
        return cls(
            *(value[name].strip() for name in names),
            mode,
            contrast,
            explanation.strip(),
        )


@dataclass(frozen=True)
class Validated:
    candidate: Candidate
    text: str
    dependencies: tuple[str, ...]
    translation: str = ""
    card_count: int = 1


def validate_candidate(
    candidate: Candidate, source: Source, known: set[str], lexicon: Lexicon
) -> Validated:
    sentence = candidate.sentence
    if (
        source.force_repair
        and candidate.mode == "word"
        and source.avoid_sentence
        and sentence == source.avoid_sentence
    ):
        raise ValidationError("Replace the rejected sentence with a different example")
    if sentence in source.rejected_sentences:
        raise ValidationError("The learner rejected this sentence")
    rest = sentence.replace(source.word, "", 1)
    if avoided := [w for w in source.avoid_words if w in rest]:
        raise ValidationError("The learner asked to avoid: " + ", ".join(avoided))
    if not 3 <= len(sentence) <= 60 or any(c in sentence for c in "<>{}\n\r"):
        raise ValidationError("Use a short plain Chinese sentence without markup")
    if sentence.count(source.word) != 1:
        raise ValidationError("Target must occur exactly once")
    if not candidate.translation or len(candidate.translation) > 500:
        raise ValidationError("A short English translation is required")
    if not candidate.pinyin or len(candidate.pinyin) > 100:
        raise ValidationError("Target pronunciation is required")
    if len(candidate.hint) > 80 or any(c in candidate.hint for c in "<>{}:"):
        raise ValidationError("Use a short plain meaning hint")
    if source.word in candidate.hint:
        raise ValidationError("Hint reveals the Chinese answer")
    tokens = lexicon.segment(sentence, extra=known | {source.word})
    if tokens.count(source.word) != 1:
        raise ValidationError("Cloze a whole word, not part of a compound")
    dependencies = tuple(dict.fromkeys(word for word in tokens if word != source.word))
    unknown = [word for word in dependencies if word not in known]
    if unknown:
        raise ValidationError("Unlearned context: " + ", ".join(unknown))
    if candidate.mode not in ("word", "contrast", "characters"):
        raise ValidationError("Invalid disambiguation mode")
    if len(candidate.explanation) > 1500:
        raise ValidationError("Disambiguation explanation is too long")
    replacement = "{{c1::" + source.word + "}}"
    count = 1
    if candidate.mode == "characters":
        characters = tuple(dict.fromkeys(source.word))
        if len(characters) < 2:
            raise ValidationError(
                "Character fallback needs different characters; repeated characters reveal no useful cue"
            )
        ordinals = {char: i + 1 for i, char in enumerate(characters)}
        replacement = "".join(
            "{{c" + str(ordinals[char]) + "::" + char + "}}" for char in source.word
        )
        count = len(characters)
    text = html.escape(sentence).replace(source.word, replacement, 1)
    translation = html.escape(candidate.translation)
    if candidate.mode == "contrast":
        other = candidate.contrast
        if other is None or other.word == source.word or not is_word(other.word):
            raise ValidationError("A different whole-word contrast is required")
        if other.word not in source.reported_confusions:
            raise ValidationError("Contrast pairs only answer a reported confusion")
        other_source = Source(
            0,
            0,
            0,
            other.word,
            other.definition,
            "contrast",
            avoid_words=tuple(w for w in source.avoid_words if w != other.word),
        )
        other_valid = validate_candidate(
            Candidate(other.sentence, other.translation, other.pinyin, ""),
            other_source,
            known,
            lexicon,
        )
        text = "<div>" + text + "</div><br><div>" + other_valid.text + "</div>"
        if source.word in other.sentence.replace(
            other.word, "", 1
        ) or other.word in sentence.replace(source.word, "", 1):
            raise ValidationError("A contrast example reveals the other answer")
        translation = (
            "<div>"
            + translation
            + "</div><br><div>"
            + other_valid.translation
            + "</div>"
        )
        dependencies = tuple(dict.fromkeys((*dependencies, *other_valid.dependencies)))
    elif candidate.contrast is not None:
        raise ValidationError("Only contrast mode may contain a second example")
    elif source.reported_confusions:
        raise ValidationError("A reported confusion needs a contrast pair")
    return Validated(candidate, text, dependencies, translation, count)


def presentation_fingerprint(fields: dict[str, str]) -> str:
    return fingerprint(
        {
            name: fields.get(name, "")
            for name in ("Text", "Translation", "Target", "Pinyin", "Meaning")
        },
        [],
    )


@dataclass(frozen=True)
class DeckBudget:
    deck_id: int
    new_limit: int
    introduced: int
    pending: int
    review_limit: int
    reviewed: int
    due_reviews: int
    # The limit without a "today only" override; defaults to new_limit.
    daily_new_limit: int | None = None


def lookahead_days(config: dict[str, Any]) -> int:
    return max(0, min(7, int(config.get("lookahead_days", 0))))


def admission_slots(
    budgets: list[DeckBudget],
    *,
    ignore_review_limit: bool = False,
    lookahead_days: int = 0,
) -> int:
    """Reserve *all* active/buried new cards and due reviews in each budget.

    Deliberately conservative: sibling burying and gather order may make some
    reserved cards unavailable today, but cannot cause over-admission.

    `lookahead_days` keeps that many future days of new cards generated, so a
    day's study never waits on generation. Only today's share is checked
    against today's review limit; Anki's own new limit keeps the rest hidden.
    """
    if not budgets:
        return 0
    remaining = []
    for budget in budgets:
        today = budget.new_limit - budget.introduced
        if not ignore_review_limit:
            today = min(
                today,
                budget.review_limit
                - budget.reviewed
                - budget.introduced
                - budget.due_reviews,
            )
        daily = (
            budget.new_limit
            if budget.daily_new_limit is None
            else budget.daily_new_limit
        )
        target = max(0, today) + lookahead_days * max(0, daily)
        remaining.append(max(0, target - budget.pending))
    return min(remaining)


def effective_limit(
    deck: dict[str, Any], config: dict[str, Any], kind: str, today: int
) -> int:
    override = deck.get(f"{kind}LimitToday")
    if isinstance(override, dict) and override.get("today") == today:
        return int(override["limit"])
    return regular_limit(deck, config, kind)


def regular_limit(deck: dict[str, Any], config: dict[str, Any], kind: str) -> int:
    value = deck.get(f"{kind}Limit")
    if value is not None:
        return int(value)
    return int(config["new" if kind == "new" else "rev"]["perDay"])
