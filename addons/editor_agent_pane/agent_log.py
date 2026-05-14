# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MAX_PREVIEW_CHARS = 500


class AgentRunLogger(Protocol):
    def record(self, event: str, **payload: Any) -> None:
        ...


class InfoLogger(Protocol):
    def info(self, message: str) -> None:
        ...


@dataclass
class JsonLineAgentRunLogger:
    logger: InfoLogger
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def record(self, event: str, **payload: Any) -> None:
        entry = {
            "run_id": self.run_id,
            "ts": time.time(),
            "event": event,
            **_json_safe(payload),
        }
        self.logger.info(json.dumps(entry, ensure_ascii=False, sort_keys=True))


def text_preview(text: str, max_chars: int = MAX_PREVIEW_CHARS) -> str:
    preview = " ".join(text.split())
    if len(preview) <= max_chars:
        return preview
    return preview[: max_chars - 3] + "..."


def line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def ensure_agent_log_folder(addon_manager: Any, addon: str) -> Path:
    path = Path(addon_manager.logs_folder(addon))
    path.mkdir(parents=True, exist_ok=True)
    return path


def command_log_payload(command: list[str]) -> dict[str, Any]:
    images = _flag_values(command, "--image")
    payload: dict[str, Any] = {
        "program": Path(command[0]).name if command else "",
        "subcommand": command[1] if len(command) > 1 else "",
        "sandbox": _flag_value(command, "--sandbox"),
        "cwd": _flag_value(command, "--cd"),
        "json": "--json" in command,
        "has_model": "--model" in command,
        "model": _flag_value(command, "--model"),
        "image_count": len(images),
        "image_basenames": [Path(path).name for path in images],
        "output_schema_basename": Path(
            _flag_value(command, "--output-schema") or ""
        ).name,
        "output_last_message_basename": Path(
            _flag_value(command, "--output-last-message") or ""
        ).name,
    }
    return payload


def stream_event_log_payload(event: dict[str, Any]) -> dict[str, Any]:
    event_type, payload = _event_view(event)
    event_type_lower = event_type.lower()
    log_payload: dict[str, Any] = {"type": event_type}

    if event_type == "malformed_json":
        log_payload["line_preview"] = text_preview(str(event.get("line", "")))
        return log_payload

    if "error" in event_type_lower:
        log_payload["error_preview"] = text_preview(
            _first_text(event) or _first_text(payload) or event_type
        )
        return log_payload

    if _looks_like_output(event_type_lower, payload):
        log_payload["output_preview"] = text_preview(
            _first_text(payload) or _first_text(event) or event_type
        )
        return log_payload

    if _looks_like_command(event_type_lower, payload):
        log_payload["tool_preview"] = text_preview(
            _command_text(payload) or _first_text(event) or event_type
        )
        return log_payload

    if "reasoning" in event_type_lower:
        summary = _summary_text(event) or _summary_text(payload)
        log_payload["has_reasoning_summary"] = bool(summary)
        if summary:
            log_payload["reasoning_summary_preview"] = text_preview(summary)
        return log_payload

    if "message" in event_type_lower or "response" in event_type_lower:
        text = _first_text(event) or _first_text(payload)
        log_payload["message_chars"] = len(text)
        return log_payload

    return log_payload


def _flag_value(command: list[str], flag: str) -> str:
    try:
        index = command.index(flag)
    except ValueError:
        return ""
    if index + 1 >= len(command):
        return ""
    return command[index + 1]


def _flag_values(command: list[str], flag: str) -> list[str]:
    values: list[str] = []
    for index, part in enumerate(command[:-1]):
        if part == flag:
            values.append(command[index + 1])
    return values


def _event_view(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    event_type = event.get("type")
    payload = _payload(event)
    payload_type = payload.get("type") if payload is not event else None
    if (
        isinstance(payload_type, str)
        and payload_type
        and event_type
        in ("event_msg", "response_item", "item.completed", "item_completed")
    ):
        return payload_type, payload
    if isinstance(event_type, str) and event_type:
        return event_type, payload
    nested = event.get("event")
    if isinstance(nested, dict):
        nested_type = nested.get("type")
        if isinstance(nested_type, str) and nested_type:
            return nested_type, payload
    return "unknown", payload


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    current = event
    for _ in range(8):
        nested = _nested_payload(current)
        if nested is None:
            return current
        if current is event or _is_wrapper_event_type(current.get("type")):
            current = nested
            continue
        return current
    return current


def _nested_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("payload", "item", "event", "msg", "message", "call"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return None


def _is_wrapper_event_type(event_type: object) -> bool:
    return event_type in (
        "event_msg",
        "response_item",
        "item.completed",
        "item_completed",
    )


def _looks_like_command(event_type_lower: str, payload: dict[str, Any]) -> bool:
    payload_type = str(payload.get("type", "")).lower()
    return (
        "command" in event_type_lower
        or "tool_call" in event_type_lower
        or "function_call" in event_type_lower
        or "command" in payload_type
        or "tool_call" in payload_type
        or "function_call" in payload_type
    )


def _looks_like_output(event_type_lower: str, payload: dict[str, Any]) -> bool:
    payload_type = str(payload.get("type", "")).lower()
    return (
        "output" in event_type_lower
        or "stderr" in event_type_lower
        or "stdout" in event_type_lower
        or "output" in payload_type
        or "stderr" in payload_type
        or "stdout" in payload_type
    )


def _command_text(payload: dict[str, Any]) -> str:
    for key in ("command", "cmd"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return " ".join(str(part) for part in value)

    name = payload.get("name")
    arguments = payload.get("arguments")
    if isinstance(name, str) and name:
        if isinstance(arguments, str):
            return f"{name} {arguments}"
        if arguments:
            return f"{name} {json.dumps(arguments, ensure_ascii=False)}"
        return name
    return ""


def _summary_text(value: dict[str, Any]) -> str:
    for key in ("summary", "reasoning_summary"):
        text = value.get(key)
        if isinstance(text, str):
            return text
        if isinstance(text, list):
            return " ".join(_summary_item_text(item) for item in text).strip()
    return ""


def _summary_item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("summary", "reasoning_summary", "text"):
            text = item.get(key)
            if isinstance(text, str):
                return text
    return ""


def _first_text(value: dict[str, Any]) -> str:
    for key in (
        "text",
        "delta",
        "content",
        "output",
        "stdout",
        "stderr",
        "message",
        "error",
    ):
        text = value.get(key)
        if isinstance(text, str):
            return text
        if isinstance(text, list):
            joined = " ".join(_summary_item_text(item) for item in text).strip()
            if joined:
                return joined
    return ""


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
