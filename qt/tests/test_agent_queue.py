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
    NO_PENDING_REQUEST,
    CollectionUnavailable,
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
    assert (
        patch_from_dict(json.loads(json.dumps(patch_to_dict(patch))), editor_snapshot())
        == patch
    )
    replace = NotePatch(
        summary="s",
        note_id=None,
        notetype_id=7,
        tag_patch=TagPatch(replace=("a", "b")),
    )
    assert (
        patch_from_dict(
            json.loads(json.dumps(patch_to_dict(replace))), editor_snapshot()
        )
        == replace
    )


def test_multi_note_patch_round_trips_through_json() -> None:
    patch = MultiNotePatch(
        summary="Both",
        note_updates=(
            MultiNoteUpdate(note_id=11, notetype_id=7, field_updates={"Back": "B"}),
            MultiNoteUpdate(note_id=12, notetype_id=7, tag_patch=TagPatch(add=("t",))),
        ),
    )
    assert (
        patch_from_dict(json.loads(json.dumps(patch_to_dict(patch))), multi_snapshot())
        == patch
    )


def test_patch_dict_uses_agent_schema_shape() -> None:
    data = patch_to_dict(note_patch())
    [update] = data["note_updates"]
    assert update["field_updates"] == [{"name": "Back", "html": "a sigh"}]
    assert update["tags"] == {"replace": None, "add": ["fixed"], "remove": ["old"]}
    assert data["note_creates"] == []


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


def test_failed_thread_with_current_proposal_can_be_applied(
    store: QueueStore,
) -> None:
    thread = store.create_thread(context(), settings(), "a")
    store.transition(thread.id, "queued", "running")
    store.add_proposal(thread.id, {"summary": "s"}, {"notes": []})
    store.transition(thread.id, "running", "ready")
    store.reply(thread.id, "more")
    store.transition(thread.id, "queued", "running")
    store.transition(thread.id, "running", "failed")
    assert store.current_proposal(thread.id) is not None
    store.transition(thread.id, "failed", "applied")
    assert [p.status for p in store.proposals(thread.id)] == ["applied"]


@pytest.mark.parametrize("back_to", ["ready", "failed"])
def test_reverting_an_apply_makes_the_proposal_current_again(
    store: QueueStore, back_to: str
) -> None:
    thread = store.create_thread(context(), settings(), "a")
    store.transition(thread.id, "queued", "running")
    store.add_proposal(thread.id, {"summary": "one"}, {"notes": []})
    store.add_proposal(thread.id, {"summary": "two"}, {"notes": []})
    store.transition(thread.id, "running", "ready")
    store.transition(thread.id, "ready", "applied")
    store.transition(thread.id, "applied", back_to)
    current = store.current_proposal(thread.id)
    assert current is not None and current.version == 2
    assert [p.status for p in store.proposals(thread.id)] == [
        "superseded",
        "current",
    ]


def test_complete_run_writes_messages_proposal_and_status_together(
    store: QueueStore,
) -> None:
    thread = store.create_thread(context(), settings(), "a")
    store.transition(thread.id, "queued", "running")
    store.set_error(thread.id, "old")
    store.complete_run(
        thread.id,
        to_status="ready",
        messages=[("system", "[summary]", ""), ("agent", "ok", "<p>ok</p>")],
        proposal={"summary": "s"},
        base={"notes": [{"note_id": 11}]},
    )
    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "ready" and loaded.error is None
    assert [(m.role, m.text) for m in store.messages(thread.id)] == [
        ("user", "a"),
        ("system", "[summary]"),
        ("agent", "ok"),
    ]
    proposal = store.current_proposal(thread.id)
    assert proposal is not None
    assert proposal.patch == {"summary": "s"}
    assert proposal.base == {"notes": [{"note_id": 11}]}


def test_complete_run_drop_proposal_supersedes_current(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "a")
    store.transition(thread.id, "queued", "running")
    store.add_proposal(thread.id, {"summary": "s"}, {"notes": []})
    store.complete_run(
        thread.id,
        to_status="answered",
        messages=[("agent", "no", "")],
        drop_proposal=True,
    )
    assert store.current_proposal(thread.id) is None
    assert [p.status for p in store.proposals(thread.id)] == ["superseded"]


def test_complete_run_writes_nothing_unless_running(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "a")
    with pytest.raises(QueueTransitionError):
        store.complete_run(
            thread.id,
            to_status="ready",
            messages=[("agent", "ok", "")],
            proposal={"summary": "s"},
            base={},
        )
    assert [m.role for m in store.messages(thread.id)] == ["user"]
    assert store.proposals(thread.id) == []
    assert store.get_thread(thread.id).status == "queued"  # type: ignore[union-attr]


