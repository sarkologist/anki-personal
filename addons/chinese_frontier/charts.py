# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Telemetry charts as one static page: inline SVG, no scripts or fetches."""

from __future__ import annotations

import re
from datetime import datetime
from html import escape
from typing import Any, Callable

WIDTH = 640
OTHER = "#8c9bb0"
FAILED = "#d9534f"
# Mid-tone fills read on both light and dark backgrounds.
LOSSES = (
    ("writer_empty", "empty writer proposals", "#d0a24a"),
    ("validation_rejected", "validation rejections", "#5b8fd9"),
    ("audit_rejected", "audit rejections", "#c8648a"),
    ("runtime_error", "runtime failures", FAILED),
)
PHASES = {
    "Writing sentences": "#5b8fd9",
    "Checking alternative answers": "#c8648a",
    "Selecting targets": "#d0a24a",
}
# validate_review's fixed messages (french/generation.py) and the fingerprint
# check beside it. Any other audit rejection reason is the reviewer's prose.
AUDIT_MESSAGES = (
    "Invalid independent review",
    "Incomplete independent review",
    "Independent review must report alternatives",
    "Alternative has an invalid example",
    "Report a concrete cloze substitution, not grammar commentary",
    "Alternative must be a competing answer",
    "Alternative has no clear judgment",
    "Missing alternatives for a contrast example",
    "Missing French-only context checks",
    "Invalid French-only context check",
    "Approval must cite visible French situational clues",
    "Context clues must quote visible French, not the answer or translation",
    "Review belongs to a different proposal",
)
THEMES = (
    (
        "articles/pronouns missing",
        re.compile(r"\b(articles?|determiners?|pronouns?|le/la|elision)\b", re.I),
    ),
    (
        "vocabulary missing",
        re.compile(r"not established|no established|missing|\bneeds?\b", re.I),
    ),
)
EMPTY = '<p class="empty">no data yet</p>'


def reason_label(stage: str, reason: str) -> str:
    text = " ".join(str(reason).split())
    if stage == "audit" and not text.startswith(AUDIT_MESSAGES):
        return "Audit: substantive rejection"
    return text.split(":", 1)[0].strip()[:60] or "(no reason)"


def writer_theme(reason: str) -> str:
    return next((name for name, rx in THEMES if rx.search(reason)), "sense/other")


def number(value: float) -> str:
    return f"{value:,}" if isinstance(value, int) else f"{value:,.1f}"


def kchars(value: float) -> str:
    return f"{round(value / 1000)}k chars"


def legend(items: list[tuple[str, str]]) -> str:
    return (
        '<p class="legend">'
        + "".join(
            f'<span><i style="background:{color}"></i>{escape(name)}</span>'
            for name, color in items
        )
        + "</p>"
    )


