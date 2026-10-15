# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))
from chinese_frontier.preparation import (  # noqa: E402
    context_bundle,
    pack_lesson,
    preparation_needed,
    read_lesson,
)


def test_bundle_retrieves_relevant_proven_usage_without_promoting_visible_words():
    records = {
        "le": ["le train"],
        "train": ["Take the train to the station."],
        "photo": ["Photo equipment is expensive."],
        "chat": ["The cat sleeps."],
    }
    selected = context_bundle(
        records, "railway station train", ["le", "chat", "photo"], 2
    )
    assert selected == ["le", "train"]
    assert "station" not in selected


def test_lesson_preserves_actionable_rejections_as_valid_bounded_json():
    report = {
        "reason": "Use a payment situation.",
        "alternatives": [
            {
                "answer": "date limite",
                "fits_context": True,
                "fits_cloze": True,
                "reason": "Also a deadline.",
            }
        ],
        "context_checks": [
            {
                "example": 0,
                "sufficient": False,
                "clues": [],
                "reason": "No payment context.",
            }
        ],
        "irrelevant": "x" * 50000,
    }
    saved = pack_lesson(report)
    assert len(saved) < 6000
    assert json.loads(saved) == read_lesson(saved)
    assert read_lesson(saved)["alternatives"][0]["answer"] == "date limite"
    assert read_lesson(saved)["context_checks"][0]["sufficient"] is False
    assert read_lesson("legacy truncated {")["reason"] == "legacy truncated {"


def test_buffer_prepares_ahead_without_changing_native_admission_capacity():
    config = {"buffer_notes": 4}
    assert preparation_needed(0, 0, config) == 4
    assert preparation_needed(0, 4, config) == 0
    assert preparation_needed(2, 1, config) == 5
    assert preparation_needed(0, 0, {"buffer_notes": 0}) == 0
