# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Local diagnostics only. No telemetry affects linguistic approval or admission."""

from __future__ import annotations

import json
import sysconfig
import time
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .providers import GenerationStopped

Observer = Callable[[dict[str, Any]], None]


def request_model(
    client: Any,
    prompt: str,
    schema: dict[str, Any],
    config: dict[str, Any],
    phase: str,
    attempt: int,
    targets: list[dict[str, Any]],
    progress: Observer | None,
) -> Any:
    provider = str(config.get("provider", "codex"))
    event = {
        "type": "call",
        "call_id": uuid.uuid4().hex,
        "phase": phase,
        "attempt": attempt,
        "started_at": time.time(),
        "prompt_chars": len(prompt),
        "schema_chars": len(json.dumps(schema, ensure_ascii=False)),
        "target_count": len(targets),
        "targets": targets[:32],
        "provider": provider,
        "model": config.get(f"{provider}_model") or "CLI default",
        "finished": False,
    }
    if progress:
        progress(event.copy())
    started = time.monotonic()
    try:
        response = client.request(prompt, schema)
        event.update(
            status="ok", response_chars=len(json.dumps(response, ensure_ascii=False))
        )
        return response
    except Exception as exc:
        status = (
            "cancelled"
            if isinstance(exc, GenerationStopped)
            else "timeout"
            if isinstance(exc, TimeoutError)
            else "error"
        )
        event.update(status=status, error=str(exc)[:1200] or type(exc).__name__)
        raise
    finally:
        event.update(
            finished=True,
            finished_at=time.time(),
            elapsed_seconds=round(time.monotonic() - started, 2),
        )
        if progress:
            progress(event.copy())


def error_location(exc: BaseException) -> str:
    """Innermost frames of an unexpected error; str(exc) alone hides the source."""
    frames = traceback.extract_tb(exc.__traceback__)
    library = Path(sysconfig.get_paths()["stdlib"]).resolve()
    # Library frames alone (e.g. urllib internals) hide which call failed.
    own = [f for f in frames[:-1] if library not in Path(f.filename).resolve().parents]
    chosen = own[-3:] + frames[-1:]
    return " <- ".join(
        f"{Path(f.filename).name}:{f.lineno} {f.name}" for f in reversed(chosen)
    )


def decision(
    observer: Observer | None,
    stage: str,
    outcome: str,
    target: str,
    key: str,
    mode: str = "",
    reason: str = "",
) -> None:
    if observer:
        observer(
            {
                "type": "decision",
                "stage": stage,
                "outcome": outcome,
                "target": target,
                "key": key,
                "mode": mode,
                "reason": reason,
            }
        )


def observe(
    store: dict[str, Any], event: dict[str, Any], day: int, now: float | None = None
) -> None:
    now = time.time() if now is None else now
    store.setdefault("version", 1)
    store.setdefault("since", now)
    store.setdefault("totals", {})
    store.setdefault("days", {})
    store.setdefault("events", [])
    store.setdefault("calls", [])
    row = dict(event)
    for name in ("reason", "error", "where"):
        if name in row:
            row[name] = str(row[name])[:1200]
    row.setdefault("at", row.get("finished_at", now))
    row["day"] = day
    counts: dict[str, float | int] = {}
    kind = row.get("type")
    if kind == "call":
        if not row.get("finished"):
            store["active_call"] = row
            return
        if any(c.get("call_id") == row.get("call_id") for c in store["calls"]):
            return
        if (store.get("active_call") or {}).get("call_id") == row.get("call_id"):
            store["active_call"] = None
        store["calls"] = (store["calls"] + [row])[-100:]
        counts = {
            "calls": 1,
            "seconds": row.get("elapsed_seconds") or 0,
            "prompt_chars": row.get("prompt_chars", 0),
            "response_chars": row.get("response_chars", 0),
        }
        if row.get("status") != "ok":
            counts[str(row.get("status", "error"))] = 1
    elif kind == "batch":
        counts = {
            "batches": 1,
            "attempted": row.get("attempted", 0),
            "approved": row.get("approved", 0),
        }
    elif kind == "admission":
        counts = {k: row.get(k, 0) for k in ("notes", "cards", "updates")}
    elif kind == "decision":
        counts = {str(row.get("stage")) + "_" + str(row.get("outcome")): 1}
    else:
        return
    for counter in (store["totals"], store["days"].setdefault(str(day), {})):
        for key, value in counts.items():
            counter[key] = counter.get(key, 0) + value
    store["days"] = dict(sorted(store["days"].items(), key=lambda p: int(p[0]))[-14:])
    store["events"] = (store["events"] + [row])[-300:]


def recover(store: dict[str, Any], day: int) -> None:
    if active := store.get("active_call"):
        observe(
            store,
            {
                **active,
                "finished": True,
                "status": "interrupted",
                "elapsed_seconds": None,
                "error": "Profile closed or process restarted before completion was recorded.",
            },
            int(active.get("day", day)),
        )


