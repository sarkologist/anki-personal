# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADDONS = ROOT / "addons"
if str(ADDONS) not in sys.path:
    sys.path.insert(0, str(ADDONS))

from editor_agent_pane.card_access import CardReadScope  # noqa: E402
from editor_agent_pane.codex_client import AgentResult, AgentStopped  # noqa: E402
from editor_agent_pane.patches import (  # noqa: E402
    EditorSnapshot,
    FieldSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    MultiNoteUpdate,
    NoteImageSnapshot,
    NotePatch,
    SelectedCardSnapshot,
    SelectedNoteSnapshot,
    SelectedTextSnapshot,
    TagPatch,
)
from editor_agent_pane.queue_runner import (  # noqa: E402
    QueueRunner,
    normalize_settings,
    note_state_hash,
)
from editor_agent_pane.queue_store import (  # noqa: E402
    QueueStore,
    QueueTransitionError,
)
from editor_agent_pane.snapshots import (  # noqa: E402
    patch_from_dict,
    patch_to_dict,
    snapshot_from_dict,
    snapshot_to_dict,
)

# --- fixtures ---------------------------------------------------------------


def editor_snapshot() -> EditorSnapshot:
    return EditorSnapshot(
        mode="edit_current",
        note_id=11,
        notetype_id=7,
        notetype_name="Basic",
        fields=(FieldSnapshot("Front", "唉声"), FieldSnapshot("Back", "sigh")),
        tags=("chinese",),
        current_field="Back",
        card_id=21,
        images=(
            NoteImageSnapshot(
                attachment_index=1,
                filename="a.png",
                fields=("Back",),
                path="/media/a.png",
            ),
        ),
        selected_text=SelectedTextSnapshot(
            field_name="Back",
            field_index=1,
            input_kind="rich_text",
            text="sigh",
            html="<b>sigh</b>",
        ),
    )


def multi_snapshot() -> MultiCardSnapshot:
    return MultiCardSnapshot(
        cards=(
            SelectedCardSnapshot(
                card_id=21,
                note_id=11,
                notetype_id=7,
                notetype_name="Basic",
                ord=0,
                template_name="Card 1",
                deck_id=3,
                deck_name="Chinese",
            ),
            SelectedCardSnapshot(
                card_id=22,
                note_id=12,
                notetype_id=7,
                notetype_name="Basic",
                ord=0,
                template_name="Card 1",
            ),
        ),
        notes=(
            SelectedNoteSnapshot(
                note_id=11,
                notetype_id=7,
                notetype_name="Basic",
                fields=(FieldSnapshot("Front", "a"), FieldSnapshot("Back", "b")),
                tags=("x",),
            ),
            SelectedNoteSnapshot(
                note_id=12,
                notetype_id=7,
                notetype_name="Basic",
                fields=(FieldSnapshot("Front", "c"), FieldSnapshot("Back", "d")),
                tags=(),
            ),
        ),
    )


def note_patch() -> NotePatch:
    return NotePatch(
        summary="Fix back",
        note_id=11,
        notetype_id=7,
        field_updates={"Back": "a sigh"},
        tag_patch=TagPatch(replace=None, add=("fixed",), remove=("old",)),
    )


def settings(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": "codex",
        "model": "gpt-5.5",
        "reasoning_effort": "high",
        "card_access": "all",
        "deck_ids": [],
        "project_folder": "",
        "project_folder_access": "read-only",
        "custom_instructions": "",
        "fast_mode": False,
        "stream_reasoning_summaries": True,
        "timeout_seconds": 300,
        "paths": {},
    }
    base.update(overrides)
    return base


def context(snapshot: EditorSnapshot | MultiCardSnapshot | None = None) -> dict:
    return {
        "source": "edit_current",
        "snapshot": snapshot_to_dict(snapshot or editor_snapshot()),
    }


