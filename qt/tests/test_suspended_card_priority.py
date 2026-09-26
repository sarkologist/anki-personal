from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "addons"))

from suspended_card_priority.core import (  # noqa: E402
    CardText,
    ReferenceIndex,
    priority_score,
    sort_ids,
    validate_batch,
)


def card(cid, question, answer, nid=None):
    return CardText.from_html(cid, nid or cid, question, answer)


def test_clozes_are_scored_by_their_actual_target():
    a = card(1, "A [...], B two", 'A <span class="cloze">one</span>, B two')
    b = card(2, "A one, B [...]", 'A one, B <span class="cloze">two</span>')
    assert a.target == "one"
    assert b.target == "two"
    assert a.fingerprint != b.fingerprint


def test_html_cleanup_preserves_math_and_marks_images():
    c = card(
        1, '<style>junk</style>Why x &lt; y?<img src="x.png">', "Because \\(x+1=y\\)"
    )
    assert c.question == "Why x < y?"
    assert c.has_media
    assert "\\(x+1=y\\)" in c.answer


def test_retrieval_includes_siblings_without_declaring_them_duplicates():
    references = [
        card(1, "unrelated", "value", 20),
        card(2, "compact space", "finite cover"),
    ]
    index = ReferenceIndex(references)
    matches = index.nearest(card(3, "compactness", "closed", 20), limit=2)
    assert 1 in [c.card_id for c in matches]


def test_conceptual_novelty_wins_and_computation_overlap_loses():
    novel = priority_score(4, 0, 0)
    duplicate = priority_score(4, 0, 4)
    calculation = priority_score(0, 4, 4)
    assert novel > duplicate > calculation
    assert priority_score(3, 1, 0) > priority_score(1, 3, 0)


def verdict(cid=1, **updates):
    value = dict(
        card_id=cid,
        conceptual=4,
        computation=0,
        overlap=0,
        confidence="high",
        reason="Adds a new distinction",
        matches=[],
    )
    value.update(updates)
    return value


def test_invalid_or_hallucinated_results_are_not_saved():
    candidates = [card(1, "Why?", "Because")]
    for value in [
        verdict(cid=2),
        verdict(conceptual=5),
        verdict(conceptual=True),
        verdict(matches=[99]),
        verdict(reason=""),
        verdict(overlap=float("nan")),
    ]:
        with pytest.raises(ValueError):
            validate_batch([value], candidates, {1: {8}}, "reference-version")
    with pytest.raises(ValueError):
        validate_batch(
            [verdict(), verdict()], candidates, {1: {8}}, "reference-version"
        )


def test_low_confidence_is_unscored_and_records_provenance():
    c = card(1, '<img src="x.png">', '<img src="y.png">')
    result = validate_batch([verdict(confidence="low")], [c], {1: set()}, "v1")["1"]
    assert result["score"] is None
    assert result["fingerprint"] == c.fingerprint
    assert result["reference_version"] == "v1"


def test_sort_is_numeric_deterministic_and_keeps_unscored_last():
    scores = {"1": {"score": 9}, "2": {"score": 100}, "3": {"score": None}}
    assert sort_ids([3, 2, 4, 1], scores, True) == [2, 1, 3, 4]
    assert sort_ids([3, 2, 4, 1], scores, False) == [1, 2, 3, 4]


def test_score_storage_roundtrip_and_reference_changes(tmp_path):
    from suspended_card_priority.core import load_scores, save_scores

    path = tmp_path / "scores.json"
    save_scores(path, {"1": {"score": 91}})
    assert load_scores(path) == {"1": {"score": 91}}
    before = ReferenceIndex([card(1, "question", "answer")]).version
    after = ReferenceIndex([card(1, "question", "changed")]).version
    assert before != after
    assert not path.with_suffix(".tmp").exists()


