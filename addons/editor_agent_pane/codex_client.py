# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable, TextIO

from .agent_log import (
    AgentRunLogger,
    command_log_payload,
    line_count,
    stream_event_log_payload,
    text_preview,
)
from .patches import EditorSnapshot, NotePatch, validate_note_patch
from .sources import SourceAccessError, resolve_project_root

DEFAULT_CODEX_APP_PATH = "/Applications/Codex.app/Contents/Resources/codex"
CODEX_HOME_ENV = "CODEX_HOME"
CODEX_AUTH_FILENAME = "auth.json"
PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE = "workspace-write"
PROJECT_FOLDER_ACCESS_READ_ONLY = "read-only"
DEFAULT_PROJECT_FOLDER_ACCESS = PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE
PROJECT_FOLDER_ACCESS_MODES = (
    PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE,
    PROJECT_FOLDER_ACCESS_READ_ONLY,
)
MODELS_WITHOUT_REASONING_SUMMARIES = frozenset({"gpt-5.3-codex-spark"})

PROJECT_ACCESS_INSTRUCTIONS = {
    PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE: (
        "If a project folder is selected, you may inspect and edit files in it. "
        "Keep file changes inside that project folder. Do not run network commands."
    ),
    PROJECT_FOLDER_ACCESS_READ_ONLY: (
        "You may inspect the selected project folder with read-only shell commands "
        "such as rg, sed, awk, and cat. Do not modify files. Do not run network "
        "commands."
    ),
}


CODEX_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "message": {"type": "string"},
        "message_html": {"type": "string"},
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
    "required": ["message", "message_html", "patch"],
    "additionalProperties": False,
}


PROMPT_TEMPLATE = """You are helping from inside an Anki editor pane.

User-customized instructions:
{custom_instructions}

Current editor context is JSON:
{context_json}

If context_json.images is non-empty, the referenced note images have been
attached to this initial prompt.
context_json.images[n] corresponds to attached image number n + 1. Each entry
records the Anki media filename plus the note fields that reference that image.

Recent conversation:
{history}

User request:
{user_prompt}

{project_access_instructions}

Return a JSON object matching the supplied schema:
- message: conversational answer for the user as plain text.
- message_html: the same answer as sanitized-subset HTML for rendering in
  Anki. Use ordinary semantic tags like p, ul, ol, li, strong, em, code, pre,
  blockquote, table, and a. Use MathJax delimiters like \\(...\\) or \\[...\\]
  for math. Do not include scripts, styles, iframes, event handlers, images, or
  javascript: links.
- patch: null unless you are proposing changes to the current note.

Do not include hidden chain-of-thought or private scratchpad reasoning. It is
fine to include a concise rationale or evidence summary in message and
message_html, especially when proposing a patch.

When proposing a patch:
- Target only the current note.
- Use exact field names from the editor context.
- Preserve HTML where appropriate.
- Explain briefly in message/message_html why the proposed change improves the
  note.
- Include tags.replace, tags.add, and tags.remove. Use null for tags.replace
  unless you intend to replace the complete tag list.
- The patch will only be shown to the user; Anki applies it after approval.
"""


@dataclass(frozen=True)
class AgentResult:
    text: str
    html: str
    proposals: tuple[NotePatch, ...]
    event_count: int = 0


@dataclass(frozen=True)
class _CodexProcessResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str
    event_count: int


CodexEventCallback = Callable[[dict[str, Any]], None]
StopRequestedCallback = Callable[[], bool]


class AgentStopped(RuntimeError):
    pass