def test_complete_run_on_a_closed_store_leaves_thread_running(
    tmp_path: Path,
) -> None:
    store = QueueStore.open(tmp_path / "q.sqlite")
    thread = store.create_thread(context(), settings(), "a")
    store.transition(thread.id, "queued", "running")
    store.close()
    with pytest.raises(Exception):
        store.complete_run(
            thread.id, to_status="answered", messages=[("agent", "ok", "")]
        )
    reopened = QueueStore.open(tmp_path / "q.sqlite")
    try:
        assert reopened.get_thread(thread.id).status == "running"  # type: ignore[union-attr]
        assert [m.role for m in reopened.messages(thread.id)] == ["user"]
    finally:
        reopened.close()


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


def test_set_settings_replaces_the_next_run_settings(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(provider="codex"), "a")
    store.set_settings(thread.id, {**settings(), "provider": "ollama", "model": "q"})
    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.settings["provider"] == "ollama"
    assert loaded.settings["model"] == "q"


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
        self.streamed: list[tuple[str, str]] = []
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
            on_stream_event=lambda thread_id, line: self.streamed.append(
                (thread_id, line)
            ),
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
    assert patch_from_dict(proposal.patch, editor_snapshot()) == note_patch()
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


def test_stream_events_are_forwarded_as_activity_lines(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "x", 11)

    def streaming(**kwargs: Any) -> AgentResult:
        kwargs["event_callback"]({"type": "exec_command_begin", "cmd": "rg x"})
        return result("ok")

    harness = Harness(store, FakeAgent([streaming]))
    harness.runner.run_once()
    assert harness.streamed == [(thread.id, "[tool] rg x")]


def test_card_reads_are_announced_as_activity_lines(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "x", 11)

    def lookup(**kwargs: Any) -> AgentResult:
        return result("looking", card_request={"query": "", "offset": 0, "limit": 5})

    harness = Harness(store, FakeAgent([lookup, result("found")]))
    harness.runner.run_once()
    assert (thread.id, "[cards] Reading permitted cards") in harness.streamed


def test_reviewer_context_is_included_in_every_prompt(store: QueueStore) -> None:
    reviewer = {
        "side": "answer",
        "question_html": "<p>唉声</p>",
        "answer_html": "<p>sigh <span style='color:blue'>example</span></p>",
        "selection": "example",
    }
    thread = store.create_thread(
        {**context(), "reviewer": reviewer}, settings(), "Fix the blue one", 11
    )
    agent = FakeAgent([result("first", (note_patch(),)), result("second")])
    harness = Harness(store, agent)
    harness.runner.run_once()
    store.reply(thread.id, "and the tags")
    harness.runner.run_once()

    for call in agent.calls:
        assert "Reviewer context" in call["prompt"]
        assert "not field content" in call["prompt"]
        assert json.dumps(reviewer, ensure_ascii=False) in call["prompt"]
    first = agent.calls[0]["prompt"]
    assert first.startswith("Fix the blue one")
    second = agent.calls[1]["prompt"]
    assert second.index("Reviewer context") < second.index("Current proposal")


def test_prompt_has_no_reviewer_section_without_reviewer_context(
    store: QueueStore,
) -> None:
    store.create_thread({**context(), "reviewer": None}, settings(), "x", 11)
    agent = FakeAgent([result()])
    Harness(store, agent).runner.run_once()
    assert "Reviewer context" not in agent.calls[0]["prompt"]


def test_shutdown_while_waiting_for_main_thread_requeues(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "x", 11)
    harness = Harness(store, FakeAgent([]))

    def stopped_main(_fn: Any) -> Any:
        harness.runner._stopping.set()
        raise AgentStopped("Anki is closing.")

    harness.runner._run_on_main = stopped_main
    harness.runner.run_once()
    assert store.get_thread(thread.id).status == "queued"  # type: ignore[union-attr]


def ready_thread_with_proposal(store: QueueStore, harness: Harness) -> str:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    harness.runner.run_once()
    assert store.current_proposal(thread.id) is not None
    return thread.id


def test_reply_without_patch_drops_the_current_proposal(store: QueueStore) -> None:
    agent = FakeAgent([result("first", (note_patch(),)), result("never mind")])
    harness = Harness(store, agent)
    thread_id = ready_thread_with_proposal(store, harness)
    store.reply(thread_id, "Actually, leave it")
    harness.runner.run_once()

    assert store.get_thread(thread_id).status == "answered"  # type: ignore[union-attr]
    assert store.current_proposal(thread_id) is None
    assert [p.status for p in store.proposals(thread_id)] == ["superseded"]
    assert "null patch drops the proposal" in agent.calls[1]["prompt"]


