# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Planning aids only; live evidence and independent admission remain authoritative."""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from typing import Any, Iterable

STOP = set(
    "the and that this with from have has for are was les des une dans pour avec est son ses elle qui que aux sur pas par comme".split()
)


def terms(text: str) -> set[str]:
    words = {
        w
        for w in re.findall(r"[^\W\d_]+", text.casefold())
        if len(w) > 2 and w not in STOP
    }
    for han in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(han[i : i + 2] for i in range(len(han) - 1))
    return words


def brief(text: str, limit: int) -> str:
    """Shorten prompt evidence at a gloss boundary where one is close."""
    if len(text) <= limit:
        return text
    cut = text.rfind("; ", 0, limit)
    return text[:cut] if cut >= limit // 2 else text[: limit - 1].rstrip() + "…"


def context_bundle(
    records: dict[str, list[str]], query: str, common: Iterable[str], limit: int = 96
) -> list[str]:
    """Retrieve proven vocabulary; words merely visible in evidence never become keys."""
    common = [w for w in dict.fromkeys(common) if w in records]
    anchors = common[: min(32, max(1, limit // 3))]
    documents = {
        w: terms(w + " " + " ".join(examples)) for w, examples in records.items()
    }
    frequency = Counter(t for document in documents.values() for t in document)
    wanted = terms(query)
    order = {w: i for i, w in enumerate(common)}

    def score(word: str) -> float:
        return sum(
            math.log((len(records) + 1) / (frequency[t] + 1)) + 1
            for t in documents[word] & wanted
        )

    ranked = sorted(records, key=lambda w: (-score(w), order.get(w, len(order)), w))
    return list(dict.fromkeys(anchors + ranked))[: max(1, limit)]


def read_lesson(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        decoded = value
    return decoded if isinstance(decoded, dict) else {"reason": str(decoded)[:1600]}


TIMEOUT_LESSON = "The model call timed out before answering."


def pack_lesson(value: Any) -> str:
    """Keep actionable fields, never truncate a JSON document halfway through."""
    value = read_lesson(value)
    result: dict[str, Any] = {
        "reason": str(value.get("reason", "No valid proposal"))[:1200]
    }
    if value.get("writer_reason"):
        result["writer_reason"] = str(value["writer_reason"])[:1200]
    for name in ("alternatives", "context_checks"):
        rows = value.get(name, [])
        if isinstance(rows, list):
            result[name] = [
                {
                    k: (v[:180] if isinstance(v, str) else v)
                    for k, v in row.items()
                    if k
                    in (
                        "answer",
                        "word",
                        "example",
                        "ordinal",
                        "fits_context",
                        "fits_cloze",
                        "sufficient",
                        "reason",
                    )
                }
                for row in rows[:8]
                if isinstance(row, dict)
            ]
    candidate = value.get("rejected_candidate")
    if isinstance(candidate, dict):
        result["rejected_candidate"] = {
            k: str(candidate[k])[:400]
            for k in ("sentence", "answer", "translation", "mode")
            if k in candidate
        }
    return json.dumps(result, ensure_ascii=False)


def previous_lessons(deferred: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {
        key: read_lesson(pack_lesson(deferred[key].get("reason", "")))
        for key in keys
        if key in deferred
    }


def reviewed_lessons(deferred: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """A target's latest failure, if it was an audit of a real sentence. A writer's
    refusal is left out: shown again, it talks the next attempt into refusing the
    same way."""
    return {
        key: {k: v for k, v in lesson.items() if k != "writer_reason"}
        for key, lesson in previous_lessons(deferred, keys).items()
        if lesson.get("alternatives")
        or lesson.get("context_checks")
        or lesson.get("rejected_candidate")
    }


def buffer_size(config: dict[str, Any]) -> int:
    return max(0, min(16, int(config.get("buffer_notes", 0))))


def preparation_needed(slots: int, queued: int, config: dict[str, Any]) -> int:
    return max(0, max(0, slots) + buffer_size(config) - max(0, queued))
