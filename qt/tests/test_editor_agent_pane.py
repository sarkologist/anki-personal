# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADDONS = ROOT / "addons"
if str(ADDONS) not in sys.path:
    sys.path.insert(0, str(ADDONS))

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
from editor_agent_pane.sources import (  # noqa: E402
    SourceAccessError,
    read_source_file,
    search_source_files,
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


def test_validate_note_patch_accepts_current_note_fields_and_tags() -> None:
    patch = validate_note_patch(
        {
            "summary": "Tighten wording",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [{"name": "Front", "html": "new front"}],
            "tags": {"add": ["agent"], "remove": ["remove-me"]},
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


def test_codex_agent_uses_read_only_cli_and_parses_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = input
        captured["timeout"] = timeout
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            """
            {
              "message": "Looks better with a shorter front.",
              "patch": {
                "summary": "Shorten front",
                "note_id": 123,
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "new front"}],
                "tags": {"add": ["agent"], "remove": []}
              }
            }
            """,
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
    )

    command = captured["command"]
    assert command[:2] == ["/usr/local/bin/codex", "exec"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[command.index("--ask-for-approval") + 1] == "never"
    assert command[command.index("--cd") + 1] == str(project.resolve())
    assert "--model" not in command
    assert command[-1] == "-"
    assert captured["timeout"] == 123
    assert "Current editor context is JSON" in captured["input"]
    assert "Improve this" in captured["input"]
    assert result.text == "Looks better with a shorter front."
    assert result.proposals[0].field_updates == {"Front": "new front"}


def test_codex_agent_passes_optional_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "patch": null}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

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
    assert result.proposals == ()


def test_codex_agent_surfaces_login_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 1, stdout="", stderr="not logged in"
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

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


def test_resolve_codex_path_prefers_configured_value() -> None:
    assert resolve_codex_path("/custom/codex") == "/custom/codex"