def test_failed_reply_keeps_the_current_proposal(store: QueueStore) -> None:
    agent = FakeAgent([result("first", (note_patch(),)), RuntimeError("cli died")])
    harness = Harness(store, agent)
    thread_id = ready_thread_with_proposal(store, harness)
    store.reply(thread_id, "Shorter")
    harness.runner.run_once()

    loaded = store.get_thread(thread_id)
    assert loaded is not None
    assert loaded.status == "failed" and loaded.error == "cli died"
    proposal = store.current_proposal(thread_id)
    assert proposal is not None and proposal.version == 1
    assert store.messages(thread_id)[-1].text == "cli died"


@pytest.mark.parametrize("where", ["note_state", "card_read"])
def test_closed_collection_requeues_without_error(
    store: QueueStore, where: str
) -> None:
    thread = store.create_thread(context(), settings(card_access="all"), "Fix back", 11)

    def lookup(**kwargs: Any) -> AgentResult:
        return result("looking", card_request={"query": "", "offset": 0, "limit": 5})

    harness = Harness(store, FakeAgent([lookup]))

    def closed(*_args: Any) -> Any:
        raise CollectionUnavailable("The collection is closed.")

    if where == "note_state":
        harness.runner._read_note_state = closed
    else:
        harness.runner._read_cards = closed

    harness.runner.run_once()

    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.status == "queued" and loaded.error is None
    assert [m.role for m in store.messages(thread.id)] == ["user"]
    assert harness.runner._requeued is True


def test_note_state_errors_fail_the_run(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    harness = Harness(store, FakeAgent([]))

    def broken(_ids: Any) -> Any:
        raise RuntimeError("backend hiccup")

    harness.runner._read_note_state = broken
    harness.runner.run_once()
    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.status == "failed" and loaded.error == "backend hiccup"


def test_shutdown_race_requeues_instead_of_cancelling(store: QueueStore) -> None:
    """A worker blocked on the main thread when Anki closes is re-queued."""
    entered = threading.Event()
    closing = threading.Event()

    def main(fn: Any) -> Any:
        entered.set()
        while not closing.is_set():
            time.sleep(0.01)
        raise AgentStopped("Anki is closing.")

    thread = store.create_thread(context(), settings(), "x", 11)
    harness = Harness(store, FakeAgent([]))
    harness.runner._run_on_main = main
    harness.runner.start()
    assert entered.wait(5)
    # close_service: request_stop() first, then unblock the main-thread bridge.
    harness.runner.request_stop()
    closing.set()
    harness.runner.join()
    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.status == "queued" and loaded.error is None


def test_claimed_thread_without_a_pending_request_fails_clearly(
    store: QueueStore,
) -> None:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    store.transition(thread.id, "queued", "running")
    store.add_message(thread.id, "agent", "done")  # a run whose status was lost
    store.reset_interrupted()
    agent = FakeAgent([])
    Harness(store, agent).runner.run_once()

    loaded = store.get_thread(thread.id)
    assert loaded is not None
    assert loaded.status == "failed" and loaded.error == NO_PENDING_REQUEST
    assert agent.calls == []


def test_run_results_are_not_written_when_the_store_rejects_them(
    store: QueueStore,
) -> None:
    thread = store.create_thread(context(), settings(), "Fix back", 11)
    harness = Harness(store, FakeAgent([result("ok", (note_patch(),))]))
    calls: list[str] = []
    original = store.complete_run

    def flaky(thread_id: str, **kwargs: Any) -> None:
        calls.append(kwargs["to_status"])
        raise RuntimeError("store closed")

    store.complete_run = flaky  # type: ignore[method-assign]
    harness.runner.run_once()
    store.complete_run = original  # type: ignore[method-assign]

    assert calls == ["ready", "failed"]
    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "running"
    assert [m.role for m in store.messages(thread.id)] == ["user"]
    assert store.proposals(thread.id) == []
    store.reset_interrupted()
    agent = FakeAgent([result("again")])
    Harness(store, agent).runner.run_once()
    assert agent.calls[0]["prompt"].startswith("Fix back")


# --- phase 2: unified patch schema, note creates ----------------------------

from editor_agent_pane.patches import (  # noqa: E402
    CreateDefaults,
    NoteCreate,
    NotetypeInfo,
    PatchValidationError,
    validate_patch,
)

NOTETYPES = {
    7: NotetypeInfo(name="Basic", field_names=("Front", "Back")),
    8: NotetypeInfo(name="Cloze", field_names=("Text", "Back Extra")),
}
DECK_IDS = {1, 3}
CATALOG = {
    "notetypes": [
        {"id": 8, "name": "Cloze", "fields": ["Text", "Back Extra"]},
        {"id": 7, "name": "Basic", "fields": ["Front", "Back"]},
    ],
    "decks": [{"id": 1, "name": "Default"}, {"id": 3, "name": "Chinese"}],
}


def create_raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "notetype_id": 7,
        "deck_id": 3,
        "fields": [
            {"name": "Back", "html": "sigh vs. shout"},
            {"name": "Front", "html": "唉声 vs 呼声"},
        ],
        "tags": ["chinese", " disambiguation "],
    }
    base.update(overrides)
    return base