class CodexCliAgent:
    def __init__(
        self,
        *,
        codex_path: str,
        model: str,
        timeout_seconds: int,
        project_folder_access: str = DEFAULT_PROJECT_FOLDER_ACCESS,
        custom_instructions: str = "",
        stream_reasoning_summaries: bool = True,
    ) -> None:
        self.codex_path = resolve_codex_path(codex_path)
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.project_folder_access = normalize_project_folder_access(
            project_folder_access
        )
        self.custom_instructions = custom_instructions.strip()
        self.stream_reasoning_summaries = stream_reasoning_summaries

    def send(
        self,
        *,
        prompt: str,
        snapshot: EditorSnapshot,
        project_root: str,
        history: list[tuple[str, str]],
        event_callback: CodexEventCallback | None = None,
        run_logger: AgentRunLogger | None = None,
        stop_requested: StopRequestedCallback | None = None,
    ) -> AgentResult:
        _log_agent_event(
            run_logger,
            "run_start",
            model=self.model or "default",
            sandbox=self.project_folder_access,
            timeout_seconds=self.timeout_seconds,
            note_id=snapshot.note_id,
            notetype_id=snapshot.notetype_id,
            field_count=len(snapshot.fields),
            tag_count=len(snapshot.tags),
            image_count=len(snapshot.images),
            prompt_chars=len(prompt),
            history_count=len(history),
            history_user_chars=sum(len(user) for user, _assistant in history),
            history_assistant_chars=sum(len(assistant) for _user, assistant in history),
        )
        with tempfile.TemporaryDirectory(prefix="anki-codex-agent-") as temp_dir:
            temp = Path(temp_dir)
            codex_home = prepare_isolated_codex_home(temp / "codex-home")
            schema_path = temp / "response.schema.json"
            output_path = temp / "last-message.json"
            schema_path.write_text(
                json.dumps(CODEX_OUTPUT_SCHEMA), encoding="utf-8"
            )

            try:
                cwd = self._working_directory(project_root, temp)
            except Exception as exc:
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="working_directory",
                    error_type=type(exc).__name__,
                    error_preview=text_preview(str(exc)),
                )
                raise
            command = self._command(
                cwd,
                schema_path,
                output_path,
                snapshot.image_paths(),
            )
            _log_agent_event(
                run_logger,
                "cli_launch",
                temp_dir=temp_dir,
                cwd=str(cwd),
                command=command_log_payload(command),
            )
            try:
                completed = self._run(
                    command,
                    self._prompt(prompt, snapshot, history),
                    env=_codex_subprocess_env(codex_home),
                    event_callback=event_callback,
                    run_logger=run_logger,
                    stop_requested=stop_requested,
                )
            except AgentStopped:
                _log_agent_event(run_logger, "run_stopped")
                raise
            except Exception as exc:
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="run",
                    error_type=type(exc).__name__,
                    error_preview=text_preview(str(exc)),
                )
                raise

            if completed.returncode != 0:
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="cli_exit",
                    returncode=completed.returncode,
                    event_count=completed.event_count,
                    stdout_lines=line_count(completed.stdout),
                    stderr_lines=line_count(completed.stderr),
                    stderr_preview=text_preview(completed.stderr),
                )
                raise RuntimeError(_format_codex_error(completed))
            if not output_path.exists():
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="missing_final_response",
                    returncode=completed.returncode,
                    event_count=completed.event_count,
                    stdout_lines=line_count(completed.stdout),
                    stderr_lines=line_count(completed.stderr),
                )
                raise RuntimeError("Codex did not write a final response.")

            raw = output_path.read_text(encoding="utf-8")
            try:
                data = _parse_json_object(raw)
            except Exception as exc:
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="final_json_parse",
                    returncode=completed.returncode,
                    event_count=completed.event_count,
                    output_chars=len(raw),
                    error_type=type(exc).__name__,
                    error_preview=text_preview(str(exc)),
                )
                raise
            message = str(data.get("message") or "").strip()
            message_html = str(data.get("message_html") or "").strip()
            patch_data = data.get("patch")
            proposals: tuple[NotePatch, ...] = ()
            if patch_data is not None:
                try:
                    proposals = (validate_note_patch(patch_data, snapshot),)
                except Exception as exc:
                    _log_agent_event(
                        run_logger,
                        "run_failure",
                        stage="patch_validation",
                        returncode=completed.returncode,
                        event_count=completed.event_count,
                        error_type=type(exc).__name__,
                        error_preview=text_preview(str(exc)),
                    )
                    raise
            _log_agent_event(
                run_logger,
                "run_finish",
                returncode=completed.returncode,
                event_count=completed.event_count,
                stdout_lines=line_count(completed.stdout),
                stderr_lines=line_count(completed.stderr),
                final_response_present=True,
                message_chars=len(message),
                message_html_chars=len(message_html),
                proposal_count=len(proposals),
            )
            return AgentResult(
                text=message,
                html=message_html,
                proposals=proposals,
                event_count=completed.event_count,
            )

    def _working_directory(self, project_root: str, fallback: Path) -> Path:
        if project_root.strip():
            return resolve_project_root(project_root)
        return fallback

    def _command(
        self,
        cwd: Path,
        schema_path: Path,
        output_path: Path,
        image_paths: tuple[str, ...],
    ) -> list[str]:
        command = [
            self.codex_path,
            "exec",
            "--sandbox",
            self.project_folder_access,
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-c",
            _model_reasoning_summary_config(
                self.model,
                self.stream_reasoning_summaries,
            ),
            "--json",
            "--cd",
            str(cwd),
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        for path in image_paths:
            command.extend(["--image", path])
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
            custom_instructions=self.custom_instructions or "(none)",
            context_json=json.dumps(snapshot.as_tool_result(), ensure_ascii=False),
            history=history_text,
            user_prompt=prompt,
            project_access_instructions=PROJECT_ACCESS_INSTRUCTIONS[
                self.project_folder_access
            ],
        )

    def _run(
        self,
        command: list[str],
        prompt: str,
        *,
        env: dict[str, str],
        event_callback: CodexEventCallback | None,
        run_logger: AgentRunLogger | None,
        stop_requested: StopRequestedCallback | None,
    ) -> _CodexProcessResult:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Could not find Codex CLI. Set the Codex CLI path in the pane."
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        lines: Queue[tuple[str, str | None]] = Queue()
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        event_count = 0
        stdout_done = False
        stderr_done = False

        stdout_reader = _start_reader(process.stdout, "stdout", lines)
        stderr_reader = _start_reader(process.stderr, "stderr", lines)

        try:
            if process.stdin is not None:
                process.stdin.write(prompt)
                process.stdin.close()
        except BrokenPipeError:
            pass

        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _log_agent_event(run_logger, "timeout_kill")
                process.kill()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                stdout_reader.join(timeout=1)
                stderr_reader.join(timeout=1)
                raise RuntimeError("Codex CLI timed out.")

            try:
                stream_name, line = lines.get(timeout=min(0.05, remaining))
            except Empty:
                stream_name = ""
                line = ""

            if stream_name == "stdout":
                if line is None:
                    stdout_done = True
                else:
                    stdout_chunks.append(line)
                    if event := _parse_stream_event(line):
                        event_count += 1
                        _log_agent_event(
                            run_logger,
                            "stream_event",
                            **stream_event_log_payload(event),
                        )
                        if event_callback is not None:
                            event_callback(event)
            elif stream_name == "stderr":
                if line is None:
                    stderr_done = True
                else:
                    stderr_chunks.append(line)

            returncode = process.poll()
            if returncode is not None and stdout_done and stderr_done:
                stdout_reader.join(timeout=1)
                stderr_reader.join(timeout=1)
                return _CodexProcessResult(
                    command=command,
                    returncode=returncode,
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks),
                    event_count=event_count,
                )

            if stop_requested is not None and stop_requested():
                _log_agent_event(run_logger, "stop_terminate")
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    _log_agent_event(run_logger, "stop_kill")
                    process.kill()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                stdout_reader.join(timeout=1)
                stderr_reader.join(timeout=1)
                raise AgentStopped("Codex run stopped.")