@pytest.fixture
def browser_module(monkeypatch):
    def module(name, **values):
        obj = types.ModuleType(name)
        obj.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, obj)
        return obj

    ns = types.SimpleNamespace
    module("aqt", gui_hooks=ns(), mw=ns())
    module("anki.errors", NotFoundError=LookupError)
    module(
        "anki.collection",
        BrowserColumns=ns(SORTING_DESCENDING=2, SORTING_NONE=0, ALIGNMENT_CENTER=1),
    )
    module("aqt.browser.table", Column=ns)
    module("aqt.operations", QueryOp=ns)
    module("aqt.qt", QAction=ns, Qt=ns(), sip=ns())
    module(
        "aqt.utils",
        showInfo=lambda *a, **k: None,
        showWarning=lambda *a, **k: None,
        tooltip=lambda *a, **k: None,
    )
    path = (
        Path(__file__).resolve().parents[2]
        / "addons/suspended_card_priority/browser.py"
    )
    spec = importlib.util.spec_from_file_location(
        "suspended_card_priority.browser", path
    )
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def test_browser_sort_preserves_search_and_other_addon_results(
    browser_module, monkeypatch
):
    b = browser_module
    ns = types.SimpleNamespace
    context = ns(
        ids=None,
        order=ns(key=b.KEY),
        reverse=True,
        addon_metadata={},
        browser=ns(table=ns(is_notes_mode=lambda: False)),
        search="deck:math is:suspended",
    )
    b._will_search(context)
    assert context.order is False
    assert context.search == "deck:math is:suspended"
    context.ids = [1, 2, 3]
    monkeypatch.setattr(
        b, "_current_score", lambda cid: {"score": {1: 9, 2: 91, 3: None}[cid]}
    )
    b._did_search(context)
    assert context.ids == [2, 1, 3]
    other = ns(ids=[8], order=ns(key=b.KEY), addon_metadata={})
    b._will_search(other)
    assert other.ids == [8] and not other.addon_metadata


def test_edited_or_unsuspended_cards_do_not_keep_numeric_scores(browser_module):
    b = browser_module
    c = types.SimpleNamespace(
        id=1, nid=1, queue=-1, question=lambda: "Q", answer=lambda: "A"
    )
    b.aqt.mw.col = types.SimpleNamespace(get_card=lambda _: c)
    b._scores = {"1": {"score": 81, "fingerprint": card(1, "Q", "A").fingerprint}}
    assert b._current_score(1)["score"] == 81
    c.answer = lambda: "edited"
    assert b._current_score(1)["stale"]
    c.queue = 0
    assert b._current_score(1) is None


def test_cli_response_validation_without_network(monkeypatch):
    from suspended_card_priority import scoring

    commands = []

    class Process:
        returncode = 0

        def __init__(self, command, **kwargs):
            commands.append(command)
            self.output = Path(command[command.index("--output-last-message") + 1])

        def communicate(self, input, timeout):
            assert "untrusted study data" in input
            self.output.write_text(json.dumps({"ratings": [verdict()]}))

        def poll(self):
            return 0

    monkeypatch.setattr(scoring, "resolve_codex", lambda _: "codex")
    monkeypatch.setattr(scoring.subprocess, "Popen", Process)
    result = scoring.score_batch([card(1, "Why?", "Because")], ReferenceIndex([]), {})
    assert result["1"]["score"] == 100
    assert commands[0][commands[0].index("--sandbox") + 1] == "read-only"


def test_cli_cancellation_terminates_process(monkeypatch):
    from suspended_card_priority import scoring

    terminated = []

    class Process:
        def __init__(self, *args, **kwargs):
            pass

        def poll(self):
            return None

        def terminate(self):
            terminated.append(True)

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(scoring, "resolve_codex", lambda _: "codex")
    monkeypatch.setattr(scoring.subprocess, "Popen", Process)
    with pytest.raises(InterruptedError):
        scoring.score_batch([card(1, "Q", "A")], ReferenceIndex([]), {}, lambda: True)
    assert terminated == [True]