def unified_raw(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "summary": "Disambiguate",
        "note_updates": [],
        "note_creates": [create_raw()],
        "assumptions": ["Used the Basic note type"],
        "questions": ["Should 呼声 get its own card too?"],
    }
    base.update(overrides)
    return base


def validate(raw: dict[str, Any], snapshot: Any = None) -> Any:
    return validate_patch(
        raw,
        snapshot or editor_snapshot(),
        notetypes=NOTETYPES,
        deck_ids=DECK_IDS,
    )


def test_validate_patch_accepts_a_creates_only_patch() -> None:
    patch = validate(unified_raw())
    assert isinstance(patch, NotePatch)
    assert patch.note_id == 11 and patch.notetype_id == 7
    assert patch.field_updates == {} and not patch.tag_patch.has_changes()
    assert patch.has_changes()
    assert patch.note_creates == (
        NoteCreate(
            notetype_id=7,
            deck_id=3,
            # reordered into the note type's field order
            fields=(
                FieldSnapshot("Front", "唉声 vs 呼声"),
                FieldSnapshot("Back", "sigh vs. shout"),
            ),
            tags=("chinese", "disambiguation"),
        ),
    )
    assert patch.assumptions == ("Used the Basic note type",)
    assert patch.questions == ("Should 呼声 get its own card too?",)


def test_validate_patch_unified_update_of_the_current_note() -> None:
    patch = validate(
        unified_raw(
            note_creates=[],
            note_updates=[
                {
                    "note_id": 11,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Back", "html": "a sigh"}],
                    "tags": {"replace": None, "add": ["fixed"], "remove": []},
                }
            ],
        )
    )
    assert isinstance(patch, NotePatch)
    assert patch.field_updates == {"Back": "a sigh"}
    assert patch.tag_patch.add == ("fixed",)
    assert patch.note_creates == ()


def test_validate_patch_unified_update_with_null_note_id_in_add_cards() -> None:
    snapshot = EditorSnapshot(
        mode="add",
        note_id=None,
        notetype_id=7,
        notetype_name="Basic",
        fields=(FieldSnapshot("Front", "x"), FieldSnapshot("Back", "")),
        tags=(),
    )
    patch = validate(
        unified_raw(
            note_creates=[],
            note_updates=[
                {
                    "note_id": None,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Back", "html": "y"}],
                    "tags": {"replace": None, "add": [], "remove": []},
                }
            ],
        ),
        snapshot,
    )
    assert isinstance(patch, NotePatch)
    assert patch.note_id is None and patch.field_updates == {"Back": "y"}


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"note_id": 12, "notetype_id": 7}, "different note"),
        ({"note_id": 11, "notetype_id": 8}, "different note type"),
        (
            {
                "note_id": 11,
                "notetype_id": 7,
                "field_updates": [{"name": "Nope", "html": "x"}],
            },
            "Unknown field: Nope",
        ),
    ],
)
def test_validate_patch_single_note_updates_target_only_the_current_note(
    update: dict[str, Any], message: str
) -> None:
    with pytest.raises(PatchValidationError, match=message):
        validate(unified_raw(note_updates=[update]))


def test_validate_patch_still_accepts_the_legacy_single_note_shape() -> None:
    patch = validate(
        {
            "summary": "Fix",
            "note_id": 11,
            "notetype_id": 7,
            "field_updates": [{"name": "Back", "html": "a sigh"}],
            "tags": {"replace": None, "add": [], "remove": []},
        }
    )
    assert isinstance(patch, NotePatch)
    assert patch.field_updates == {"Back": "a sigh"}


def test_validate_patch_multi_with_updates_and_creates() -> None:
    patch = validate(
        unified_raw(
            note_updates=[
                {
                    "note_id": 12,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Back", "html": "D!"}],
                    "tags": {"replace": None, "add": [], "remove": []},
                }
            ]
        ),
        multi_snapshot(),
    )
    assert isinstance(patch, MultiNotePatch)
    assert patch.affected_note_ids() == (12,)
    assert len(patch.note_creates) == 1
    assert patch.questions == ("Should 呼声 get its own card too?",)


def test_validate_patch_multi_creates_only_is_valid() -> None:
    patch = validate(unified_raw(), multi_snapshot())
    assert isinstance(patch, MultiNotePatch)
    assert patch.note_updates == () and patch.has_changes()