@pytest.fixture
def store(tmp_path: Path):
    store = QueueStore.open(tmp_path / "agent_queue.sqlite")
    yield store
    store.close()


# --- snapshot / patch serialisation -----------------------------------------


def test_editor_snapshot_round_trips_through_json_including_image_paths() -> None:
    snapshot = editor_snapshot()
    data = json.loads(json.dumps(snapshot_to_dict(snapshot)))
    restored = snapshot_from_dict(data)
    assert restored == snapshot
    assert restored.image_paths() == ("/media/a.png",)


def test_editor_snapshot_without_optional_parts_round_trips() -> None:
    snapshot = EditorSnapshot(
        mode="add",
        note_id=None,
        notetype_id=7,
        notetype_name="Basic",
        fields=(FieldSnapshot("Front", ""),),
        tags=(),
    )
    assert snapshot_from_dict(json.loads(json.dumps(snapshot_to_dict(snapshot)))) == (
        snapshot
    )


def test_multi_card_snapshot_round_trips_through_json() -> None:
    snapshot = multi_snapshot()
    data = json.loads(json.dumps(snapshot_to_dict(snapshot)))
    assert snapshot_from_dict(data) == snapshot


def test_snapshot_from_dict_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError):
        snapshot_from_dict({"kind": "other"})


def test_note_patch_round_trips_through_json() -> None:
    patch = note_patch()
    assert patch_from_dict(json.loads(json.dumps(patch_to_dict(patch)))) == patch
    replace = NotePatch(
        summary="s",
        note_id=None,
        notetype_id=7,
        tag_patch=TagPatch(replace=("a", "b")),
    )
    assert patch_from_dict(json.loads(json.dumps(patch_to_dict(replace)))) == replace


def test_multi_note_patch_round_trips_through_json() -> None:
    patch = MultiNotePatch(
        summary="Both",
        note_updates=(
            MultiNoteUpdate(note_id=11, notetype_id=7, field_updates={"Back": "B"}),
            MultiNoteUpdate(note_id=12, notetype_id=7, tag_patch=TagPatch(add=("t",))),
        ),
    )
    assert patch_from_dict(json.loads(json.dumps(patch_to_dict(patch)))) == patch


def test_patch_dict_uses_agent_schema_shape() -> None:
    data = patch_to_dict(note_patch())
    assert data["field_updates"] == [{"name": "Back", "html": "a sigh"}]
    assert data["tags"] == {"replace": None, "add": ["fixed"], "remove": ["old"]}


# --- store ------------------------------------------------------------------


def test_create_thread_persists_context_settings_and_first_message(
    store: QueueStore, tmp_path: Path
) -> None:
    thread = store.create_thread(context(), settings(), "Fix the back field", 11)
    assert thread.status == "queued"
    assert thread.note_id == 11
    assert thread.title == "Fix the back field"
    assert thread.session is None and thread.error is None
    store.close()

    reopened = QueueStore.open(tmp_path / "agent_queue.sqlite")
    try:
        loaded = reopened.get_thread(thread.id)
        assert loaded is not None
        assert loaded.context == context()
        assert loaded.settings == settings()
        messages = reopened.messages(thread.id)
        assert [(m.seq, m.role, m.text) for m in messages] == [
            (1, "user", "Fix the back field")
        ]
    finally:
        reopened.close()


def test_title_is_truncated_first_line_of_prompt(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "x" * 100 + "\nmore")
    assert len(thread.title) <= 60
    assert thread.title.startswith("xxx")


def test_schema_version_is_recorded(store: QueueStore) -> None:
    assert store.schema_version() >= 1


def test_get_thread_returns_none_for_unknown_id(store: QueueStore) -> None:
    assert store.get_thread("missing") is None


