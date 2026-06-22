# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable

from .agent_log import AgentRunLogger, line_count, text_preview
from .codex_client import (
    CODEX_OUTPUT_SCHEMA,
    PROJECT_FOLDER_ACCESS_READ_ONLY,
    PROMPT_TEMPLATE,
    AgentPatch,
    AgentResult,
    AgentStopped,
    _log_agent_event,
    _parse_json_object,
    _selection_instructions,
    _snapshot_log_payload,
    _start_reader,
    _validate_optional_agent_patch,
    normalize_project_folder_access,
)
from .patches import EditorSnapshot, MultiCardSnapshot
from .sources import resolve_project_root

DEFAULT_CLAUDE_CLI = "claude"
CLAUDE_CLI_CANDIDATES = (
    os.path.expanduser("~/.local/bin/claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
)

# The claude CLI accepts these reasoning effort levels. Anything else (including
# Codex's "none" and the empty default) is dropped so we never pass an invalid
# value to `--effort`.
CLAUDE_EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})

# `--permission-mode acceptEdits` keeps headless (`-p`) runs non-interactive so
# read/grep/edit tools run without blocking on an approval prompt.
PERMISSION_MODE_ACCEPT_EDITS = "acceptEdits"

# Read-only project access restricts Claude to a read-only tool allow-list.
# Unlike Codex's OS-level sandbox this is tool-restriction based, but allow-listing
# (rather than disallowing a few edit tools) means no shell, file-mutation, or
# network tools can run at all.
CLAUDE_READ_ONLY_ALLOWED_TOOLS = ("Read", "Grep", "Glob")

# Claude shares Codex's response contract (message / message_html / patch).
CLAUDE_OUTPUT_SCHEMA = CODEX_OUTPUT_SCHEMA

CLAUDE_PROJECT_ACCESS_INSTRUCTIONS = {
    "workspace-write": (
        "A project folder is selected. You may inspect and edit files in it with "
        "your tools. Keep file changes inside that project folder. Do not run "
        "network commands."
    ),
    PROJECT_FOLDER_ACCESS_READ_ONLY: (
        "A project folder is selected. You may inspect it with read-only tools "
        "such as Read, Grep, and Glob. Do not modify files. Do not run network "
        "commands."
    ),
}

CLAUDE_CONTEXT_ONLY_INSTRUCTIONS = (
    "No project folder is selected and tools are disabled. Use only the current "
    "editor context JSON, recent conversation, selected text, and user request. "
    "Do not claim to have inspected files, run commands, or checked external "
    "sources."
)


StopRequestedCallback = Callable[[], bool]


@dataclass(frozen=True)
class _ClaudeProcessResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


