# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADDONS = ROOT / "addons"
if str(ADDONS) not in sys.path:
    sys.path.insert(0, str(ADDONS))

from editor_agent_pane.activity import (  # noqa: E402
    CodexActivityRenderer,
    compact_activity_transcript,
)
from editor_agent_pane.codex_client import (  # noqa: E402
    CodexCliAgent,
    resolve_codex_path,
)
from editor_agent_pane.patches import (  # noqa: E402
    EditorSnapshot,
    FieldSnapshot,
    PatchValidationError,
    validate_note_patch,
)
from editor_agent_pane.sanitize import sanitize_html  # noqa: E402
from editor_agent_pane.sources import (  # noqa: E402
    SourceAccessError,
    read_source_file,
    search_source_files,
)
from editor_agent_pane.surface import (  # noqa: E402
    render_assistant_message,
    render_error_message,
    render_proposal_diff,
    render_user_message,
)


def snapshot() -> EditorSnapshot:
    return EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(name="Front", html="old front"),
            FieldSnapshot(name="Back", html="old back"),
        ),
        tags=("keep", "remove-me"),
    )


class FakeStdin:
    def __init__(self) -> None:
        self.text = ""
        self.closed = False

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def close(self) -> None:
        self.closed = True


class FakePopen:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = 0,
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_read_source_file_rejects_traversal_and_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(SourceAccessError, match="traversal"):
        read_source_file(root, "../outside.md")

    with pytest.raises(SourceAccessError, match="relative"):
        read_source_file(root, outside)


def test_read_source_file_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = root / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(SourceAccessError, match="escapes"):
        read_source_file(root, "linked.md")


def test_read_source_file_rejects_binary_and_large_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    binary = root / "binary.txt"
    binary.write_bytes(b"hello\x00world")
    large = root / "large.md"
    large.write_text("x" * 20, encoding="utf-8")

    with pytest.raises(SourceAccessError, match="binary"):
        read_source_file(root, "binary.txt")

    with pytest.raises(SourceAccessError, match="large"):
        read_source_file(root, "large.md", max_bytes=10)


def test_search_source_files_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    for index in range(4):
        (root / f"file{index}.md").write_text(
            f"needle line {index}\nneedle again {index}",
            encoding="utf-8",
        )

    hits = search_source_files(root, "needle", max_results=3, max_files=10)

    assert len(hits) == 3
    assert hits[0].path == "file0.md"
    assert hits[0].line == 1


def test_sanitize_html_allows_formatting_and_mathjax() -> None:
    assert (
        sanitize_html("<p>Use <strong>canonical</strong> divisors \\[K_X\\].</p>")
        == "<p>Use <strong>canonical</strong> divisors \\[K_X\\].</p>"
    )


def test_sanitize_html_strips_scripts_events_and_unsafe_links() -> None:
    assert sanitize_html(
        '<script>alert(1)</script><p onclick="evil()">Hi</p>'
        '<a href="javascript:alert(1)" title="unsafe">bad</a>'
        '<a href="https://example.test/?a=1&b=2">good</a>'
    ) == (
        '<p>Hi</p><a title="unsafe">bad</a>'
        '<a href="https://example.test/?a=1&amp;b=2" rel="noopener noreferrer">good</a>'
    )


def test_sanitize_html_escapes_unknown_tags() -> None:
    assert sanitize_html("<custom data-x='1'>x</custom>") == (
        "&lt;custom data-x=&#x27;1&#x27;&gt;x&lt;/custom&gt;"
    )


def test_surface_rendering_helpers_escape_and_sanitize() -> None:
    assert "&lt;b&gt;hi&lt;/b&gt;<br>again" in render_user_message("<b>hi</b>\nagain")
    assert "<script>" not in render_assistant_message(
        "<p>Math \\(x^2\\)</p><script>bad()</script>",
        "fallback",
    )
    assert "\\(x^2\\)" in render_assistant_message(
        "<p>Math \\(x^2\\)</p><script>bad()</script>",
        "fallback",
    )
    assert "&lt;boom&gt;" in render_error_message("<boom>")