def test_threads_for_note_and_list_threads_filter(store: QueueStore) -> None:
    a = store.create_thread(context(), settings(), "a", 11)
    b = store.create_thread(context(), settings(), "b", 11)
    c = store.create_thread(context(), settings(), "c", 12)
    store.transition(b.id, "queued", "running")
    assert [t.id for t in store.threads_for_note(11)] == [a.id, b.id]
    assert [t.id for t in store.threads_for_note(12)] == [c.id]
    assert {t.id for t in store.list_threads(statuses=("queued",))} == {a.id, c.id}
    assert len(store.list_threads()) == 3


def test_messages_are_sequenced(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "first")
    store.add_message(thread.id, "agent", "answer", "<p>answer</p>")
    store.add_message(thread.id, "system", "activity")
    messages = store.messages(thread.id)
    assert [(m.seq, m.role) for m in messages] == [
        (1, "user"),
        (2, "agent"),
        (3, "system"),
    ]
    assert messages[1].html == "<p>answer</p>"
    with pytest.raises(ValueError):
        store.add_message(thread.id, "robot", "x")


def test_add_proposal_supersedes_previous_and_bumps_version(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "p")
    assert store.current_proposal(thread.id) is None
    first = store.add_proposal(thread.id, {"summary": "one"}, {"notes": []})
    second = store.add_proposal(thread.id, {"summary": "two"}, {"notes": []})
    assert (first.version, second.version) == (1, 2)
    current = store.current_proposal(thread.id)
    assert current is not None
    assert current.version == 2 and current.patch == {"summary": "two"}
    statuses = [p.status for p in store.proposals(thread.id)]
    assert statuses == ["superseded", "current"]


def test_legal_transitions_follow_state_machine(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "p")
    store.transition(thread.id, "queued", "running")
    store.transition(thread.id, "running", "ready")
    store.transition(thread.id, "ready", "queued")
    store.transition(thread.id, "queued", "running")
    store.transition(thread.id, "running", "answered")
    store.transition(thread.id, "answered", "queued")
    store.transition(thread.id, "queued", "running")
    store.transition(thread.id, "running", "failed")
    store.transition(thread.id, "failed", "queued")
    store.transition(thread.id, "queued", "running")
    store.transition(thread.id, "running", "ready")
    store.transition(thread.id, "ready", "stale")
    store.transition(thread.id, "stale", "applied")
    assert store.get_thread(thread.id).status == "applied"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ((), ("queued", "ready")),
        ((), ("queued", "applied")),
        (("running",), ("running", "applied")),
        (("running", "ready", "applied"), ("applied", "queued")),
        (("running", "answered"), ("answered", "applied")),
    ],
)
def test_illegal_transitions_raise(
    store: QueueStore, path: tuple[str, ...], illegal: tuple[str, str]
) -> None:
    thread = store.create_thread(context(), settings(), "p")
    status = "queued"
    for target in path:
        store.transition(thread.id, status, target)
        status = target
    with pytest.raises(QueueTransitionError):
        store.transition(thread.id, *illegal)


def test_transition_from_wrong_current_status_raises(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "p")
    with pytest.raises(QueueTransitionError):
        store.transition(thread.id, "running", "ready")
    assert store.get_thread(thread.id).status == "queued"  # type: ignore[union-attr]


def test_apply_and_discard_close_current_proposal(store: QueueStore) -> None:
    applied = store.create_thread(context(), settings(), "a")
    discarded = store.create_thread(context(), settings(), "d")
    for thread in (applied, discarded):
        store.transition(thread.id, "queued", "running")
        store.add_proposal(thread.id, {"summary": "s"}, {"notes": []})
        store.transition(thread.id, "running", "ready")
    store.transition(applied.id, "ready", "applied")
    store.transition(discarded.id, "ready", "discarded")
    assert store.current_proposal(applied.id) is None
    assert [p.status for p in store.proposals(applied.id)] == ["applied"]
    assert [p.status for p in store.proposals(discarded.id)] == ["discarded"]


