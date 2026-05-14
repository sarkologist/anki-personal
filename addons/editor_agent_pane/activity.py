# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_PREVIEW_CHARS = 500
MAX_SUMMARY_ITEMS = 3


@dataclass
class CodexActivityRenderer:
    stream_reasoning_summaries: bool = True
    event_count: int = 0
    command_count: int = 0
    output_count: int = 0
    message_count: int = 0
    reasoning_count: int = 0
    error_count: int = 0
    unknown_types: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)
    reasoning_summaries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def record(self, event: dict[str, Any]) -> str | None:
        self.event_count += 1
        event_type = _event_type(event)
        event_type_lower = event_type.lower()
        payload = _payload(event)

        if event_type == "malformed_json":
            self.unknown_types.add(event_type)
            return f"[event] malformed JSON: {_preview(str(event.get('line', '')))}"

        if "error" in event_type_lower:
            self.error_count += 1
            text = _preview(_first_text(event) or event_type)
            self._remember(self.errors, text)
            return f"[error] {text}"

        if _looks_like_output(event_type_lower, payload):
            self.output_count += 1
            return f"[output] {_preview(_first_text(payload) or _first_text(event) or event_type)}"

        if _looks_like_command(event_type_lower, payload):
            self.command_count += 1
            text = _preview(_command_text(payload) or _first_text(event) or event_type)
            self._remember(self.commands, text)
            return f"[tool] {text}"

        if "reasoning" in event_type_lower:
            self.reasoning_count += 1
            if not self.stream_reasoning_summaries:
                return None
            summary = _summary_text(event) or _summary_text(payload)
            if summary:
                text = _preview(summary)
                self._remember(self.reasoning_summaries, text)
                return f"[reasoning] {text}"
            return "[reasoning] updated"

        if "message" in event_type_lower or "response" in event_type_lower:
            self.message_count += 1
            text = _first_text(event) or _first_text(payload)
            if text:
                return f"[message] {_preview(text)}"
            return f"[message] {event_type}"

        if event_type_lower.endswith(("started", "completed")):
            return f"[status] {event_type.replace('.', ' ')}"

        self.unknown_types.add(event_type)
        return f"[event] {event_type}"

    def compact_summary(self) -> str:
        parts = [f"{self.event_count} stream event{_plural(self.event_count)}"]
        if self.commands:
            parts.append(f"tools: {_summary_list(self.commands, self.command_count)}")
        elif self.command_count:
            parts.append(f"{self.command_count} tool call{_plural(self.command_count)}")
        if self.output_count:
            parts.append(f"{self.output_count} output chunk{_plural(self.output_count)}")
        if self.reasoning_summaries:
            parts.append(
                f"reasoning: {_summary_list(self.reasoning_summaries, self.reasoning_count)}"
            )
        elif self.reasoning_count:
            parts.append(
                f"{self.reasoning_count} reasoning update{_plural(self.reasoning_count)}"
            )
        if self.message_count:
            parts.append(f"{self.message_count} message update{_plural(self.message_count)}")
        if self.errors:
            parts.append(f"errors: {_summary_list(self.errors, self.error_count)}")
        elif self.error_count:
            parts.append(f"{self.error_count} error event{_plural(self.error_count)}")
        if self.unknown_types:
            parts.append(
                f"{len(self.unknown_types)} other event type{_plural(len(self.unknown_types))}"
            )
        return f"[Codex activity: {', '.join(parts)}]\n"

    def _remember(self, values: list[str], value: str) -> None:
        if value and len(values) < MAX_SUMMARY_ITEMS:
            values.append(value)


def compact_activity_transcript(
    transcript: str, activity_start: int | None, replacement: str
) -> str:
    if activity_start is None or activity_start < 0 or activity_start > len(transcript):
        return transcript + replacement
    return transcript[:activity_start] + replacement


def _event_type(event: dict[str, Any]) -> str:
    event_type = event.get("type")
    payload = _payload(event)
    payload_type = payload.get("type") if payload is not event else None
    if (
        isinstance(payload_type, str)
        and payload_type
        and event_type in ("event_msg", "response_item")
    ):
        return payload_type
    if isinstance(event_type, str) and event_type:
        return event_type
    nested = event.get("event")
    if isinstance(nested, dict):
        nested_type = nested.get("type")
        if isinstance(nested_type, str) and nested_type:
            return nested_type
    return "unknown"


def _payload(event: dict[str, Any]) -> dict[str, Any]:
    for key in ("payload", "item", "event", "msg", "message", "call"):
        value = event.get(key)
        if isinstance(value, dict):
            return value
    return event


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
            return " ".join(str(item) for item in text)
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
            return " ".join(str(item) for item in text)
    return ""


def _preview(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= MAX_PREVIEW_CHARS:
        return text
    return text[: MAX_PREVIEW_CHARS - 1] + "..."


def _plural(count: int) -> str:
    return "" if count == 1 else "s"


def _summary_list(values: list[str], total_count: int) -> str:
    shown = "; ".join(values)
    extra_count = total_count - len(values)
    if extra_count > 0:
        shown += f"; +{extra_count} more"
    return shown