def import_calls(store: dict[str, Any], calls: list[dict[str, Any]], day: int) -> None:
    if store.get("legacy_imported"):
        return
    for i, call in enumerate(calls):
        if call.get("finished"):
            observe(
                store,
                {
                    **call,
                    "type": "call",
                    "call_id": f"legacy-{i}",
                    "imported": True,
                    "status": "error" if call.get("error") else "ok",
                },
                day,
            )
    store["legacy_imported"] = True


def clock(value: float | None) -> str:
    return (
        datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        if value
        else "unknown"
    )


def overview(
    store: dict[str, Any],
    state: dict[str, Any],
    status: str,
    busy: bool,
    prepared: int,
    buffer: int,
    now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    lines = [status]
    active = store.get("active_call")
    if busy and active:
        lines += [
            f"Active: {active.get('phase', 'request')} · attempt {active.get('attempt', '?')} · {max(0, int(now - active.get('started_at', now)))}s elapsed",
            f"Provider: {active.get('provider', 'unknown')} · model: {active.get('model', 'unknown')}",
        ]
    lines += [f"Approved buffer: {prepared} / {buffer} notes (repairs excluded)"]
    snapshot = store.get("snapshot")
    if snapshot:
        lines += [
            f"Available new-card slots: {snapshot['slots']} · last checked {clock(snapshot.get('at'))}",
            f"Established context units: {snapshot['known']}",
        ]
        for budget in snapshot.get("budgets", []):
            lines += [
                f"Deck {budget['deck_id']}: new limit {budget['new_limit']}, introduced {budget['introduced']}, pending {budget['pending']}; reviews due {budget['due_reviews']}, completed {budget['reviewed']}, limit {budget['review_limit']}"
            ]
        lines += [
            "Pending counts include buried cards; parent and review limits also reserve capacity."
        ]
    else:
        lines += ["No capacity snapshot yet; Check now refreshes it."]
    retry = max(state.get("retry_after", 0), state.get("refill_at", 0))
    lines += [
        f"Next retry allowed: {clock(retry)} ({max(0, int(retry - now))}s remaining)"
        if retry > now
        else "Retry allowed now; periodic checks still apply.",
        f"Cycle batches: {state.get('cycle_batches', 0)} / {state.get('cycle_limit', 0)}",
        "",
        f"Telemetry since {clock(store.get('since'))}",
    ]
    totals = store.get("totals", {})
    attempted, approved = totals.get("attempted", 0), totals.get("approved", 0)
    yield_text = (
        f"{approved}/{attempted} ({approved / attempted:.0%})"
        if attempted
        else "no completed target attempts"
    )
    calls = totals.get("calls", 0)
    seconds = totals.get("seconds", 0)
    lines += [
        f"Target approval yield: {yield_text}",
        f"Admitted: {totals.get('notes', 0)} notes, {totals.get('cards', 0)} new cards · updated existing notes: {totals.get('updates', 0)}",
        f"Tracked calls: {calls} · total measured time: {seconds:.1f}s",
        f"Input: {totals.get('prompt_chars', 0):,} prompt characters · output: {totals.get('response_chars', 0):,} JSON characters",
        f"Timeouts: {totals.get('timeout', 0)} · errors: {totals.get('error', 0)} · cancelled: {totals.get('cancelled', 0)} · interrupted: {totals.get('interrupted', 0)}",
        f"Runtime failures: {totals.get('runtime_error', 0)}",
        f"Rejected: validation {totals.get('validation_rejected', 0)}, language audit {totals.get('audit_rejected', 0)}, empty proposals {totals.get('writer_empty', 0)}, admission {totals.get('admission_rejected', 0)}",
    ]
    today = store.get("days", {}).get(str(state.get("day")), {})
    lines += [
        f"This Anki day: {today.get('calls', 0)} tracked calls, {today.get('approved', 0)}/{today.get('attempted', 0)} target attempts approved",
        "",
        "Call OK means JSON returned, not card approval. Characters measure text size, not tokens, money or subscription quota. Schema characters are recorded separately per call. Time is summed request latency; concurrent languages may overlap. Target attempts include repairs and repeat attempts; a multi-character note can create several cards. Approval yield measures gate acceptance, not learning outcomes. Recent calls imported at upgrade are partial history; batch outcomes start with telemetry. The last 100 calls, 300 events and 14 Anki-day summaries are retained; aggregate counters persist. Diagnostics stay local to this profile.",
    ]
    return "\n".join(lines)


def decisions_text(store: dict[str, Any]) -> str:
    rows = [e for e in reversed(store.get("events", [])) if e.get("type") != "call"]
    return (
        "\n\n".join(
            f"{clock(e.get('at'))} · {e.get('stage', e.get('type'))} · {e.get('outcome', '')}\n{e.get('target', '')} {e.get('mode', '')}\n{e.get('reason', '')}"
            + (f"\nat {e['where']}" if e.get("where") else "")
            if e.get("type") == "decision"
            else f"{clock(e.get('at'))} · {e['type']}\n"
            + json.dumps(
                {k: v for k, v in e.items() if k not in ("at", "type", "day")},
                ensure_ascii=False,
            )
            for e in rows
        )
        or "No decisions recorded yet."
    )