def test_validate_patch_multi_rejects_unselected_notes() -> None:
    with pytest.raises(PatchValidationError, match="not selected"):
        validate(
            unified_raw(note_updates=[{"note_id": 99, "notetype_id": 7}]),
            multi_snapshot(),
        )


def test_validate_patch_without_changes_raises() -> None:
    with pytest.raises(PatchValidationError, match="does not contain any changes"):
        validate(unified_raw(note_creates=[]))


@pytest.mark.parametrize(
    ("create", "message"),
    [
        (create_raw(notetype_id=99), "Unknown note type: 99"),
        (create_raw(deck_id=42), "Unknown deck: 42"),
        (
            create_raw(fields=[{"name": "Front", "html": "x"}]),
            "missing fields: Back",
        ),
        (
            create_raw(
                fields=[
                    {"name": "Front", "html": "x"},
                    {"name": "Back", "html": "y"},
                    {"name": "Notes", "html": "z"},
                ]
            ),
            "Unknown field for note type Basic: Notes",
        ),
        (
            create_raw(
                fields=[
                    {"name": "Front", "html": "x"},
                    {"name": "Front", "html": "y"},
                    {"name": "Back", "html": "y"},
                ]
            ),
            "Duplicate field: Front",
        ),
        (
            create_raw(
                fields=[
                    {"name": "Front", "html": "<p>&nbsp; <br></p>"},
                    {"name": "Back", "html": "y"},
                ]
            ),
            "first field \\(Front\\) must not be empty",
        ),
        (
            create_raw(
                fields=[
                    {"name": "Front", "html": "<html><body>x</body></html>"},
                    {"name": "Back", "html": "y"},
                ]
            ),
            "document wrapper",
        ),
        (create_raw(tags=["two words"]), "whitespace"),
        (create_raw(notetype_id="seven"), "notetype_id must be an integer"),
    ],
)
def test_validate_patch_rejects_bad_creates(
    create: dict[str, Any], message: str
) -> None:
    with pytest.raises(PatchValidationError, match=message):
        validate(unified_raw(note_creates=[create]))


def test_validate_patch_first_field_may_be_only_an_image() -> None:
    patch = validate(
        unified_raw(
            note_creates=[
                create_raw(
                    fields=[
                        {"name": "Front", "html": '<img src="a.png">'},
                        {"name": "Back", "html": ""},
                    ]
                )
            ]
        )
    )
    assert patch.note_creates[0].fields[0].html == '<img src="a.png">'


def test_validate_patch_without_a_catalog_checks_structure_only() -> None:
    patch = validate_patch(
        unified_raw(note_creates=[create_raw(notetype_id=99, deck_id=42)]),
        editor_snapshot(),
        notetypes=None,
        deck_ids=None,
    )
    assert patch.note_creates[0].notetype_id == 99
    with pytest.raises(PatchValidationError, match="must not be empty"):
        validate_patch(
            unified_raw(
                note_creates=[create_raw(fields=[{"name": "Front", "html": " "}])]
            ),
            editor_snapshot(),
            notetypes=None,
            deck_ids=None,
        )


def create_without_ids(**overrides: Any) -> dict[str, Any]:
    """create_raw() without notetype_id/deck_id, unless given in overrides."""
    raw = create_raw()
    del raw["notetype_id"], raw["deck_id"]
    raw.update(overrides)
    return raw


@pytest.mark.parametrize(
    "create",
    [
        create_without_ids(),
        create_without_ids(notetype_id=None, deck_id=None),
        create_without_ids(notetype_id="", deck_id=""),
    ],
)
def test_create_without_ids_takes_the_defaults(create: dict[str, Any]) -> None:
    patch = validate_patch(
        unified_raw(note_creates=[create]),
        editor_snapshot(),
        notetypes=NOTETYPES,
        deck_ids=DECK_IDS,
        defaults=CreateDefaults(notetype_id=7, deck_id=3),
    )
    [created] = patch.note_creates
    assert (created.notetype_id, created.deck_id) == (7, 3)


def test_create_accepts_numeric_string_ids() -> None:
    patch = validate(
        unified_raw(note_creates=[create_raw(notetype_id="7", deck_id="3")])
    )
    [created] = patch.note_creates
    assert (created.notetype_id, created.deck_id) == (7, 3)


@pytest.mark.parametrize("name", ["Cloze", "cloze"])
def test_create_resolves_a_note_type_name(name: str) -> None:
    create = create_raw(
        notetype_id=name,
        fields=[
            {"name": "Text", "html": "{{c1::a}}"},
            {"name": "Back Extra", "html": ""},
        ],
    )
    [created] = validate(unified_raw(note_creates=[create])).note_creates
    assert created.notetype_id == 8


