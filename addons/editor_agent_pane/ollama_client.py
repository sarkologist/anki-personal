# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from queue import Empty, Queue
from threading import Thread
from typing import Any, Callable, TextIO

from .agent_log import AgentRunLogger, line_count, text_preview
from .codex_client import (
    PROMPT_TEMPLATE,
    AgentPatch,
    AgentResult,
    AgentStopped,
    _parse_json_object,
    _selection_instructions,
    _snapshot_log_payload,
    _validate_optional_agent_patch,
)
from .patches import EditorSnapshot, MultiCardSnapshot

DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"

OLLAMA_CONTEXT_INSTRUCTIONS = (
    "You are running as a local Ollama model. You do not have Codex tools, shell "
    "access, web access, or project folder access. Use only the current editor "
    "context JSON, recent conversation, selected text, and user request. Do not "
    "claim to have inspected files, run commands, or checked external sources."
)


@dataclass(frozen=True)
class OllamaModelInfo:
    name: str
    size: int | None = None
    digest: str = ""
    modified_at: str = ""
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class _OllamaProcessResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str


StopRequestedCallback = Callable[[], bool]


class OllamaCliAgent:
    def __init__(
        self,
        *,
        ollama_path: str,
        ollama_host: str,
        model: str,
        timeout_seconds: int,
        custom_instructions: str = "",
    ) -> None:
        self.ollama_path = resolve_ollama_path(ollama_path)
        self.ollama_host = normalize_ollama_host(ollama_host)
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds
        self.custom_instructions = custom_instructions.strip()

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
        del project_root
        del event_callback
        if not self.model:
            raise RuntimeError("Select an Ollama model first.")
        _log_agent_event(
            run_logger,
            "run_start",
            provider="ollama",
            model=self.model,
            timeout_seconds=self.timeout_seconds,
            **_snapshot_log_payload(snapshot),
            prompt_chars=len(prompt),
            history_count=len(history),
            history_user_chars=sum(len(user) for user, _assistant in history),
            history_assistant_chars=sum(len(assistant) for _user, assistant in history),
        )

        command = self._command()
        _log_agent_event(
            run_logger,
            "cli_launch",
            command=_ollama_command_log_payload(command),
        )
        try:
            completed = self._run(
                command,
                self._prompt(prompt, snapshot, history),
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
            raise RuntimeError(_format_ollama_error(completed))

        try:
            data = _parse_json_object(completed.stdout)
        except Exception as exc:
            _log_agent_event(
                run_logger,
                "run_failure",
                stage="final_json_parse",
                returncode=completed.returncode,
                stdout_lines=line_count(completed.stdout),
                stderr_lines=line_count(completed.stderr),
                output_chars=len(completed.stdout),
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

    def _command(self) -> list[str]:
        return [
            self.ollama_path,
            "run",
            self.model,
            "--format",
            "json",
            "--hidethinking",
            "--nowordwrap",
        ]

    def _prompt(
        self,
        prompt: str,
        snapshot: EditorSnapshot | MultiCardSnapshot,
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
            image_instructions=_ollama_image_instructions(snapshot),
            selection_instructions=_selection_instructions(snapshot),
            history=history_text,
            user_prompt=prompt,
            project_access_instructions=OLLAMA_CONTEXT_INSTRUCTIONS,
        )

    def _run(
        self,
        command: list[str],
        prompt: str,
        *,
        run_logger: AgentRunLogger | None,
        stop_requested: StopRequestedCallback | None,
    ) -> _OllamaProcessResult:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=_ollama_env(self.ollama_host),
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                "Could not find Ollama CLI. Set the Ollama CLI path in the pane."
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
                raise RuntimeError("Ollama CLI timed out.")

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
                return _OllamaProcessResult(
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
                raise AgentStopped("Ollama run stopped.")


def discover_ollama_models(
    *,
    ollama_path: str = "",
    ollama_host: str = "",
    timeout_seconds: float = 2.0,
) -> tuple[OllamaModelInfo, ...]:
    host = normalize_ollama_host(ollama_host)
    try:
        return _discover_ollama_models_from_api(host, timeout_seconds)
    except Exception as api_exc:
        try:
            return _discover_ollama_models_from_cli(
                resolve_ollama_path(ollama_path),
                host,
                timeout_seconds,
            )
        except Exception as cli_exc:
            raise RuntimeError(
                "Could not discover Ollama models. "
                f"API: {api_exc}; CLI: {cli_exc}"
            ) from cli_exc


def ollama_model_names(models: tuple[OllamaModelInfo, ...]) -> tuple[str, ...]:
    return tuple(model.name for model in models if model.name.strip())


def parse_ollama_list(output: str) -> tuple[OllamaModelInfo, ...]:
    models: list[OllamaModelInfo] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("NAME "):
            continue
        parts = line.split(maxsplit=3)
        if not parts:
            continue
        models.append(
            OllamaModelInfo(
                name=parts[0],
                digest=parts[1] if len(parts) > 1 else "",
            )
        )
    return tuple(models)


def normalize_ollama_host(ollama_host: str) -> str:
    host = str(ollama_host).strip() or DEFAULT_OLLAMA_HOST
    if "://" not in host:
        host = f"http://{host}"
    return host.rstrip("/")


def resolve_ollama_path(configured_path: str) -> str:
    return configured_path.strip() or "ollama"


def _discover_ollama_models_from_api(
    ollama_host: str,
    timeout_seconds: float,
) -> tuple[OllamaModelInfo, ...]:
    with urllib.request.urlopen(  # noqa: S310 - local user-configured Ollama host
        f"{ollama_host}/api/tags",
        timeout=timeout_seconds,
    ) as response:
        raw = response.read().decode("utf-8")
    parsed = json.loads(raw)
    raw_models = parsed.get("models") if isinstance(parsed, dict) else None
    if not isinstance(raw_models, list):
        raise RuntimeError("Ollama tags response did not include models.")
    models: list[OllamaModelInfo] = []
    for raw_model in raw_models:
        if not isinstance(raw_model, dict):
            continue
        name = str(raw_model.get("name") or raw_model.get("model") or "").strip()
        if not name:
            continue
        size = raw_model.get("size")
        models.append(
            OllamaModelInfo(
                name=name,
                size=size if type(size) is int else None,
                digest=str(raw_model.get("digest") or ""),
                modified_at=str(raw_model.get("modified_at") or ""),
                details=(
                    dict(raw_model["details"])
                    if isinstance(raw_model.get("details"), dict)
                    else None
                ),
            )
        )
    return tuple(models)


def _discover_ollama_models_from_cli(
    ollama_path: str,
    ollama_host: str,
    timeout_seconds: float,
) -> tuple[OllamaModelInfo, ...]:
    completed = subprocess.run(
        [ollama_path, "list"],
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        env=_ollama_env(ollama_host),
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(_format_ollama_discovery_error(completed))
    return parse_ollama_list(completed.stdout)


def _ollama_image_instructions(snapshot: EditorSnapshot | MultiCardSnapshot) -> str:
    if not snapshot.image_paths():
        return ""
    return (
        "context_json.images may list Anki media filenames referenced by the note. "
        "This local Ollama pane has not attached image files, so do not infer visual "
        "details that are not present in the text context."
    )


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


def _ollama_env(ollama_host: str) -> dict[str, str]:
    env = dict(os.environ)
    env["OLLAMA_HOST"] = normalize_ollama_host(ollama_host)
    return env


def _ollama_command_log_payload(command: list[str]) -> dict[str, Any]:
    return {
        "program": command[0] if command else "",
        "subcommand": command[1] if len(command) > 1 else "",
        "model": command[2] if len(command) > 2 else "",
        "json_format": "--format" in command and "json" in command,
    }


def _format_ollama_error(completed: _OllamaProcessResult) -> str:
    stderr = completed.stderr.strip()
    stdout = completed.stdout.strip()
    details = stderr or stdout or "no output"
    return f"Ollama CLI failed with exit code {completed.returncode}: {details[-1200:]}"


def _format_ollama_discovery_error(completed: subprocess.CompletedProcess[str]) -> str:
    details = completed.stderr.strip() or completed.stdout.strip() or "no output"
    return f"`ollama list` failed with exit code {completed.returncode}: {details}"


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
