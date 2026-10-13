# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from threading import Event
from typing import Any


class GenerationStopped(Exception):
    pass


def resolve_cli(provider: str, configured: str = "") -> str:
    if provider not in ("codex", "claude"):
        raise ValueError("Choose Codex or Claude")
    if configured.strip():
        path = Path(configured).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f"{provider} executable not found: {path}")
        return str(path)
    found = shutil.which(provider)
    if found:
        return found
    candidates = [
        Path.home() / ".local" / "bin" / provider,
        Path("/opt/homebrew/bin") / provider,
        Path("/usr/local/bin") / provider,
    ]
    if provider == "codex":
        candidates.append(Path("/Applications/Codex.app/Contents/Resources/codex"))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise ValueError(f"Install and sign in to the {provider} CLI first")


def subscription_environment() -> dict[str, str]:
    env = os.environ.copy()
    # Keep CLI OAuth/keychain login. Never silently charge a direct API account.
    for name in (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDECODE",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        env.pop(name, None)
    return env


def cli_command(
    provider: str,
    executable: str,
    folder: Path,
    schema: dict[str, Any],
    model: str = "",
) -> list[str]:
    if provider == "codex":
        command = [
            executable,
            "exec",
            "--ignore-user-config",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "-c",
            'approval_policy="never"',
            "-c",
            "features.shell_tool=false",
            "-c",
            "features.apps=false",
            "-c",
            "features.multi_agent=false",
            "-c",
            'web_search="disabled"',
            "--cd",
            str(folder),
            "--output-schema",
            str(folder / "schema.json"),
            "--output-last-message",
            str(folder / "answer.json"),
        ]
        if model.strip():
            command.extend(["--model", model.strip()])
        command.append("-")
    elif provider == "claude":
        command = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--disable-slash-commands",
            "--json-schema",
            json.dumps(schema),
        ]
        if model.strip():
            command.extend(["--model", model.strip()])
    else:
        raise ValueError("Unknown provider")
    return command


def _stop_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        os.killpg(process.pid, signal.SIGTERM)
    else:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait()


def parse_response(provider: str, stdout: str, answer: str = "") -> dict[str, Any]:
    if provider == "codex":
        value = json.loads(answer)
    else:
        envelope = json.loads(stdout)
        if not isinstance(envelope, dict) or envelope.get("is_error"):
            raise ValueError("Claude did not complete sentence generation")
        value = envelope.get("structured_output")
        if value is None and isinstance(envelope.get("result"), str):
            value = json.loads(envelope["result"])
    if not isinstance(value, dict):
        raise ValueError("Sentence generation did not return a JSON object")
    return value


class CliGenerator:
    def __init__(self, config: dict[str, Any], stop: Event) -> None:
        self.provider = str(config.get("provider", "codex"))
        self.executable = resolve_cli(
            self.provider, str(config.get(f"{self.provider}_path", ""))
        )
        self.model = str(config.get(f"{self.provider}_model", ""))
        self.timeout = max(10, min(900, int(config.get("timeout_seconds", 240))))
        self.stop = stop
        self.env = subscription_environment()

    def check_login(self) -> None:
        if self.provider == "codex":
            command = [self.executable, "login", "status"]
        else:
            command = [self.executable, "auth", "status"]
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=15,
            check=False,
        )
        output = result.stdout + result.stderr
        if result.returncode:
            raise ValueError(
                f"Sign in to {self.provider} CLI to enable automatic generation"
            )
        if self.provider == "codex":
            if "Logged in using ChatGPT" not in output:
                raise ValueError(
                    "Run codex login and choose ChatGPT subscription sign-in"
                )
        else:
            info = json.loads(result.stdout)
            if not info.get("loggedIn") or info.get("authMethod") not in (
                "claude.ai",
                "oauth_token",
            ):
                raise ValueError("Sign in to Claude Code with your Claude subscription")
            if info.get("apiProvider", "firstParty") != "firstParty":
                raise ValueError(
                    "Select first-party Claude subscription authentication"
                )

    def request(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        if self.stop.is_set():
            raise GenerationStopped()
        with tempfile.TemporaryDirectory(prefix="anki-chinese-frontier-") as temporary:
            folder = Path(temporary)
            (folder / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            command = cli_command(
                self.provider, self.executable, folder, schema, self.model
            )
            with (
                (folder / "stdout.txt").open("w+") as stdout,
                (folder / "stderr.txt").open("w+") as stderr,
            ):
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    encoding="utf-8",
                    cwd=folder,
                    env=self.env,
                    start_new_session=os.name == "posix",
                )
                deadline = time.monotonic() + self.timeout
                pending_input: str | None = prompt
                try:
                    while True:
                        if self.stop.is_set():
                            raise GenerationStopped()
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                f"{self.provider} generation timed out; it will retry later"
                            )
                        try:
                            process.communicate(input=pending_input, timeout=0.5)
                            break
                        except subprocess.TimeoutExpired:
                            pending_input = None
                    if process.returncode:
                        stderr.seek(0)
                        detail = stderr.read(2000).strip()
                        if not detail:
                            stdout.seek(0)
                            raw = stdout.read(10000)
                            try:
                                envelope = json.loads(raw)
                                detail = str(
                                    envelope.get("result", envelope.get("error", raw))
                                )[:2000]
                            except (ValueError, AttributeError):
                                detail = raw[:2000]
                        raise RuntimeError(
                            f"{self.provider} exited with {process.returncode}: {detail}"
                        )
                    stdout.seek(0)
                    output = stdout.read(2 * 1024 * 1024)
                finally:
                    _stop_process(process)
            answer_path = folder / "answer.json"
            answer = (
                answer_path.read_text(encoding="utf-8") if answer_path.exists() else ""
            )
            return parse_response(self.provider, output, answer)