def _log_agent_event(
    run_logger: AgentRunLogger | None,
    event: str,
    **payload: Any,
) -> None:
    if run_logger is None:
        return
    try:
        run_logger.record(event, **payload)
    except Exception:
        pass


def resolve_codex_path(configured_path: str) -> str:
    configured = configured_path.strip()
    if configured:
        return configured
    if os.path.exists(DEFAULT_CODEX_APP_PATH):
        return DEFAULT_CODEX_APP_PATH
    return "codex"


def normalize_project_folder_access(project_folder_access: str) -> str:
    access = project_folder_access.strip()
    if access in PROJECT_FOLDER_ACCESS_MODES:
        return access
    return DEFAULT_PROJECT_FOLDER_ACCESS


def prepare_isolated_codex_home(target: Path) -> Path:
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    source_auth = _source_codex_home() / CODEX_AUTH_FILENAME
    if source_auth.exists():
        copied_auth = target / CODEX_AUTH_FILENAME
        shutil.copy2(source_auth, copied_auth)
        copied_auth.chmod(0o600)
    return target


def _source_codex_home() -> Path:
    if configured := os.environ.get(CODEX_HOME_ENV):
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def _codex_subprocess_env(codex_home: Path) -> dict[str, str]:
    env = os.environ.copy()
    env[CODEX_HOME_ENV] = str(codex_home)
    return env


def _model_reasoning_summary_config(
    model: str,
    stream_reasoning_summaries: bool,
) -> str:
    summary_mode = (
        "concise"
        if stream_reasoning_summaries and model not in MODELS_WITHOUT_REASONING_SUMMARIES
        else "none"
    )
    return f'model_reasoning_summary="{summary_mode}"'


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


def _start_reader(
    stream: TextIO,
    stream_name: str,
    lines: Queue[tuple[str, str | None]],
) -> Thread:
    def read_lines() -> None:
        try:
            for line in stream:
                lines.put((stream_name, line))
        finally:
            lines.put((stream_name, None))

    thread = Thread(target=read_lines, daemon=True)
    thread.start()
    return thread


def _parse_stream_event(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return {"type": "malformed_json", "line": line}
    if isinstance(event, dict):
        return event
    return {"type": "unknown_json", "value": event}


def _format_codex_error(completed: _CodexProcessResult) -> str:
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


def project_root_status(
    project_root: str,
    project_folder_access: str = DEFAULT_PROJECT_FOLDER_ACCESS,
) -> str:
    if not project_root.strip():
        return "No project folder selected; Codex will use card context only."
    try:
        root = resolve_project_root(project_root)
    except SourceAccessError as exc:
        return str(exc)
    access = normalize_project_folder_access(project_folder_access)
    if access == PROJECT_FOLDER_ACCESS_READ_ONLY:
        return f"Read-only project folder: {root}"
    return f"Writable project folder: {root}"