def test_render_proposal_diff_includes_sanitized_field_preview_and_diff() -> None:
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(
                name="Front",
                html='<p onclick="evil()">old \\(x\\)</p><script>bad()</script>',
            ),
        ),
        tags=(),
    )
    patch = validate_note_patch(
        {
            "summary": "Improve field",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [
                {
                    "name": "Front",
                    "html": '<p>new \\[K_X\\]</p><a href="https://example.test">ok</a>',
                }
            ],
            "tags": {"replace": None, "add": [], "remove": []},
        },
        current,
    )

    rendered = render_proposal_diff(current, patch)
    preview = rendered.split('<div class="agent-diff-heading">', maxsplit=1)[0]

    assert "Improve field" in rendered
    assert '<div class="agent-preview-heading">Current</div>' in rendered
    assert '<div class="agent-preview-heading">Proposed</div>' in rendered
    assert "<p>old \\(x\\)</p>" in preview
    assert "<p>new \\[K_X\\]</p>" in preview
    assert "<script>" not in preview
    assert "onclick" not in preview
    assert 'href="https://example.test"' in preview
    assert "agent-diff-file" in rendered
    assert "agent-diff-hunk" in rendered
    assert "agent-diff-del" in rendered
    assert "agent-diff-add" in rendered
    assert "+&lt;p&gt;new \\[K_X\\]&lt;/p&gt;" in rendered


def test_render_proposal_diff_includes_tag_preview_and_diff() -> None:
    patch = validate_note_patch(
        {
            "summary": "Retag",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [],
            "tags": {"replace": None, "add": ["agent"], "remove": ["remove-me"]},
        },
        snapshot(),
    )

    rendered = render_proposal_diff(snapshot(), patch)

    assert "Tags" in rendered
    assert '<span class="agent-tag">keep</span>' in rendered
    assert '<span class="agent-tag">agent</span>' in rendered
    assert (
        '<div class="agent-diff-row agent-diff-del">-keep remove-me</div>' in rendered
    )
    assert '<div class="agent-diff-row agent-diff-add">+keep agent</div>' in rendered


def test_validate_note_patch_accepts_current_note_fields_and_tags() -> None:
    patch = validate_note_patch(
        {
            "summary": "Tighten wording",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [{"name": "Front", "html": "new front"}],
            "tags": {"replace": None, "add": ["agent"], "remove": ["remove-me"]},
        },
        snapshot(),
    )

    assert patch.field_updates == {"Front": "new front"}
    assert patch.tag_patch.apply(snapshot().tags) == ("keep", "agent")


def test_validate_note_patch_rejects_unknown_field() -> None:
    with pytest.raises(PatchValidationError, match="Unknown field"):
        validate_note_patch(
            {
                "summary": "Bad field",
                "notetype_id": 7,
                "field_updates": [{"name": "Extra", "html": "no"}],
            },
            snapshot(),
        )


def test_validate_note_patch_rejects_stale_note_and_notetype() -> None:
    with pytest.raises(PatchValidationError, match="different note"):
        validate_note_patch(
            {
                "summary": "Wrong note",
                "note_id": 456,
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "new"}],
            },
            snapshot(),
        )

    with pytest.raises(PatchValidationError, match="different note type"):
        validate_note_patch(
            {
                "summary": "Wrong type",
                "note_id": 123,
                "notetype_id": 8,
                "field_updates": [{"name": "Front", "html": "new"}],
            },
            snapshot(),
        )


def test_validate_note_patch_rejects_whitespace_tags() -> None:
    with pytest.raises(PatchValidationError, match="whitespace"):
        validate_note_patch(
            {
                "summary": "Bad tag",
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "new"}],
                "tags": {"add": ["two words"]},
            },
            snapshot(),
        )


def test_validate_note_patch_can_replace_all_tags() -> None:
    patch = validate_note_patch(
        {
            "summary": "Retag",
            "notetype_id": 7,
            "field_updates": [{"name": "Front", "html": "new front"}],
            "tags": {"replace": ["fresh"], "add": [], "remove": []},
        },
        snapshot(),
    )

    assert patch.tag_patch.apply(snapshot().tags) == ("fresh",)