def test_next_runnable_is_oldest_queued(store: QueueStore) -> None:
    a = store.create_thread(context(), settings(), "a")
    b = store.create_thread(context(), settings(), "b")
    assert store.next_runnable().id == a.id  # type: ignore[union-attr]
    store.transition(a.id, "queued", "running")
    assert store.next_runnable().id == b.id  # type: ignore[union-attr]
    store.transition(b.id, "queued", "running")
    assert store.next_runnable() is None


def test_claim_next_marks_running_atomically(store: QueueStore) -> None:
    a = store.create_thread(context(), settings(), "a")
    claimed = store.claim_next()
    assert claimed is not None and claimed.id == a.id and claimed.status == "running"
    assert store.claim_next() is None


def test_reply_appends_message_and_requeues(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "a")
    store.transition(thread.id, "queued", "running")
    store.transition(thread.id, "running", "answered")
    store.set_error(thread.id, "old")
    store.reply(thread.id, "and also", "<p>and also</p>")
    reloaded = store.get_thread(thread.id)
    assert reloaded is not None and reloaded.status == "queued"
    assert reloaded.error is None
    assert store.messages(thread.id)[-1].text == "and also"
    store.transition(thread.id, "queued", "running")
    with pytest.raises(QueueTransitionError):
        store.reply(thread.id, "too early")


def test_reset_interrupted_requeues_running(store: QueueStore) -> None:
    a = store.create_thread(context(), settings(), "a")
    b = store.create_thread(context(), settings(), "b")
    store.transition(a.id, "queued", "running")
    assert store.reset_interrupted() == 1
    assert store.get_thread(a.id).status == "queued"  # type: ignore[union-attr]
    assert store.get_thread(b.id).status == "queued"  # type: ignore[union-attr]


def test_set_error_session_and_run_log_path(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "a")
    store.set_error(thread.id, "boom")
    store.set_session(thread.id, {"provider": "claude", "session_id": "s1"})
    store.set_run_log_path(thread.id, "/logs/run.jsonl")
    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.error == "boom"
    assert loaded.session == {"provider": "claude", "session_id": "s1"}
    assert loaded.run_log_path == "/logs/run.jsonl"
    store.set_session(thread.id, None)
    assert store.get_thread(thread.id).session is None  # type: ignore[union-attr]


def test_sweep_removes_old_terminal_threads_only(store: QueueStore) -> None:
    old_applied = store.create_thread(context(), settings(), "a")
    old_ready = store.create_thread(context(), settings(), "r")
    new_discarded = store.create_thread(context(), settings(), "d")
    for thread in (old_applied, old_ready, new_discarded):
        store.transition(thread.id, "queued", "running")
        store.add_proposal(thread.id, {"summary": "s"}, {"notes": []})
        store.transition(thread.id, "running", "ready")
    store.transition(old_applied.id, "ready", "applied")
    store.transition(new_discarded.id, "ready", "discarded")
    old = time.time() - 40 * 86400
    store._set_updated_at_for_tests(old_applied.id, old)
    store._set_updated_at_for_tests(old_ready.id, old)

    assert store.sweep(30) == 1
    assert store.get_thread(old_applied.id) is None
    assert store.messages(old_applied.id) == []
    assert store.proposals(old_applied.id) == []
    assert store.get_thread(old_ready.id) is not None
    assert store.get_thread(new_discarded.id) is not None


def test_store_is_usable_from_other_threads(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "a")
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for i in range(20):
                store.add_message(thread.id, "system", f"m{i}")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    seqs = [m.seq for m in store.messages(thread.id)]
    assert seqs == list(range(1, 82))


# --- settings ---------------------------------------------------------------


