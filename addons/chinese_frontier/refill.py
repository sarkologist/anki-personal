# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import math
from typing import Any


def reset_day(state: dict[str, Any], day: int) -> None:
    if state.get("day") != day:
        state.update(
            day=day,
            batches=0,
            attempted=0,
            approved=0,
            cycle_batches=0,
            cycle_limit=0,
            refill_at=0,
        )


def pause_cycle(state: dict[str, Any], config: dict[str, Any], now: float) -> None:
    state["refill_at"] = now + max(30, int(config.get("refill_retry_seconds", 1800)))


def refill_ready(
    state: dict[str, Any],
    day: int,
    slots: int,
    repairs: int,
    config: dict[str, Any],
    now: float,
) -> bool:
    """Bound subscription work per cycle; retry deficits without a daily cutoff."""
    reset_day(state, day)
    needed = max(slots, repairs)
    if needed <= 0:
        return False
    if not config.get("auto_fill", True):
        return state.get("batches", 0) < max(
            1, int(config.get("max_batches_per_day", 32))
        )
    if state.get("refill_at", 0) > now:
        return False
    if state.get("refill_at", 0):
        state.update(cycle_batches=0, cycle_limit=0, refill_at=0)
    ceiling = max(1, min(128, int(config.get("max_batches_per_cycle", 32))))
    if not state.get("cycle_limit"):
        size = min(needed, max(1, min(8, int(config.get("batch_size", 4)))))
        # A small prior avoids assuming perfect approval after a lucky first batch.
        rate = max(
            0.05,
            min(1.0, (state.get("approved", 0) + 1) / (state.get("attempted", 0) + 5)),
        )
        state["cycle_limit"] = min(ceiling, 2 * math.ceil(needed / (size * rate)))
    if state.get("cycle_batches", 0) >= min(ceiling, state["cycle_limit"]):
        pause_cycle(state, config, now)
        return False
    return True


def reserve_batch(state: dict[str, Any]) -> None:
    state["batches"] = state.get("batches", 0) + 1
    state["cycle_batches"] = state.get("cycle_batches", 0) + 1


def record_batch(
    state: dict[str, Any],
    attempted: int,
    approved: int,
    config: dict[str, Any],
    now: float,
    *,
    spent_model_calls: bool = False,
) -> None:
    if not attempted:
        # A ranking call spends budget without pretending a target was attempted.
        if not spent_model_calls:
            state["batches"] = max(0, state.get("batches", 0) - 1)
            state["cycle_batches"] = max(0, state.get("cycle_batches", 0) - 1)
        pause_cycle(state, config, now)
        return
    state["attempted"] = state.get("attempted", 0) + attempted
    state["approved"] = state.get("approved", 0) + approved