@pytest.mark.parametrize(
    ("create", "message"),
    [
        (
            create_without_ids(notetype_id=7),
            "New note 1: no deck_id and no default deck.",
        ),
        (
            create_without_ids(deck_id=3),
            "New note 1: no notetype_id and no default note type.",
        ),
    ],
)
def test_create_without_ids_or_defaults_fails_against_the_collection(
    create: dict[str, Any], message: str
) -> None:
    with pytest.raises(PatchValidationError) as excinfo:
        validate(unified_raw(note_creates=[create]))
    assert str(excinfo.value) == message


def test_structure_only_create_without_ids_falls_back_to_the_snapshot() -> None:
    [single] = validate_patch(
        unified_raw(note_creates=[create_without_ids()]),
        editor_snapshot(),
        notetypes=None,
        deck_ids=None,
    ).note_creates
    assert (single.notetype_id, single.deck_id) == (7, None)
    [multi] = validate_patch(
        unified_raw(note_creates=[create_without_ids()]),
        multi_snapshot(),
        notetypes=None,
        deck_ids=None,
    ).note_creates
    assert (multi.notetype_id, multi.deck_id) == (7, None)


def test_create_without_a_deck_round_trips() -> None:
    patch = validate_patch(
        unified_raw(note_creates=[create_without_ids()]),
        editor_snapshot(),
        notetypes=None,
        deck_ids=None,
    )
    data = json.loads(json.dumps(patch_to_dict(patch)))
    assert data["note_creates"][0]["deck_id"] is None
    assert patch_from_dict(data, editor_snapshot()) == patch


@pytest.mark.parametrize("key", ["assumptions", "questions"])
def test_validate_patch_rejects_non_string_notes(key: str) -> None:
    with pytest.raises(PatchValidationError, match=key):
        validate(unified_raw(**{key: "just one"}))
    assert getattr(validate(unified_raw(**{key: [" ", "a "]})), key) == ("a",)


def note_patch_with_creates() -> NotePatch:
    return NotePatch(
        summary="Both",
        note_id=11,
        notetype_id=7,
        field_updates={"Back": "a sigh"},
        note_creates=(
            NoteCreate(
                notetype_id=7,
                deck_id=3,
                fields=(
                    FieldSnapshot("Front", "唉声 vs 呼声"),
                    FieldSnapshot("Back", "b"),
                ),
                tags=("disambiguation",),
            ),
        ),
        assumptions=("a",),
        questions=("q",),
    )


def test_patch_with_creates_round_trips_in_the_schema_shape() -> None:
    patch = note_patch_with_creates()
    data = json.loads(json.dumps(patch_to_dict(patch)))
    assert set(data) == {
        "summary",
        "note_updates",
        "note_creates",
        "assumptions",
        "questions",
    }
    assert data["note_updates"] == [
        {
            "note_id": 11,
            "notetype_id": 7,
            "field_updates": [{"name": "Back", "html": "a sigh"}],
            "tags": {"replace": None, "add": [], "remove": []},
        }
    ]
    assert data["note_creates"] == [
        {
            "notetype_id": 7,
            "deck_id": 3,
            "fields": [
                {"name": "Front", "html": "唉声 vs 呼声"},
                {"name": "Back", "html": "b"},
            ],
            "tags": ["disambiguation"],
        }
    ]
    assert data["assumptions"] == ["a"] and data["questions"] == ["q"]
    assert patch_from_dict(data, editor_snapshot()) == patch
    # A stored dict is valid agent output: it can be validated again.
    assert validate(data) == patch


def test_creates_only_patch_round_trips() -> None:
    patch = validate(unified_raw())
    data = json.loads(json.dumps(patch_to_dict(patch)))
    assert data["note_updates"] == []
    assert patch_from_dict(data, editor_snapshot()) == patch


def test_multi_patch_with_creates_round_trips() -> None:
    patch = validate(unified_raw(), multi_snapshot())
    data = json.loads(json.dumps(patch_to_dict(patch)))
    assert patch_from_dict(data, multi_snapshot()) == patch


def test_legacy_stored_single_note_patch_still_loads() -> None:
    legacy = {
        "summary": "Fix back",
        "note_id": 11,
        "notetype_id": 7,
        "field_updates": [{"name": "Back", "html": "a sigh"}],
        "tags": {"replace": None, "add": ["fixed"], "remove": ["old"]},
    }
    assert patch_from_dict(legacy, editor_snapshot()) == note_patch()


# --- schema and prompt -------------------------------------------------------


