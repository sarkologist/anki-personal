# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .patches import EditorSnapshot, NotePatch, validate_note_patch
from .sources import SourceAccessError, resolve_project_root

DEFAULT_CODEX_APP_PATH = "/Applications/Codex.app/Contents/Resources/codex"


CODEX_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "patch": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "note_id": {"type": ["integer", "null"]},
                        "notetype_id": {"type": "integer"},
                        "field_updates": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "html": {"type": "string"},
                                },
                                "required": ["name", "html"],
                                "additionalProperties": False,
                            },
                        },
                        "tags": {
                            "type": "object",
                            "properties": {
                                "replace": {
                                    "type": ["array", "null"],
                                    "items": {"type": "string"},
                                },
                                "add": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "remove": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                            },
                            "required": ["replace", "add", "remove"],
                            "additionalProperties": False,
                        },
                    },
                    "required": [
                        "summary",
                        "note_id",
                        "notetype_id",
                        "field_updates",
                        "tags",
                    ],
                    "additionalProperties": False,
                },
            ]
        },
    },
    "required": ["message", "patch"],
    "additionalProperties": False,
}


PROMPT_TEMPLATE = """You are helping from inside an Anki editor pane.

Current editor context is JSON:
{context_json}

Recent conversation:
{history}

User request:
{user_prompt}

You may inspect the selected project folder with read-only shell commands such
as rg, sed, awk, and cat. Do not modify files. Do not run network commands.

Return a JSON object matching the supplied schema:
- message: conversational answer for the user.
- patch: null unless you are proposing changes to the current note.

When proposing a patch:
- Target only the current note.
- Use exact field names from the editor context.
- Preserve HTML where appropriate.
- Include tags.replace, tags.add, and tags.remove. Use null for tags.replace
  unless you intend to replace the complete tag list.
- The patch will only be shown to the user; Anki applies it after approval.
"""


@dataclass(frozen=True)
class AgentResult:
    text: str
    proposals: tuple[NotePatch, ...]


class CodexCliAgent:
    def __init__(
        self,
        *,
        codex_path: str,
        model: str,
        timeout_seconds: int,
    ) -> None:
        self.codex_path = resolve_codex_path(codex_path)
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds

    def send(
        self,
        *,
        prompt: str,
        snapshot: EditorSnapshot,
        project_root: str,
        history: list[tuple[str, str]],
    ) -> AgentResult:
        with tempfile.TemporaryDirectory(prefix="anki-codex-agent-") as temp_dir:
            temp = Path(temp_dir)
            schema_path = temp / "response.schema.json"
            output_path = temp / "last-message.json"
            schema_path.write_text(
                json.dumps(CODEX_OUTPUT_SCHEMA), encoding="utf-8"
            )

            cwd = self._working_directory(project_root, temp)
            command = self._command(cwd, schema_path, output_path)
            completed = self._run(command, self._prompt(prompt, snapshot, history))

            if completed.returncode != 0:
                raise RuntimeError(_format_codex_error(completed))
            if not output_path.exists():
                raise RuntimeError("Codex did not write a final response.")

            raw = output_path.read_text(encoding="utf-8")
            data = _parse_json_object(raw)
            message = str(data.get("message") or "").strip()
            patch_data = data.get("patch")
            proposals: tuple[NotePatch, ...] = ()
            if patch_data is not None:
                proposals = (validate_note_patch(patch_data, snapshot),)
            return AgentResult(text=message, proposals=proposals)

    def _working_directory(self, project_root: str, fallback: Path) -> Path:
        if project_root.strip():
            return resolve_project_root(project_root)
        return fallback

    def _command(
        self,
        cwd: Path,
        schema_path: Path,
        output_path: Path,
    ) -> list[str]:
        command = [
            self.codex_path,
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "--cd",
            str(cwd),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        return command

    def _prompt(
        self,
        prompt: str,
        snapshot: EditorSnapshot,
        history: list[tuple[str, str]],
    ) -> str:
        recent_history = history[-6:]
        if recent_history:
            history_text = "\n".join(
                f"User: {user}\nAssistant: {assistant}"
                for user, assistant in recent_history
            )
        else:
            history_text = "(none)"
        return PROMPT_TEMPLATE.format(
            context_json=json.dumps(snapshot.as_tool_result(), ensure_ascii=False),
            history=history_text,
            user_prompt=prompt,
        )

    def _run(
        self,
        command: list[str],
        prompt: str,
    ) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Could not find Codex CLI. Set the Codex CLI path in the pane."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Codex CLI timed out.") from exc


def resolve_codex_path(configured_path: str) -> str:
    configured = configured_path.strip()
    if configured:
        return configured
    if os.path.exists(DEFAULT_CODEX_APP_PATH):
        return DEFAULT_CODEX_APP_PATH
    return "codex"


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise RuntimeError("Codex returned a non-JSON response.") from None
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise RuntimeError("Codex returned JSON that was not an object.")
    return parsed


def _format_codex_error(completed: subprocess.CompletedProcess[str]) -> str:
    stderr = completed.stderr.strip()
    stdout = completed.stdout.strip()
    details = stderr or stdout or "no output"
    if "invalid_json_schema" in details:
        if match := re.search(r'"message":\s*"([^"]+)"', details):
            return f"Codex CLI rejected the response schema: {match.group(1)}"
        return "Codex CLI rejected the response schema."
    if "not logged in" in details.lower() or "login" in details.lower():
        return "Codex CLI is not logged in. Run `codex login` and choose ChatGPT sign-in."
    return f"Codex CLI failed with exit code {completed.returncode}: {details[-1200:]}"


def project_root_status(project_root: str) -> str:
    if not project_root.strip():
        return "No project folder selected; Codex will use card context only."
    try:
        root = resolve_project_root(project_root)
    except SourceAccessError as exc:
        return str(exc)
    return f"Read-only project folder: {root}"
