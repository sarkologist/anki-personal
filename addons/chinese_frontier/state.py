# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import refill
from .generation import GENERATION_POLICY, Draft
from .preparation import pack_lesson


def vocabulary_version(known: dict[str, str]) -> str:
    return hashlib.sha256(
        json.dumps(
            [GENERATION_POLICY, known], sort_keys=True, ensure_ascii=False
        ).encode()
    ).hexdigest()


REFILL_COUNTERS = ("attempted", "approved", "cycle_batches", "cycle_limit", "refill_at")


@dataclass
class State:
    drafts: list[Draft] = field(default_factory=list)
    deferred: dict[str, dict[str, Any]] = field(default_factory=dict)
    day: int = -1
    batches: int = 0
    last_error: str = ""
    retry_after: float = 0
    attempted: int = 0
    approved: int = 0
    cycle_batches: int = 0
    cycle_limit: int = 0
    refill_at: float = 0
    model_calls: list[dict[str, Any]] = field(default_factory=list)
    active_request: dict[str, Any] | None = None

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1:
            raise ValueError("Unsupported Chinese Frontier state version")
        return cls(
            [Draft.from_dict(item) for item in data.get("drafts", [])]
            if data.get("generation_policy") == GENERATION_POLICY
            else [],
            data.get("deferred", {}),
            int(data.get("day", -1)),
            int(data.get("batches", 0)),
            str(data.get("last_error", "")),
            float(data.get("retry_after", 0)),
            **{
                name: int(data.get(name, 0))
                for name in REFILL_COUNTERS
                if name != "refill_at"
            },
            refill_at=float(data.get("refill_at", 0)),
            model_calls=data.get("model_calls", [])[-30:],
            active_request=data.get("active_request"),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "generation_policy": GENERATION_POLICY,
                    "drafts": [draft.as_dict() for draft in self.drafts],
                    "deferred": self.deferred,
                    "day": self.day,
                    "batches": self.batches,
                    "last_error": self.last_error,
                    "retry_after": self.retry_after,
                    **{name: getattr(self, name) for name in REFILL_COUNTERS},
                    "model_calls": self.model_calls,
                    "active_request": self.active_request,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def reset_day(self, day: int) -> None:
        refill.reset_day(vars(self), day)

    def can_refill(
        self, slots: int, repairs: int, config: dict[str, Any], now: float
    ) -> bool:
        return refill.refill_ready(vars(self), self.day, slots, repairs, config, now)

    def reserve_batch(self) -> None:
        refill.reserve_batch(vars(self))

    def record_batch(
        self, attempted: int, approved: int, config: dict[str, Any], now: float
    ) -> None:
        refill.record_batch(vars(self), attempted, approved, config, now)

    def pause_refill(self, config: dict[str, Any], now: float) -> None:
        refill.pause_cycle(vars(self), config, now)

    def can_try(self, key: str, vocabulary: str) -> bool:
        item = self.deferred.get(key)
        return (
            not item
            or item["vocabulary"] != vocabulary
            or time.time() >= item["retry_after"]
        )

    def defer(self, key: str, reason: str, vocabulary: str) -> None:
        previous = self.deferred.get(key, {})
        attempts = (
            int(previous.get("attempts", 0)) + 1
            if previous.get("vocabulary") == vocabulary
            else 1
        )
        self.deferred[key] = {
            "reason": pack_lesson(reason),
            "vocabulary": vocabulary,
            "attempts": attempts,
            "retry_after": time.time() + min(7 * 86400, 1800 * 2 ** min(attempts, 9)),
        }
