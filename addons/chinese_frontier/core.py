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
        }


def fingerprint(fields: dict[str, str], tags: list[str]) -> str:
    return hashlib.sha256(
        json.dumps([fields, sorted(tags)], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Candidate:
    sentence: str
    translation: str
    pinyin: str
    hint: str

    @classmethod
    def from_dict(cls, value: Any) -> Candidate:
        if not isinstance(value, dict):
            raise ValidationError("Candidate must be an object")
        names = ("sentence", "translation", "pinyin", "hint")
        if any(not isinstance(value.get(name), str) for name in names):
            raise ValidationError("Candidate fields must be strings")
        return cls(*(value[name].strip() for name in names))


@dataclass(frozen=True)
class Validated:
    candidate: Candidate
    text: str
    dependencies: tuple[str, ...]


def validate_candidate(
    candidate: Candidate, source: Source, known: set[str], lexicon: Lexicon
) -> Validated:
    sentence = candidate.sentence
    if source.avoid_sentence and sentence == source.avoid_sentence:
        raise ValidationError("Replace the rejected sentence with a different example")
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
    text = html.escape(sentence).replace(source.word, "{{c1::" + source.word + "}}", 1)
    return Validated(candidate, text, dependencies)


@dataclass(frozen=True)
class DeckBudget:
    deck_id: int
    new_limit: int
    introduced: int
    pending: int
    review_limit: int
    reviewed: int
    due_reviews: int


def admission_slots(
    budgets: list[DeckBudget], *, ignore_review_limit: bool = False
) -> int:
    """Reserve *all* active/buried new cards and due reviews in each budget.

    Deliberately conservative: sibling burying and gather order may make some
    reserved cards unavailable today, but cannot cause over-admission.
    """
    if not budgets:
        return 0
    remaining = []
    for budget in budgets:
        new = budget.new_limit - budget.introduced - budget.pending
        if not ignore_review_limit:
            review = (
                budget.review_limit
                - budget.reviewed
                - budget.introduced
                - budget.due_reviews
                - budget.pending
            )
            new = min(new, review)
        remaining.append(max(0, new))
    return min(remaining)


def effective_limit(
    deck: dict[str, Any], config: dict[str, Any], kind: str, today: int
) -> int:
    override = deck.get(f"{kind}LimitToday")
    if isinstance(override, dict) and override.get("today") == today:
        return int(override["limit"])
    value = deck.get(f"{kind}Limit")
    if value is not None:
        return int(value)
    return int(config["new" if kind == "new" else "rev"]["perDay"])