def test_codex_agent_uses_read_only_cli_and_parses_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        captured["stdin"] = stdin
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["text"] = text
        captured["bufsize"] = bufsize
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            """
            {
              "message": "Looks better with a shorter front.",
              "message_html": "<p>Looks <strong>better</strong> with a shorter front.</p>",
              "patch": {
                "summary": "Shorten front",
                "note_id": 123,
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "new front"}],
                "tags": {"replace": null, "add": ["agent"], "remove": []}
              }
            }
            """,
            encoding="utf-8",
        )
        process = FakePopen(
            stdout=(
                '{"type":"turn.started"}\n'
                '{"type":"exec_command_begin","cmd":"rg canonical"}\n'
            )
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    events: list[dict[str, Any]] = []

    result = CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
        event_callback=events.append,
    )

    command = captured["command"]
    assert command[:2] == ["/usr/local/bin/codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--json" in command
    assert "--ask-for-approval" not in command
    assert command[command.index("--cd") + 1] == str(project.resolve())
    assert "--model" not in command
    assert command[-1] == "-"
    assert captured["stdin"] == subprocess.PIPE
    assert captured["stdout"] == subprocess.PIPE
    assert captured["stderr"] == subprocess.PIPE
    assert "Current editor context is JSON" in captured["process"].stdin.text
    assert "Improve this" in captured["process"].stdin.text
    assert result.text == "Looks better with a shorter front."
    assert result.html == "<p>Looks <strong>better</strong> with a shorter front.</p>"
    assert result.proposals[0].field_updates == {"Front": "new front"}
    assert result.event_count == 2
    assert [event["type"] for event in events] == [
        "turn.started",
        "exec_command_begin",
    ]


def test_codex_agent_passes_optional_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = CodexCliAgent(
        codex_path="codex",
        model="gpt-5-codex",
        timeout_seconds=300,
    ).send(
        prompt="Explain",
        snapshot=snapshot(),
        project_root="",
        history=[("Earlier", "Earlier answer")],
    )

    command = captured["command"]
    assert command[command.index("--model") + 1] == "gpt-5-codex"
    assert result.text == "No changes."
    assert result.html == "<p>No changes.</p>"
    assert result.proposals == ()
    assert result.event_count == 1


def test_codex_agent_surfaces_login_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        return FakePopen(stderr="not logged in", returncode=1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="codex login"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
        )


def test_codex_agent_surfaces_schema_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        return FakePopen(
            stderr=(
                'ERROR: {"type":"error","error":{"code":"invalid_json_schema",'
                '"message":"required is required"}}'
            ),
            returncode=1,
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="rejected the response schema"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
        )


def test_codex_agent_streams_malformed_json_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        return FakePopen(stdout='not json\n{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    events: list[dict[str, Any]] = []

    result = CodexCliAgent(
        codex_path="codex",
        model="",
        timeout_seconds=300,
    ).send(
        prompt="Explain",
        snapshot=snapshot(),
        project_root="",
        history=[],
        event_callback=events.append,
    )

    assert result.event_count == 2
    assert events[0] == {"type": "malformed_json", "line": "not json"}
    assert events[1] == {"type": "turn.completed"}


def test_codex_agent_kills_timed_out_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        process = FakePopen(returncode=None)
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="timed out"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=0,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
        )

    assert captured["process"].killed


def test_codex_activity_renderer_compacts_verbose_activity() -> None:
    renderer = CodexActivityRenderer()

    live_lines = [
        renderer.record({"type": "turn.started"}),
        renderer.record({"type": "exec_command_begin", "cmd": "rg canonical"}),
        renderer.record({"type": "exec_command_output", "output": "line 1\nline 2"}),
        renderer.record({"type": "reasoning", "summary": "Checking the source."}),
        renderer.record({"type": "unexpected.future.event"}),
    ]

    assert live_lines == [
        "[status] turn started",
        "[tool] rg canonical",
        "[output] line 1 line 2",
        "[reasoning] Checking the source.",
        "[event] unexpected.future.event",
    ]
    assert renderer.compact_summary() == (
        "[Codex activity: 5 stream events, 1 tool call, 1 output chunk, "
        "1 reasoning update, 1 other event type]\n"
    )


def test_compact_activity_transcript_replaces_live_tail() -> None:
    transcript = "You: Explain\nAssistant: [Live Codex activity]\n[tool] rg canonical\n"
    start = transcript.index("[Live Codex activity]")

    assert compact_activity_transcript(
        transcript, start, "[Codex activity: done]\n"
    ) == ("You: Explain\nAssistant: [Codex activity: done]\n")


def test_codex_cli_json_stream_smoke(tmp_path: Path) -> None:
    if os.environ.get("ANKI_CODEX_CLI_INTEGRATION") != "1":
        pytest.skip("set ANKI_CODEX_CLI_INTEGRATION=1 to run real Codex CLI smoke")

    repo_status_before = _git_status()
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.md"
    source.write_text(
        "A canonical divisor is attached to top forms.\n", encoding="utf-8"
    )
    events: list[dict[str, Any]] = []

    result = CodexCliAgent(
        codex_path=os.environ.get("ANKI_CODEX_CLI_PATH", ""),
        model="",
        timeout_seconds=120,
    ).send(
        prompt=(
            "Reply with one brief sentence about the source file. Do not propose "
            "note changes; use patch null. Include simple paragraph HTML in "
            "message_html."
        ),
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
        event_callback=events.append,
    )

    assert result.text
    assert result.html
    assert result.proposals == ()
    assert events
    assert source.read_text(encoding="utf-8") == (
        "A canonical divisor is attached to top forms.\n"
    )
    assert _git_status() == repo_status_before


def _git_status() -> str:
    if not (ROOT / ".git").exists():
        return ""
    return subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


def test_resolve_codex_path_prefers_configured_value() -> None:
    assert resolve_codex_path("/custom/codex") == "/custom/codex"
