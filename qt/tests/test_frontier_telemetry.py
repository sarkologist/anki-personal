# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))
from chinese_frontier.providers import GenerationStopped  # noqa: E402
from chinese_frontier.telemetry import (  # noqa: E402
    import_calls,
    observe,
    overview,
    recover,
    request_model,
)


class Client:
    def request(self, prompt, schema):
        return {"proposals": []}


def test_call_telemetry_measures_payload_and_deduplicates_completion():
    events = []
    result = request_model(
        Client(),
        "bonjour",
        {},
        {"provider": "claude"},
        "Writing sentences",
        1,
        [{"target": "train", "key": "1"}],
        events.append,
    )
    assert result == {"proposals": []}
    assert not events[0]["finished"] and events[1]["finished"]
    assert events[1]["prompt_chars"] == 7
    assert events[1]["response_chars"] > 0
    assert events[1]["provider"] == "claude"
    store = {}
    for e in [*events, events[-1]]:
        observe(store, e, 10)
    assert store["totals"]["calls"] == 1
    assert len(store["calls"]) == 1 and store["active_call"] is None


@pytest.mark.parametrize(
    "error,status",
    [
        (TimeoutError("timeout"), "timeout"),
        (GenerationStopped(), "cancelled"),
        (ValueError("malformed JSON"), "error"),
    ],
)
def test_failed_calls_are_distinct_from_quality_rejections(error, status):
    class Failing:
        def request(self, *args):
            raise error

    events = []
    with pytest.raises(type(error)):
        request_model(
            Failing(),
            "text",
            {},
            {},
            "Checking alternative answers",
            2,
            [],
            events.append,
        )
    assert events[-1]["status"] == status
    store = {}
    observe(store, events[-1], 10)
    assert store["totals"][status] == 1
    assert not store["totals"].get("audit_rejected")


def test_telemetry_preserves_note_card_and_repair_counts_independently():
    store = {}
    observe(store, {"type": "batch", "attempted": 4, "approved": 1}, 10)
    observe(store, {"type": "admission", "notes": 1, "cards": 2, "updates": 1}, 10)
    observe(
        store,
        {
            "type": "decision",
            "stage": "audit",
            "outcome": "rejected",
            "reason": "Another word also fits.",
        },
        10,
    )
    assert store["totals"]["approved"] == 1
    assert store["totals"]["cards"] == 2
    assert store["totals"]["updates"] == 1
    assert store["totals"]["audit_rejected"] == 1
    assert store["days"]["10"]["attempted"] == 4
    assert store["events"][-1]["reason"] == "Another word also fits."


def test_legacy_call_import_is_once_only_and_does_not_invent_batch_history():
    store = {}
    calls = [
        {
            "finished": True,
            "started_at": 1,
            "elapsed_seconds": 2,
            "prompt_chars": 200,
            "phase": "Writing sentences",
        }
    ]
    import_calls(store, calls, 10)
    import_calls(store, calls, 10)
    assert store["totals"]["calls"] == 1
    assert not store["totals"].get("attempted")
    assert store["calls"][0]["imported"]


def test_restart_marks_inflight_call_interrupted_without_fabricating_duration():
    store = {}
    observe(
        store, {"type": "call", "call_id": "x", "started_at": 1, "finished": False}, 10
    )
    recover(store, 10)
    assert store["calls"][-1]["status"] == "interrupted"
    assert store["calls"][-1]["elapsed_seconds"] is None
    assert store["totals"]["seconds"] == 0
    assert not store["active_call"]


def test_telemetry_is_bounded_and_survives_profile_state_restart(tmp_path):
    from chinese_frontier.state import State

    state = State(day=10)
    for i in range(400):
        observe(
            state.telemetry,
            {
                "type": "decision",
                "stage": "writer",
                "outcome": "empty",
                "reason": "x" * 3000,
            },
            i,
        )
    assert len(state.telemetry["events"]) <= 300
    assert len(state.telemetry["days"]) <= 14
    assert len(state.telemetry["events"][-1]["reason"]) <= 1200
    state.save(tmp_path / "state.json")
    restored = State.load(tmp_path / "state.json")
    assert restored.telemetry == state.telemetry


def test_overview_explains_units_waiting_and_observation_window():
    store = {}
    observe(store, {"type": "batch", "attempted": 4, "approved": 1}, 10, now=100)
    text = overview(
        store, {"day": 10, "refill_at": 300}, "Waiting", False, 2, 4, now=200
    )
    assert "25%" in text and "2 / 4" in text
    assert "100s" in text or "1m 40s" in text
    assert "characters" in text and "tokens" in text
    assert "No capacity snapshot" in text


def test_ranking_without_targets_spends_budget_without_fabricating_attempts():
    from chinese_frontier.refill import record_batch

    state = {"batches": 1, "cycle_batches": 1}
    record_batch(state, 0, 0, {}, 100, spent_model_calls=True)
    assert state["batches"] == state["cycle_batches"] == 1
    assert state.get("attempted", 0) == 0 and state["refill_at"] > 100


def test_diagnostics_widget_refreshes_real_calls_and_preserves_plain_text(
    monkeypatch, tmp_path
):
    from chinese_frontier.diagnostics import Diagnostics

    from aqt.qt import QApplication, QDialog

    app = QApplication.instance() or QApplication([])
    store = {}
    data = {
        "store": store,
        "state": {},
        "status": "Idle",
        "busy": False,
        "prepared": 2,
        "buffer": 4,
    }
    parent = QDialog()
    widget = Diagnostics(parent, lambda: data)
    assert "2 / 4" in widget.summary.toPlainText()
    observe(
        store,
        {
            "type": "call",
            "call_id": "x",
            "finished": True,
            "status": "timeout",
            "error": "<b>timeout</b>",
            "elapsed_seconds": 3,
            "target_count": 1,
            "prompt_chars": 200,
        },
        10,
    )
    observe(
        store,
        {
            "type": "decision",
            "stage": "audit",
            "outcome": "rejected",
            "target": "méfiance",
            "reason": "<b>Synonym also fits.</b>",
        },
        10,
    )
    widget.refresh()
    assert widget.choices.count() == 1
    assert "timeout" in widget.calls.toPlainText()
    assert "timeout" in widget.details.toPlainText()
    assert "<b>Synonym also fits.</b>" in widget.decisions.toPlainText()
    assert app is not None
    import json

    from aqt.qt import QFileDialog

    output = tmp_path / "diagnostics.json"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", lambda *args: (str(output), "JSON (*.json)")
    )
    widget.export()
    assert json.loads(output.read_text())["store"]["calls"][0]["status"] == "timeout"
    parent.close()


def test_error_location_names_the_add_on_caller_not_only_library_frames():
    import urllib.request

    from chinese_frontier.telemetry import error_location

    def fetch_senses():
        return urllib.request.urlopen("unknown://dictionary")

    try:
        fetch_senses()
    except OSError as exc:
        where = error_location(exc)
    assert where.startswith("request.py")
    assert "fetch_senses" in where