class ClaudeCliAgent:
    def __init__(
        self,
        *,
        claude_path: str,
        model: str,
        timeout_seconds: int,
        project_folder_access: str = "workspace-write",
        custom_instructions: str = "",
        reasoning_effort: str = "",
    ) -> None:
        self.claude_path = resolve_claude_path(claude_path)
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.project_folder_access = normalize_project_folder_access(
            project_folder_access
        )
        self.custom_instructions = custom_instructions.strip()
        self.reasoning_effort = claude_effort_value(reasoning_effort)

    def send(
        self,
        *,
        prompt: str,
        snapshot: EditorSnapshot | MultiCardSnapshot,
        project_root: str,
        history: list[tuple[str, str]],
        event_callback: Callable[[dict[str, Any]], None] | None = None,
        run_logger: AgentRunLogger | None = None,
        stop_requested: StopRequestedCallback | None = None,
    ) -> AgentResult:
        del event_callback  # Claude runs are not streamed into the activity view.
        _log_agent_event(
            run_logger,
            "run_start",
            provider="claude",
            model=self.model or "default",
            reasoning_effort=self.reasoning_effort or "default",
            sandbox=self.project_folder_access,
            timeout_seconds=self.timeout_seconds,
            **_snapshot_log_payload(snapshot),
            prompt_chars=len(prompt),
            history_count=len(history),
            history_user_chars=sum(len(user) for user, _assistant in history),
            history_assistant_chars=sum(len(assistant) for _user, assistant in history),
        )
        with tempfile.TemporaryDirectory(prefix="anki-claude-agent-") as temp_dir:
            temp = Path(temp_dir)
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
            command = self._command(cwd, project_root)
            _log_agent_event(
                run_logger,
                "cli_launch",
                temp_dir=temp_dir,
                cwd=str(cwd),
                command=_claude_command_log_payload(command),
            )
            try:
                completed = self._run(
                    command,
                    self._prompt(prompt, snapshot, history, project_root),
                    cwd=cwd,
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
                    stdout_lines=line_count(completed.stdout),
                    stderr_lines=line_count(completed.stderr),
                    stderr_preview=text_preview(completed.stderr),
                )
                raise RuntimeError(_format_claude_process_error(completed))

            try:
                envelope = _parse_claude_envelope(completed.stdout)
            except Exception as exc:
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="envelope_parse",
                    returncode=completed.returncode,
                    output_chars=len(completed.stdout),
                    error_type=type(exc).__name__,
                    error_preview=text_preview(str(exc)),
                )
                raise

            try:
                _raise_for_envelope_error(envelope)
            except Exception as exc:
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="api_error",
                    returncode=completed.returncode,
                    error_type=type(exc).__name__,
                    error_preview=text_preview(str(exc)),
                )
                raise

            try:
                data = _claude_structured_data(envelope)
            except Exception as exc:
                _log_agent_event(
                    run_logger,
                    "run_failure",
                    stage="final_json_parse",
                    returncode=completed.returncode,
                    error_type=type(exc).__name__,
                    error_preview=text_preview(str(exc)),
                )
                raise

            message = str(data.get("message") or "").strip()
            message_html = str(data.get("message_html") or "").strip()
            patch_data = data.get("patch")
            proposals: tuple[AgentPatch, ...] = ()
            if patch_data is not None:
                try:
                    patch = _validate_optional_agent_patch(patch_data, snapshot)
                    proposals = (patch,) if patch is not None else ()
                except Exception as exc:
                    _log_agent_event(
                        run_logger,
                        "run_failure",
                        stage="patch_validation",
                        returncode=completed.returncode,
                        error_type=type(exc).__name__,
                        error_preview=text_preview(str(exc)),
                    )
                    raise

            _log_agent_event(
                run_logger,
                "run_finish",
                returncode=completed.returncode,
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
                event_count=0,
            )

    def _working_directory(self, project_root: str, fallback: Path) -> Path:
        if project_root.strip():
            return resolve_project_root(project_root)
        return fallback

    def _command(self, cwd: Path, project_root: str) -> list[str]:
        command = [
            self.claude_path,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--json-schema",
            json.dumps(CLAUDE_OUTPUT_SCHEMA),
        ]
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(["--effort", self.reasoning_effort])
        if project_root.strip():
            command.extend(["--add-dir", str(cwd)])
            if self.project_folder_access == PROJECT_FOLDER_ACCESS_READ_ONLY:
                # Allow-list read-only tools only: no edits, shell, or network.
                command.extend(
                    ["--allowedTools", " ".join(CLAUDE_READ_ONLY_ALLOWED_TOOLS)]
                )
            else:
                command.extend(["--permission-mode", PERMISSION_MODE_ACCEPT_EDITS])
        else:
            command.extend(["--tools", ""])
        return command

    def _prompt(
        self,
        prompt: str,
        snapshot: EditorSnapshot | MultiCardSnapshot,
        history: list[tuple[str, str]],
        project_root: str,
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
            image_instructions=_claude_image_instructions(snapshot),
            selection_instructions=_selection_instructions(snapshot),
            history=history_text,
            user_prompt=prompt,
            project_access_instructions=self._project_access_instructions(project_root),
        )

    def _project_access_instructions(self, project_root: str) -> str:
        if not project_root.strip():
            return CLAUDE_CONTEXT_ONLY_INSTRUCTIONS
        return CLAUDE_PROJECT_ACCESS_INSTRUCTIONS[self.project_folder_access]

    def _run(
        self,
        command: list[str],
        prompt: str,
        *,
        cwd: Path,
        run_logger: AgentRunLogger | None,
        stop_requested: StopRequestedCallback | None,
    ) -> _ClaudeProcessResult:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(cwd),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Could not find Claude CLI. Set the Claude CLI path in the pane."
            ) from exc

        assert process.stdout is not None
        assert process.stderr is not None
        lines: Queue[tuple[str, str | None]] = Queue()
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
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
                raise RuntimeError("Claude CLI timed out.")

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
            elif stream_name == "stderr":
                if line is None:
                    stderr_done = True
                else:
                    stderr_chunks.append(line)

            returncode = process.poll()
            if returncode is not None and stdout_done and stderr_done:
                stdout_reader.join(timeout=1)
                stderr_reader.join(timeout=1)
                return _ClaudeProcessResult(
                    command=command,
                    returncode=returncode,
                    stdout="".join(stdout_chunks),
                    stderr="".join(stderr_chunks),
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
                raise AgentStopped("Claude run stopped.")


def resolve_claude_path(configured_path: str) -> str:
    configured = configured_path.strip()
    if configured:
        return configured
    if path := shutil.which("claude"):
        return path
    for path in CLAUDE_CLI_CANDIDATES:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    return DEFAULT_CLAUDE_CLI


def claude_effort_value(effort: object) -> str:
    value = str(effort).strip()
    return value if value in CLAUDE_EFFORT_LEVELS else ""


def _parse_claude_envelope(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise RuntimeError("Claude CLI returned no output.")
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Claude CLI returned a non-JSON response.") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Claude CLI returned JSON that was not an object.")
    return parsed


def _raise_for_envelope_error(envelope: dict[str, Any]) -> None:
    is_error = bool(envelope.get("is_error"))
    subtype = str(envelope.get("subtype") or "")
    if is_error or subtype.startswith("error"):
        result_text = str(envelope.get("result") or "")
        raise RuntimeError(_format_claude_api_error(result_text, envelope))


def _claude_structured_data(envelope: dict[str, Any]) -> dict[str, Any]:
    # With --json-schema the validated object comes back already parsed in
    # `structured_output`; the `result` field only carries a short human note
    # (e.g. "Done."). Fall back to parsing `result` as JSON when there is no
    # structured payload (no schema honored / older CLI).
    structured = envelope.get("structured_output")
    if structured is None:
        structured = envelope.get("structuredOutput")
    if isinstance(structured, dict):
        return structured
    result_text = str(envelope.get("result") or "")
    if not result_text.strip():
        raise RuntimeError("Claude did not return a structured response.")
    return _parse_json_object(result_text)


def _format_claude_api_error(result_text: str, envelope: dict[str, Any]) -> str:
    details = result_text.strip()
    lowered = details.lower()
    status = envelope.get("api_error_status")
    if (
        status == 401
        or "authentication_error" in lowered
        or "failed to authenticate" in lowered
        or "invalid authentication" in lowered
    ):
        return (
            "Claude CLI is not logged in or its credentials are invalid. Run "
            "`claude` once to sign in (or `claude setup-token`), then try again."
        )
    subtype = str(envelope.get("subtype") or "")
    if subtype == "error_max_turns":
        return "Claude stopped after reaching the maximum number of turns."
    if details:
        return f"Claude CLI reported an error: {details[-1200:]}"
    if subtype:
        return f"Claude CLI failed: {subtype}"
    return "Claude CLI failed with an unknown error."


def _format_claude_process_error(completed: _ClaudeProcessResult) -> str:
    stderr = completed.stderr.strip()
    stdout = completed.stdout.strip()
    details = stderr or stdout or "no output"
    lowered = details.lower()
    if "not logged in" in lowered or "authentication" in lowered:
        return (
            "Claude CLI is not logged in. Run `claude` once to sign in "
            "(or `claude setup-token`), then try again."
        )
    return f"Claude CLI failed with exit code {completed.returncode}: {details[-1200:]}"


def _claude_image_instructions(snapshot: EditorSnapshot | MultiCardSnapshot) -> str:
    if not snapshot.image_paths():
        return ""
    return (
        "context_json.images may list Anki media filenames referenced by the note. "
        "The pane has not attached the image files, so do not infer visual details "
        "that are not present in the text context."
    )


def _claude_command_log_payload(command: list[str]) -> dict[str, Any]:
    return {
        "program": command[0] if command else "",
        "model": command[command.index("--model") + 1] if "--model" in command else "",
        "effort": (
            command[command.index("--effort") + 1] if "--effort" in command else ""
        ),
        "permission_mode": (
            command[command.index("--permission-mode") + 1]
            if "--permission-mode" in command
            else ""
        ),
        "tools_disabled": "--tools" in command,
        "read_only": "--allowedTools" in command,
    }