def test_output_schema_is_the_unified_patch_shape() -> None:
    from editor_agent_pane.codex_client import CODEX_OUTPUT_SCHEMA

    variants = CODEX_OUTPUT_SCHEMA["properties"]["patch"]["anyOf"]
    assert variants[0] == {"type": "null"}
    assert len(variants) == 2
    patch_schema = variants[1]
    assert set(patch_schema["required"]) == {
        "summary",
        "note_updates",
        "note_creates",
        "assumptions",
        "questions",
    }
    assert set(patch_schema["properties"]) == set(patch_schema["required"])
    create = patch_schema["properties"]["note_creates"]["items"]
    assert set(create["required"]) == {"notetype_id", "deck_id", "fields", "tags"}
    assert create["additionalProperties"] is False


def test_prompt_describes_creates_assumptions_and_questions() -> None:
    from editor_agent_pane.codex_client import PROMPT_TEMPLATE

    for text in ("note_creates", "assumptions", "questions", "note_updates"):
        assert text in PROMPT_TEMPLATE
    assert "top level of patch" not in PROMPT_TEMPLATE


@pytest.mark.parametrize("snapshot_kind", ["editor", "multi"])
def test_ollama_normalizer_fills_unified_defaults(snapshot_kind: str) -> None:
    from editor_agent_pane.ollama_client import _normalize_ollama_patch_data

    snapshot = editor_snapshot() if snapshot_kind == "editor" else multi_snapshot()
    normalized = _normalize_ollama_patch_data(
        {
            "note_updates": [
                {"note_id": 11, "field_updates": {"Back": "x"}},
            ],
            "note_creates": [
                {"notetype_id": 7, "deck_id": 3, "fields": {"Front": "f", "Back": ""}}
            ],
        },
        snapshot,
    )
    patch = validate(normalized, snapshot)
    assert patch.has_changes()
    assert patch.note_creates[0].fields[0] == FieldSnapshot("Front", "f")
    assert patch.note_creates[0].tags == ()


def test_ollama_normalizer_keeps_the_legacy_list_shape() -> None:
    from editor_agent_pane.ollama_client import _normalize_ollama_patch_data

    normalized = _normalize_ollama_patch_data(
        [{"name": "Back", "html": "x"}], editor_snapshot()
    )
    patch = validate(normalized)
    assert isinstance(patch, NotePatch) and patch.field_updates == {"Back": "x"}


# --- store: proposal results ---------------------------------------------------


def test_set_proposal_result_is_stored_on_the_proposal(store: QueueStore) -> None:
    thread = store.create_thread(context(), settings(), "a")
    proposal = store.add_proposal(thread.id, {"summary": "s"}, {"notes": []})
    assert proposal.result is None
    store.set_proposal_result(thread.id, 1, {"created_note_ids": [5, 6]})
    current = store.current_proposal(thread.id)
    assert current is not None
    assert current.result == {"created_note_ids": [5, 6]}


def test_v1_store_is_migrated(tmp_path: Path) -> None:
    import sqlite3

    from editor_agent_pane import queue_store

    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript(queue_store._SCHEMA_V1)
    conn.execute("pragma user_version = 1")
    conn.execute(
        "insert into threads values ('t', 0, 0, 'ready', 't', null, '{}', '{}',"
        " null, null, null)"
    )
    conn.execute(
        "insert into proposals values ('t', 1, 0, '{\"summary\": \"s\"}', '{}',"
        " 'current')"
    )
    conn.commit()
    conn.close()
    store = QueueStore.open(path)
    try:
        assert store.schema_version() >= 2
        proposal = store.current_proposal("t")
        assert proposal is not None and proposal.result is None
        store.set_proposal_result("t", 1, {"created_note_ids": [1]})
        assert store.current_proposal("t").result == {"created_note_ids": [1]}  # type: ignore[union-attr]
    finally:
        store.close()


# --- runner: creates ------------------------------------------------------------


class CatalogHarness(Harness):
    def __init__(self, store: QueueStore, agent: FakeAgent, **state: Any) -> None:
        super().__init__(store, agent, **state)
        self.catalog_reads = 0
        self.runner._read_catalog = self.read_catalog

    def read_catalog(self) -> dict[str, Any]:
        self.catalog_reads += 1
        return CATALOG


def creates_context(snapshot: Any = None) -> dict[str, Any]:
    return {**context(snapshot), "deck_id": 3}


def creates_only_patch() -> NotePatch:
    return validate(unified_raw())


def test_creates_only_proposal_is_stored_ready_without_a_base(
    store: QueueStore,
) -> None:
    thread = store.create_thread(creates_context(), settings(), "Disambiguate", 11)
    harness = CatalogHarness(store, FakeAgent([result("ok", (creates_only_patch(),))]))
    harness.runner.run_once()

    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "ready", loaded.error
    proposal = store.current_proposal(thread.id)
    assert proposal is not None
    assert proposal.base == {"notes": []}
    assert patch_from_dict(proposal.patch, editor_snapshot()) == creates_only_patch()
    assert proposal.patch["assumptions"] == ["Used the Basic note type"]
    assert proposal.patch["questions"] == ["Should 呼声 get its own card too?"]
    assert harness.catalog_reads == 1


