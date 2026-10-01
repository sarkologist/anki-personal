"""No Anki dependency: rendered card extraction, retrieval, and score validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.targets: list[str] = []
        self.hidden = 0
        self.cloze = 0
        self.has_media = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("img", "audio", "video", "svg"):
            self.has_media = True
        if tag == "span":
            if self.cloze or "cloze" in dict(attrs).get("class", "").split():
                self.cloze += 1
        if tag in ("br", "div", "p", "li", "hr"):
            self.handle_data(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if tag == "span" and self.cloze:
            self.cloze -= 1
            if not self.cloze:
                self.targets.append(" ")
        if tag in ("div", "p", "li"):
            self.handle_data(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)
            if self.cloze:
                self.targets.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self.parts).split())


@dataclass(frozen=True)
class CardText:
    card_id: int
    note_id: int
    question: str
    answer: str
    target: str
    has_media: bool
    fingerprint: str

    @classmethod
    def from_html(cls, cid: int, nid: int, question: str, answer: str) -> CardText:
        front, back = _Text(), _Text()
        front.feed(question)
        back.feed(answer)
        fingerprint = hashlib.sha256(
            json.dumps([cid, nid, question, answer], ensure_ascii=False).encode()
        ).hexdigest()
        return cls(
            cid,
            nid,
            front.text,
            back.text,
            " ".join("".join(back.targets).split()),
            front.has_media or back.has_media,
            fingerprint,
        )

    def prompt_data(self) -> dict:
        data = asdict(self)
        data.pop("fingerprint")
        # Bound unusually large templates; mark truncation for the assessor.
        data["truncated"] = len(self.question) > 5000 or len(self.answer) > 6000
        data["question"] = self.question[:5000]
        data["answer"] = self.answer[:6000]
        return data


def tokens(text: str) -> Counter:
    # Token overlap retrieves candidates; it never determines semantic redundancy.
    words = re.findall(r"[^\W\d_]{2,}", text.lower())
    return Counter(words)


class ReferenceIndex:
    def __init__(self, cards: list[CardText]) -> None:
        self.cards = {c.card_id: c for c in cards}
        self.siblings: dict[int, list[int]] = defaultdict(list)
        terms = {}
        counts: Counter = Counter()
        for c in cards:
            self.siblings[c.note_id].append(c.card_id)
            terms[c.card_id] = tokens(c.question + " " + c.target + " " + c.target)
            counts.update(terms[c.card_id].keys())
        self.idf = {t: math.log(1 + len(cards) / (1 + n)) for t, n in counts.items()}
        self.postings: dict[str, list[tuple[int, float]]] = defaultdict(list)
        for cid, words in terms.items():
            vector = self.vector(words)
            for term, weight in vector.items():
                self.postings[term].append((cid, weight))
        self.version = hashlib.sha256(
            "".join(
                c.fingerprint for c in sorted(cards, key=lambda c: c.card_id)
            ).encode()
        ).hexdigest()

    def vector(self, words: Counter) -> dict[str, float]:
        raw = {t: (1 + math.log(n)) * self.idf.get(t, 0) for t, n in words.items()}
        norm = math.sqrt(sum(w * w for w in raw.values())) or 1
        return {t: w / norm for t, w in raw.items() if w}

    def nearest(self, card: CardText, limit: int = 8) -> list[CardText]:
        similarities: dict[int, float] = defaultdict(float)
        for term, weight in self.vector(
            tokens(card.question + " " + card.target + " " + card.target)
        ).items():
            for cid, other in self.postings.get(term, []):
                similarities[cid] += weight * other
        # Include sibling clozes, but let the semantic assessment distinguish targets.
        siblings = self.siblings.get(card.note_id, [])[:limit]
        ranked = sorted(similarities, key=lambda cid: (-similarities[cid], cid))
        ids = list(dict.fromkeys([*siblings, *ranked]))[:limit]
        return [self.cards[cid] for cid in ids if cid != card.card_id]


def priority_score(conceptual: int, computation: int, overlap: int) -> int:
    c, k, o = conceptual / 4, computation / 4, overlap / 4
    return round(
        max(0, min(100, 100 * (0.25 + 0.75 * c) * (1 - 0.85 * o) - 35 * k - 20 * o * k))
    )


def validate_batch(
    values: list[dict],
    cards: list[CardText],
    allowed: dict[int, set[int]],
    reference_version: str,
) -> dict[str, dict]:
    expected = {c.card_id: c for c in cards}
    results = {}
    for value in values:
        cid = value.get("card_id")
        if type(cid) is not int or cid not in expected or str(cid) in results:
            raise ValueError("Unexpected or repeated card ID in scoring response")
        for key in ("conceptual", "computation", "overlap"):
            if type(value.get(key)) is not int or not 0 <= value[key] <= 4:
                raise ValueError(f"Invalid {key} rating")
        if value.get("confidence") not in ("high", "medium", "low"):
            raise ValueError("Invalid confidence")
        if not isinstance(value.get("reason"), str) or not value["reason"].strip():
            raise ValueError("Missing score explanation")
        matches = value.get("matches")
        if not isinstance(matches, list) or any(
            type(m) is not int or m not in allowed[cid] for m in matches
        ):
            raise ValueError("Overlap evidence refers to an unprovided card")
        if value["overlap"] > 0 and not matches:
            raise ValueError("Overlap needs supporting card IDs")
        result = dict(value)
        if not expected[cid].question and not expected[cid].answer:
            result["confidence"] = "low"
        result["score"] = (
            None
            if result["confidence"] == "low"
            else priority_score(
                value["conceptual"], value["computation"], value["overlap"]
            )
        )
        result.update(
            fingerprint=expected[cid].fingerprint,
            reference_version=reference_version,
            note_id=expected[cid].note_id,
            scored_at=datetime.now(timezone.utc).isoformat(),
        )
        results[str(cid)] = result
    if set(results) != {str(cid) for cid in expected}:
        raise ValueError("Scoring response omitted cards")
    return results


def sort_ids(ids: list[int], scores: dict[str, dict], descending: bool) -> list[int]:
    def key(cid: int) -> tuple:
        score = scores.get(str(cid), {}).get("score")
        return (
            score is None,
            (-score if descending else score) if score is not None else 0,
            cid,
        )

    return sorted(ids, key=key)


def load_scores(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not isinstance(data.get("scores"), dict):
        raise ValueError("Unsupported priority score file")
    return data["scores"]


def save_scores(path: Path, scores: dict[str, dict]) -> None:
    # Replacing atomically keeps the previous batch intact if the process stops.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"version": 1, "scores": scores}, ensure_ascii=False, indent=2)
    )
    temporary.replace(path)