def test_normalize_settings_cleans_values() -> None:
    normalized = normalize_settings(
        {
            "provider": "bogus",
            "model": " gpt ",
            "reasoning_effort": "minimal",
            "card_access": "everything",
            "deck_ids": ["3", 4],
            "project_folder_access": "nonsense",
            "fast_mode": "yes",
            "timeout_seconds": "x",
            "paths": {"codex_path": " /bin/codex ", "unknown": "x"},
        }
    )
    assert normalized["provider"] == "codex"
    assert normalized["model"] == "gpt"
    assert normalized["reasoning_effort"] == ""
    assert normalized["card_access"] == "current"
    assert normalized["deck_ids"] == [3, 4]
    assert normalized["project_folder_access"] in ("read-only", "workspace-write")
    assert normalized["fast_mode"] is False
    assert normalized["timeout_seconds"] == 300
    assert normalized["paths"]["codex_path"] == "/bin/codex"
    assert "unknown" not in normalized["paths"]


def test_normalize_settings_drops_effort_for_ollama_and_unsupported() -> None:
    assert normalize_settings(settings(provider="ollama"))["reasoning_effort"] == ""
    assert (
        normalize_settings(settings(provider="claude", reasoning_effort="none"))[
            "reasoning_effort"
        ]
        == ""
    )


# --- runner -----------------------------------------------------------------


class FakeAgent:
    def __init__(self, results: list[Any]) -> None:
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    def send(self, **kwargs: Any) -> AgentResult:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if callable(result):
            return result(**kwargs)
        if isinstance(result, BaseException):
            raise result
        return result


class Harness:
    def __init__(self, store: QueueStore, agent: FakeAgent, **state: Any) -> None:
        self.store = store
        self.agent = agent
        self.notified: list[str] = []
        self.main_calls = 0
        self.card_reads: list[tuple[CardReadScope, Any]] = []
        self.made_with: list[dict[str, Any]] = []
        self.note_state: dict[int, Any] = state.get(
            "note_state",
            {11: {"mod": 100, "fields": ["唉声", "sigh"], "tags": ["chinese"]}},
        )
        self.runner = QueueRunner(
            store,
            run_on_main=self.run_on_main,
            read_cards=self.read_cards,
            read_note_state=self.read_note_state,
            make_agent=self.make_agent,
            notify=self.notified.append,
            logger_factory=lambda thread: (None, f"/logs/{thread.id}.jsonl"),
        )

    def run_on_main(self, fn: Any) -> Any:
        self.main_calls += 1
        return fn()

    def read_cards(self, scope: CardReadScope, request: Any) -> dict[str, Any]:
        self.card_reads.append((scope, request))
        return {"cards": [], "total": 0, "next_offset": None, "read_only": True}

    def read_note_state(self, note_ids: list[int]) -> dict[int, Any]:
        return {nid: self.note_state.get(nid) for nid in note_ids}

    def make_agent(self, settings: dict[str, Any]) -> FakeAgent:
        self.made_with.append(settings)
        return self.agent


def result(text: str = "done", proposals: tuple = (), **kw: Any) -> AgentResult:
    return AgentResult(text=text, html=f"<p>{text}</p>", proposals=proposals, **kw)