def test_proposal_with_update_and_create_records_only_the_update_base(
    store: QueueStore,
) -> None:
    store.create_thread(creates_context(), settings(), "Both", 11)
    patch = note_patch_with_creates()
    harness = CatalogHarness(store, FakeAgent([result("ok", (patch,))]))
    harness.runner.run_once()
    [thread] = store.list_threads()
    proposal = store.current_proposal(thread.id)
    assert proposal is not None
    assert [n["note_id"] for n in proposal.base["notes"]] == [11]


@pytest.mark.parametrize(
    ("create", "message"),
    [
        (create_raw(notetype_id=99), "Unknown note type: 99"),
        (create_raw(deck_id=42), "Unknown deck: 42"),
    ],
)
def test_create_for_unknown_notetype_or_deck_fails_the_run(
    store: QueueStore, create: dict[str, Any], message: str
) -> None:
    # The client validated structure only; the runner checks the collection.
    patch = validate_patch(
        unified_raw(note_creates=[create]),
        editor_snapshot(),
        notetypes=None,
        deck_ids=None,
    )
    thread = store.create_thread(creates_context(), settings(), "Disambiguate", 11)
    CatalogHarness(store, FakeAgent([result("ok", (patch,))])).runner.run_once()
    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "failed"
    assert loaded.error is not None and message in loaded.error
    assert store.current_proposal(thread.id) is None
    assert message in store.messages(thread.id)[-1].text


def test_create_without_ids_is_ready_with_the_source_notetype_and_deck(
    store: QueueStore,
) -> None:
    # The client validated structure only; the runner fills the defaults.
    patch = validate_patch(
        unified_raw(note_creates=[create_without_ids()]),
        editor_snapshot(),
        notetypes=None,
        deck_ids=None,
    )
    thread = store.create_thread(creates_context(), settings(), "Disambiguate", 11)
    CatalogHarness(store, FakeAgent([result("ok", (patch,))])).runner.run_once()
    loaded = store.get_thread(thread.id)
    assert loaded is not None and loaded.status == "ready", loaded.error
    proposal = store.current_proposal(thread.id)
    assert proposal is not None
    [created] = patch_from_dict(proposal.patch, editor_snapshot()).note_creates
    assert (created.notetype_id, created.deck_id) == (7, 3)


def test_prompt_lists_note_types_and_decks_with_defaults_first(
    store: QueueStore,
) -> None:
    store.create_thread(creates_context(), settings(), "Disambiguate", 11)
    agent = FakeAgent([result()])
    CatalogHarness(store, agent).runner.run_once()
    prompt = agent.calls[0]["prompt"]
    marker = "Available note types and decks as JSON"
    assert marker in prompt
    section = json.loads(prompt.split(marker, 1)[1].split(":\n", 1)[1].split("\n")[0])
    assert section["notetypes"][0] == {
        "id": 7,
        "name": "Basic",
        "fields": ["Front", "Back"],
        "default": True,
    }
    assert "default" not in section["notetypes"][1]
    assert section["decks"][0] == {"id": 3, "name": "Chinese", "default": True}
    assert section["decks"][1] == {"id": 1, "name": "Default"}


def test_multi_prompt_defaults_to_the_first_selected_card(store: QueueStore) -> None:
    store.create_thread(context(multi_snapshot()), settings(), "x")
    agent = FakeAgent([result()])
    CatalogHarness(
        store,
        agent,
        note_state={
            11: {"mod": 1, "fields": ["a", "b"], "tags": []},
            12: {"mod": 1, "fields": ["c", "d"], "tags": []},
        },
    ).runner.run_once()
    prompt = agent.calls[0]["prompt"]
    section = json.loads(
        prompt.split("Available note types and decks as JSON", 1)[1]
        .split(":\n", 1)[1]
        .split("\n")[0]
    )
    assert section["notetypes"][0]["id"] == 7
    assert section["decks"][0]["id"] == 3


def test_catalog_errors_requeue_when_the_collection_is_closed(
    store: QueueStore,
) -> None:
    thread = store.create_thread(creates_context(), settings(), "x", 11)
    harness = CatalogHarness(store, FakeAgent([]))

    def closed() -> Any:
        raise CollectionUnavailable("closed")

    harness.runner._read_catalog = closed
    harness.runner.run_once()
    assert store.get_thread(thread.id).status == "queued"  # type: ignore[union-attr]