def columns(
    labels: list[str],
    series: list[tuple[str, str, list[float]]],
    stacked: bool = False,
    ticks: list[str] | None = None,
    notes: list[str] | None = None,
    marks: list[str] | None = None,
    fmt: Callable[[float], str] = number,
) -> str:
    """Vertical bars per label; each rect's <title> names its exact value."""
    left, top, height = 48, 18, 150
    slot = (WIDTH - left - 8) / len(labels)
    tops = [
        sum(v[i] for _, _, v in series) if stacked else max(v[i] for _, _, v in series)
        for i in range(len(labels))
    ]
    peak = max(tops) or 1
    base = top + height
    step = max(1, -(-len(labels) * 34 // (WIDTH - left)))
    parts = [
        f'<line x1="{left}" y1="{top}" x2="{WIDTH}" y2="{top}" class="grid"/>',
        f'<line x1="{left}" y1="{base}" x2="{WIDTH}" y2="{base}" class="axis"/>',
        f'<text x="{left - 4}" y="{top + 4}" text-anchor="end">{escape(fmt(peak))}</text>',
        f'<text x="{left - 4}" y="{base}" text-anchor="end">0</text>',
    ]
    width = slot * 0.7 / (1 if stacked else len(series))
    for i, label in enumerate(labels):
        x0 = left + i * slot
        y: float = base
        for j, (name, color, values) in enumerate(series):
            if not values[i]:
                continue
            h = values[i] / peak * height
            x = x0 + slot * 0.15 + (0 if stacked else j * width)
            top_y = (y if stacked else base) - h
            parts.append(
                f'<rect x="{x:.1f}" y="{top_y:.1f}" width="{width:.1f}" height="{h:.1f}" fill="{color}">'
                f"<title>{escape(label)} · {escape(name)}: {escape(fmt(values[i]))}</title></rect>"
            )
            if stacked:
                y -= h
        middle = x0 + slot / 2
        if notes and notes[i]:
            y_note = base - tops[i] / peak * height - 4
            parts.append(
                f'<text x="{middle:.1f}" y="{y_note:.1f}" text-anchor="middle">{escape(notes[i])}</text>'
            )
        if marks and marks[i]:
            parts.append(
                f'<text x="{middle:.1f}" y="{base + 11}" text-anchor="middle" class="mark" fill="{FAILED}">×'
                f"<title>{escape(label)} · {escape(marks[i])}</title></text>"
            )
        if i % step == 0:
            tick = ticks[i] if ticks else label
            parts.append(
                f'<text x="{middle:.1f}" y="{base + 24}" text-anchor="middle">{escape(tick)}</text>'
            )
    return svg(base + 30, parts)


def hbars(rows: list[tuple[str, int]], color: str) -> str:
    label, row = 380, 22
    peak = max(count for _, count in rows) or 1
    parts = []
    for i, (name, count) in enumerate(rows):
        y = i * row + 2
        w = count / peak * (WIDTH - label - 40)
        parts += [
            f'<text x="0" y="{y + 13}">{escape(name)}</text>',
            f'<rect x="{label}" y="{y + 2}" width="{w:.1f}" height="14" fill="{color}">'
            f"<title>{escape(name)}: {count}</title></rect>",
            f'<text x="{label + w + 4:.1f}" y="{y + 13}">{count}</text>',
        ]
    return svg(len(rows) * row + 4, parts)


def svg(height: float, parts: list[str]) -> str:
    return (
        f'<svg viewBox="0 0 {WIDTH} {height}" width="{WIDTH}" height="{height}" role="img">'
        + "".join(parts)
        + "</svg>"
    )


def section(title: str, caption: str, body: str, headline: str = "") -> str:
    head = f'<p class="head">{escape(headline)}</p>' if headline else ""
    return f'<section><h3>{escape(title)}</h3><p class="caption">{escape(caption)}</p>{head}{body}</section>'


def hhmm(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%H:%M") if value else "?"


def yield_chart(store: dict[str, Any], state: dict[str, Any]) -> str:
    caption = "Target attempts and gate approvals per Anki day (last 14 kept). Attempts include repairs and repeat attempts; yield measures gate acceptance, not learning outcomes."
    days = sorted((int(d), v) for d, v in store.get("days", {}).items())
    # Keep days without attempts so both daily charts share one axis.
    if not any(v.get("attempted") or v.get("approved") for _, v in days):
        return section("Yield per Anki day", caption, EMPTY)
    today = int(state.get("day") or days[-1][0])
    labels = [str(d - today) if d != today else "today" for d, _ in days]
    attempted = [v.get("attempted", 0) for _, v in days]
    approved = [v.get("approved", 0) for _, v in days]
    totals = store.get("totals", {})
    total_attempted = totals.get("attempted", 0)
    total_approved = totals.get("approved", 0)
    headline = (
        f"Cumulative approval yield: {total_approved}/{total_attempted} ({total_approved / total_attempted:.0%})"
        if total_attempted
        else ""
    )
    return section(
        "Yield per Anki day",
        caption,
        legend([("attempted", OTHER), ("approved", "#2f9e6e")])
        + columns(
            labels,
            [("attempted", OTHER, attempted), ("approved", "#2f9e6e", approved)],
            notes=[f"{p / a:.0%}" if a else "–" for a, p in zip(attempted, approved)],
        ),
        headline,
    )


def losses_chart(store: dict[str, Any], state: dict[str, Any]) -> str:
    caption = "Rejection decisions per Anki day. A target can be rejected on each retry, so counts can exceed attempts. Runtime failures are errors, not quality rejections."
    days = sorted((int(d), v) for d, v in store.get("days", {}).items())
    kinds = [k for k in LOSSES if any(v.get(k[0]) for _, v in days)]
    if not kinds:
        return section("Where targets are lost", caption, EMPTY)
    today = int(state.get("day") or days[-1][0])
    labels = [str(d - today) if d != today else "today" for d, _ in days]
    return section(
        "Where targets are lost",
        caption,
        legend([(name, color) for _, name, color in kinds])
        + columns(
            labels,
            [(name, color, [v.get(k, 0) for _, v in days]) for k, name, color in kinds],
            stacked=True,
        ),
    )


def batches_chart(store: dict[str, Any]) -> str:
    caption = "Target attempts and approvals per batch, oldest first (last 40 retained). × marks a batch that ended in an error."
    rows = [e for e in store.get("events", []) if e.get("type") == "batch"][-40:]
    if not rows:
        return section("Recent batches", caption, EMPTY)
    return section(
        "Recent batches",
        caption,
        legend([("attempted", OTHER), ("approved", "#2f9e6e")])
        + columns(
            [f"batch {i + 1}" for i in range(len(rows))],
            [
                ("attempted", OTHER, [e.get("attempted", 0) for e in rows]),
                ("approved", "#2f9e6e", [e.get("approved", 0) for e in rows]),
            ],
            ticks=[hhmm(e.get("at")) for e in rows],
            marks=["error" if e.get("outcome") == "error" else "" for e in rows],
        ),
    )


def started(store: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(store.get("calls", []), key=lambda c: c.get("started_at") or 0)


def prompt_chart(store: dict[str, Any]) -> str:
    caption = "Input characters per writer call, in start order (last 100 calls retained). Characters measure text size, not tokens, money or subscription quota."
    rows = [c for c in started(store) if c.get("phase") == "Writing sentences"]
    if not rows:
        return section("Writer prompt size", caption, EMPTY)
    sizes = [c.get("prompt_chars", 0) for c in rows]
    return section(
        "Writer prompt size",
        caption,
        columns(
            [f"call {i + 1}" for i in range(len(rows))],
            [("input characters", PHASES["Writing sentences"], sizes)],
            ticks=[hhmm(c.get("started_at")) for c in rows],
        ),
        f"Latest {kchars(sizes[-1])} · max {kchars(max(sizes))} · {len(rows)} writer calls",
    )


def latency_chart(store: dict[str, Any]) -> str:
    caption = "Request latency per call by phase, in start order. × marks a call that returned no JSON; interrupted calls have unknown duration. Call OK means JSON returned, not card approval."
    rows = started(store)
    if not rows:
        return section("Call latency", caption, EMPTY)
    phases = list(PHASES) + sorted(
        {str(c.get("phase") or "unknown") for c in rows} - set(PHASES)
    )
    phases = [
        p for p in phases if any(str(c.get("phase") or "unknown") == p for c in rows)
    ]
    colors = [PHASES.get(p, OTHER) for p in phases]
    return section(
        "Call latency",
        caption,
        legend(list(zip(phases, colors)))
        + columns(
            [f"call {i + 1}" for i in range(len(rows))],
            [
                (
                    p,
                    color,
                    [
                        float(c.get("elapsed_seconds") or 0)
                        if str(c.get("phase") or "unknown") == p
                        else 0
                        for c in rows
                    ],
                )
                for p, color in zip(phases, colors)
            ],
            stacked=True,
            ticks=[hhmm(c.get("started_at")) for c in rows],
            marks=[
                "" if c.get("status") == "ok" else str(c.get("status")) for c in rows
            ],
            fmt=lambda v: f"{v:.1f}s",
        ),
    )


def counted(labels: list[str], limit: int = 8) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda p: (-p[1], p[0]))[:limit]


def reasons_chart(store: dict[str, Any]) -> str:
    caption = "Most frequent validation and audit rejection reasons among the last 300 events. Messages are grouped up to the first colon; reviewer prose is one substantive bucket."
    rows = counted(
        [
            reason_label(e["stage"], e.get("reason", ""))
            for e in store.get("events", [])
            if e.get("type") == "decision"
            and e.get("stage") in ("validation", "audit")
            and e.get("outcome") == "rejected"
        ]
    )
    body = hbars(rows, "#5b8fd9") if rows else EMPTY
    return section("Top rejection reasons", caption, body)


def themes_chart(store: dict[str, Any]) -> str:
    caption = "Why the writer returned no proposal, among the last 300 events. Buckets come from a keyword heuristic over the writer's stated reasons and are approximate."
    rows = counted(
        [
            writer_theme(str(e.get("reason", "")))
            for e in store.get("events", [])
            if e.get("type") == "decision"
            and e.get("stage") == "writer"
            and e.get("outcome") == "empty"
        ]
    )
    body = hbars(rows, "#d0a24a") if rows else EMPTY
    return section("Writer refusal themes", caption, body)


def charts_html(
    store: dict[str, Any], state: dict[str, Any], dark: bool = False
) -> str:
    """Deterministic for unchanged input, so callers can skip identical reloads."""
    fg, bg = ("#dddddd", "#2c2c2c") if dark else ("#222222", "#ffffff")
    style = f"""
body {{ font-family: -apple-system, "Segoe UI", sans-serif; font-size: 13px; margin: 12px; color: {fg}; background: {bg}; }}
h3 {{ margin: 20px 0 2px; font-size: 14px; }}
p {{ margin: 2px 0 6px; }}
.caption {{ opacity: 0.7; font-size: 12px; }}
.head {{ font-weight: 600; }}
.legend span {{ margin-right: 14px; font-size: 12px; }}
.legend i {{ display: inline-block; width: 10px; height: 10px; margin-right: 4px; }}
svg text {{ fill: {fg}; font-size: 10px; }}
svg text.mark {{ fill: {FAILED}; font-size: 12px; }}
.grid {{ stroke: {OTHER}; stroke-dasharray: 3 3; stroke-opacity: 0.5; }}
.axis {{ stroke: {OTHER}; }}
"""
    body = "".join(
        [
            yield_chart(store, state),
            losses_chart(store, state),
            batches_chart(store),
            prompt_chart(store),
            latency_chart(store),
            reasons_chart(store),
            themes_chart(store),
        ]
    )
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{style}</style></head><body>{body}</body></html>'
