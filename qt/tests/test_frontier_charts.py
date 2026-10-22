# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import ast
import inspect
import re
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))
from chinese_frontier.charts import (  # noqa: E402
    AUDIT_MESSAGES,
    charts_html,
    reason_label,
    writer_theme,
)
from chinese_frontier.telemetry import observe  # noqa: E402


def decision(store, day, stage, outcome, reason=""):
    observe(
        store,
        {"type": "decision", "stage": stage, "outcome": outcome, "reason": reason},
        day,
    )


def call(store, day, i, phase, prompt_chars, elapsed, status="ok"):
    observe(
        store,
        {
            "type": "call",
            "call_id": f"c{i}",
            "phase": phase,
            "finished": True,
            "started_at": 1000 + i,
            "prompt_chars": prompt_chars,
            "elapsed_seconds": elapsed,
            "status": status,
        },
        day,
    )


def sample() -> dict:
    store: dict = {}
    observe(store, {"type": "batch", "attempted": 100, "approved": 1}, 8)
    observe(store, {"type": "batch", "attempted": 6, "approved": 2}, 10)
    observe(store, {"type": "batch", "attempted": 3, "approved": 0}, 10)
    observe(
        store,
        {"type": "batch", "attempted": 1, "approved": 0, "outcome": "error"},
        10,
    )
    for _ in range(3):
        decision(store, 10, "writer", "empty", "No established article le/la.")
    decision(store, 10, "writer", "empty", "Needs a word for a knife.")
    decision(store, 10, "writer", "empty", "The sense is unclear.")
    decision(store, 10, "validation", "rejected", "Missing context verb analysis: lui")
    decision(store, 10, "validation", "rejected", "Missing context verb analysis: plus")
    decision(store, 8, "audit", "rejected", "Another word also fits the blank.")
    decision(store, 10, "audit", "rejected", "Approval must cite visible French")
    decision(store, 10, "runtime", "error", "boom")
    call(store, 10, 1, "Writing sentences", 53_000, 30.5)
    call(store, 10, 2, "Checking alternative answers", 9_000, 12.0)
    call(store, 10, 3, "Writing sentences", 120_000, 60.0, status="timeout")
    call(store, 10, 4, "Selecting targets", 4_000, 5.0)
    return store


def test_yield_per_day_labels_relative_days_and_rates():
    html = charts_html(sample(), {"day": 10})
    assert "today · attempted: 10</title>" in html
    assert "today · approved: 2</title>" in html
    assert "-2 · attempted: 100</title>" in html
    assert ">20%</text>" in html and ">1%</text>" in html
    assert "Cumulative approval yield: 3/110 (3%)" in html


def test_daily_charts_share_days_even_without_attempts():
    store = sample()
    decision(store, 9, "writer", "empty", "Needs a word.")
    yields, losses = charts_html(store, {"day": 10}).split("<section>")[1:3]
    assert ">-1</text>" in yields and ">-1</text>" in losses


def test_losses_per_day_stack_rejection_kinds_with_legend():
    html = charts_html(sample(), {"day": 10})
    assert "today · empty writer proposals: 5</title>" in html
    assert "today · validation rejections: 2</title>" in html
    assert "-2 · audit rejections: 1</title>" in html
    assert "today · runtime failures: 1</title>" in html
    assert 'class="legend"' in html


def test_recent_batches_mark_errors():
    html = charts_html(sample(), {"day": 10})
    assert html.count('class="mark"') >= 2  # error batch and timeout call
    assert "batch 4 · error" in html


def test_writer_prompt_size_reports_latest_and_max_in_start_order():
    html = charts_html(sample(), {"day": 10})
    assert "Latest 120k chars · max 120k chars · 2 writer calls" in html
    assert "call 1 · input characters: 53,000</title>" in html
    assert "call 4 · Selecting targets: 5.0s</title>" in html
    assert "call 3 · timeout" in html


def test_rejection_reasons_group_suffixes_and_substantive_audits():
    assert reason_label("validation", "Missing context verb analysis: lui") == (
        "Missing context verb analysis"
    )
    assert reason_label("audit", "Another word also fits.") == (
        "Audit: substantive rejection"
    )
    assert reason_label("audit", AUDIT_MESSAGES[0]) == AUDIT_MESSAGES[0]
    assert len(reason_label("validation", "x" * 200)) == 60
    html = charts_html(sample(), {"day": 10})
    assert "Missing context verb analysis: 2</title>" in html


def test_writer_themes_are_keyword_buckets():
    assert writer_theme("No established article le/la.") == "articles/pronouns missing"
    assert writer_theme("Needs a word for a knife.") == "vocabulary missing"
    assert writer_theme("The sense is unclear.") == "sense/other"
    html = charts_html(sample(), {"day": 10})
    assert "articles/pronouns missing: 3</title>" in html


def test_audit_messages_cover_every_fixed_validate_review_error():
    from chinese_frontier.french.generation import validate_review

    source = textwrap.dedent(inspect.getsource(validate_review))
    raised = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "ValidationError"
    }
    assert raised and raised <= set(AUDIT_MESSAGES)


def test_empty_store_renders_no_data_for_every_chart():
    html = charts_html({}, {})
    assert html.count("no data yet") == html.count("<section>") == 7
    assert "<script" not in html.lower() and "http" not in html


def test_untrusted_reasons_are_escaped():
    store: dict = {}
    decision(store, 10, "validation", "rejected", "<script>alert(1)</script>")
    decision(store, 10, "writer", "empty", "<img src=x onerror=alert(1)>")
    html = charts_html(store, {"day": 10})
    assert "<script" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_dark_mode_changes_only_colours():
    light = charts_html(sample(), {"day": 10})
    dark = charts_html(sample(), {"day": 10}, dark=True)
    assert light != dark
    strip = lambda s: re.sub(r"#[0-9a-f]{6}", "", s)  # noqa: E731
    assert strip(light) == strip(dark)