def test_run_with_patch_stores_proposal_and_marks_ready(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    harness = Harness(store, FakeAgent([result("ok", (note_patch(),))]))

    assert harness.runner.run_once() == thread.id

    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.status == "ready"
    assert loaded.run_log_path == f"/logs/{thread.id}.jsonl"
    assert loaded.session is None
    roles = [(m.role, m.text) for m in store.messages(thread.id)]
    assert roles[0] == ("user", "Fix back")
    assert ("agent", "ok") in roles
    assert any(role == "system" and "took" in text for role, text in roles)
    proposal = store.current_proposal(thread.id)
    assert proposal is not None and proposal.version == 1
    assert patch_from_dict(proposal.patch) == note_patch()
    assert proposal.base == {
        "notes": [
            {
                "note_id": 11,
                "mod": 100,
                "hash": note_state_hash(["唉声", "sigh"], ["chinese"]),
            }
        ]
    }
    assert thread.id in harness.notified
    assert harness.made_with[0]["provider"] == "codex"


def test_run_passes_snapshot_prompt_and_empty_history(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    agent = FakeAgent([result()])
    Harness(store, agent).runner.run_once()
    call = agent.calls[0]
    assert call["prompt"].startswith("Fix back")
    assert call["history"] == []
    assert isinstance(call["snapshot"], EditorSnapshot)
    assert call["snapshot"].image_paths() == ("/media/a.png",)
    assert callable(call["stop_requested"])
    del thread


def test_run_refreshes_snapshot_fields_from_current_note(store: QueueStore) -> None:
    store.create_thread(context(), settings(), "Fix back", 11)
    agent = FakeAgent([result()])
    harness = Harness(
        store,
        agent,
        note_state={11: {"mod": 5, "fields": ["唉声", "edited"], "tags": ["t"]}},
    )
    harness.runner.run_once()
    snapshot = agent.calls[0]["snapshot"]
    assert snapshot.field_html("Back") == "edited"
    assert snapshot.tags == ("t",)


def test_answer_without_patch_marks_answered(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "Explain", 11)
    Harness(store, FakeAgent([result("because")])).runner.run_once()
    assert store.get_thread(thread.id).status == "answered"  # type: ignore[union-attr]
    assert store.current_proposal(thread.id) is None


def test_reply_replays_history_and_current_proposal(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    agent = FakeAgent([result("first", (note_patch(),)), result("second")])
    harness = Harness(store, agent)
    harness.runner.run_once()
    store.reply(thread.id, "Move the example to the back")
    harness.runner.run_once()

    call = agent.calls[1]
    assert call["history"] == [("Fix back", "first")]
    assert call["prompt"].startswith("Move the example to the back")
    assert "Current proposal (version 1) as JSON" in call["prompt"]
    assert json.dumps(patch_to_dict(note_patch()), ensure_ascii=False) in call["prompt"]


def test_reply_after_failure_joins_unanswered_user_messages(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    agent = FakeAgent([RuntimeError("cli died"), result("ok")])
    harness = Harness(store, agent)
    harness.runner.run_once()
    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "failed"
    assert loaded.error == "cli died"
    store.reply(thread.id, "try again")
    harness.runner.run_once()
    call = agent.calls[1]
    assert call["history"] == []
    assert call["prompt"].startswith("Fix back\n\ntry again")
    assert store.get_thread(thread.id).status == "answered"  # type: ignore[union-attr]


def test_card_lookups_use_thread_scope_via_main_thread(store: QueueStore) -> None:
    store.create_thread(context(), settings(card_access="deck", deck_ids=[3]), "x", 11)

    def lookup(**kwargs: Any) -> AgentResult:
        return result("looking", card_request={"query": "", "offset": 0, "limit": 5})

    agent = FakeAgent([lookup, result("found")])
    harness = Harness(store, agent)
    harness.runner.run_once()
    assert harness.card_reads == [
        (CardReadScope("deck", (3,)), {"query": "", "offset": 0, "limit": 5})
    ]
    assert harness.main_calls >= 2  # note state + card read
    assert "Anki read-only card access" in agent.calls[1]["prompt"]


def test_mismatched_patch_type_fails_thread(store: QueueStore) -> None:
    thread = store.create_thread(context(multi_snapshot()), settings(), "x")
    agent = FakeAgent([result("bad", (note_patch(),))])
    Harness(store, agent).runner.run_once()
    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "failed"
    assert store.current_proposal(thread.id) is None


def test_multi_note_patch_records_base_for_each_note(store: QueueStore) -> None:
    thread = store.create_thread(context(multi_snapshot()), settings(), "x")
    patch = MultiNotePatch(
        summary="s",
        note_updates=(
            MultiNoteUpdate(note_id=11, notetype_id=7, field_updates={"Back": "B"}),
            MultiNoteUpdate(note_id=12, notetype_id=7, field_updates={"Back": "D"}),
        ),
    )
    harness = Harness(
        store,
        FakeAgent([result("ok", (patch,))]),
        note_state={
            11: {"mod": 1, "fields": ["a", "b"], "tags": ["x"]},
            12: None,
        },
    )
    harness.runner.run_once()
    proposal = store.current_proposal(thread.id)
    assert proposal is not None
    assert proposal.base == {
        "notes": [
            {"note_id": 11, "mod": 1, "hash": note_state_hash(["a", "b"], ["x"])},
            {"note_id": 12, "mod": None, "hash": None},
        ]
    }


def test_agent_stopped_marks_failed_cancelled(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "x", 11)
    Harness(store, FakeAgent([AgentStopped("stop")])).runner.run_once()
    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.status == "failed" and loaded.error == "cancelled"


def test_corrupt_context_fails_thread_without_crashing(store: QueueStore) -> None:
    thread = store.create_thread({"snapshot": {"kind": "nope"}}, settings(), "x")
    harness = Harness(store, FakeAgent([]))
    assert harness.runner.run_once() == thread.id
    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "failed" and loaded.error


def test_run_once_returns_none_when_idle(store: QueueStore) -> None:
    assert Harness(store, FakeAgent([])).runner.run_once() is None


def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_background_worker_processes_kicked_threads(store: QueueStore) -> None:
    harness = Harness(store, FakeAgent([result("a"), result("b")]))
    harness.runner.start()
    try:
        first = store.create_thread(context(), settings(), "one", 11)
        harness.runner.enqueue_kick()
        second = store.create_thread(context(), settings(), "two", 11)
        harness.runner.enqueue_kick()
        wait_for(
            lambda: all(
                store.get_thread(t.id).status == "answered"  # type: ignore[union-attr]
                for t in (first, second)
            )
        )
    finally:
        harness.runner.stop()


def test_cancel_running_thread_marks_failed(store: QueueStore) -> None:
    started = threading.Event()

    def block(**kwargs: Any) -> AgentResult:
        started.set()
        while not kwargs["stop_requested"]():
            time.sleep(0.01)
        raise AgentStopped("stopped")

    thread = store.create_thread(context(), settings(), "x", 11)
    harness = Harness(store, FakeAgent([block]))
    harness.runner.start()
    try:
        assert started.wait(5)
        assert harness.runner.cancel(thread.id)
        wait_for(
            lambda: store.get_thread(thread.id).status == "failed"  # type: ignore[union-attr]
        )
        assert store.get_thread(thread.id).error == "cancelled"  # type: ignore[union-attr]
    finally:
        harness.runner.stop()


def test_cancel_queued_thread_marks_failed(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "x", 11)
    harness = Harness(store, FakeAgent([]))
    assert harness.runner.cancel(thread.id)
    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.status == "failed" and loaded.error == "cancelled"
    assert thread.id in harness.notified
    assert not harness.runner.cancel("missing")


def test_stop_during_run_requeues_thread(store: QueueStore) -> None:
    started = threading.Event()

    def block(**kwargs: Any) -> AgentResult:
        started.set()
        while not kwargs["stop_requested"]():
            time.sleep(0.01)
        raise AgentStopped("stopped")

    thread = store.create_thread(context(), settings(), "x", 11)
    harness = Harness(store, FakeAgent([block]))
    harness.runner.start()
    assert started.wait(5)
    harness.runner.stop()
    assert store.get_thread(thread.id).status == "queued"  # type: ignore[union-attr]


def test_start_resets_interrupted_threads(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "x", 11)
    store.transition(thread.id, "queued", "running")
    harness = Harness(store, FakeAgent([result()]))
    harness.runner.start()
    try:
        wait_for(
            lambda: store.get_thread(thread.id).status == "answered"  # type: ignore[union-attr]
        )
    finally:
        harness.runner.stop()
