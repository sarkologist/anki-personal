# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

MAX_PREVIEW_CHARS = 500
MAX_SUMMARY_ITEMS = 3
SAFE_METADATA_KEYS = (
    "status",
    "phase",
    "name",
    "action",
    "query",
    "url",
    "pattern",
    "duration_ms",
    "result_count",
    "results_count",
    "count",
)


@dataclass
class CodexActivityRenderer:
    stream_reasoning_summaries: bool = True
    event_count: int = 0
    command_count: int = 0
    output_count: int = 0
    message_count: int = 0
    reasoning_count: int = 0
    error_count: int = 0
    web_count: int = 0
    unknown_types: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)
    web_actions: list[str] = field(default_factory=list)
    reasoning_summaries: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    detail_lines: list[str] = field(default_factory=list)

    def record(self, event: dict[str, Any]) -> str | None:
        self.event_count += 1
        event_type, payload = _event_view(event)
        event_type_lower = event_type.lower()

        if event_type == "malformed_json":
            self.unknown_types.add(event_type)
            return self._line(
                f"[event] malformed JSON: {_preview(str(event.get('line', '')))}"
            )

        if "error" in event_type_lower:
            self.error_count += 1
            text = _preview(_first_text(event) or event_type)
            self._remember(self.errors, text)
            return self._line(f"[error] {text}")

        if is_web_search_event(event_type_lower, payload):
            self.web_count += 1
            text = web_search_activity_text(event_type, event, payload) or "activity"
            self._remember(self.web_actions, text)
            return self._line(f"[web] {text}")

        if _looks_like_output(event_type_lower, payload):
            self.output_count += 1
            return self._line(
                f"[output] {_preview(_first_text(payload) or _first_text(event) or event_type)}"
            )

        if _looks_like_command(event_type_lower, payload):
            self.command_count += 1
            text = _preview(_command_text(payload) or _first_text(event) or event_type)
            self._remember(self.commands, text)
            return self._line(f"[tool] {text}")

        if "reasoning" in event_type_lower:
            self.reasoning_count += 1
            if not self.stream_reasoning_summaries:
                return None
            summary = _summary_text(event) or _summary_text(payload)
            if summary:
                text = _preview(summary)
                self._remember(self.reasoning_summaries, text)
                return self._line(f"[reasoning] {text}")
            return self._line("[reasoning] activity")

        if "message" in event_type_lower or "response" in event_type_lower:
            self.message_count += 1
            text = _first_text(event) or _first_text(payload)
            if text:
                return self._line(f"[message] {_preview(text)}")
            return self._line(f"[message] {event_type}")

        if event_type_lower.endswith(("started", "completed")):
            detail = safe_event_metadata_text(event, payload)
            line = f"[status] {event_type.replace('.', ' ')}"
            if detail:
                line += f" ({detail})"
            return self._line(line)

        self.unknown_types.add(event_type)
        detail = safe_event_metadata_text(event, payload)
        line = f"[event] {event_type}"
        if detail:
            line += f" ({detail})"
        return self._line(line)

    def compact_summary(self) -> str:
        parts = [f"{self.event_count} stream event{_plural(self.event_count)}"]
        if self.commands:
            parts.append(f"tools: {_summary_list(self.commands, self.command_count)}")
        elif self.command_count:
            parts.append(f"{self.command_count} tool call{_plural(self.command_count)}")
        if self.web_actions:
            parts.append(f"web: {_summary_list(self.web_actions, self.web_count)}")
        elif self.web_count:
            parts.append(f"{self.web_count} web update{_plural(self.web_count)}")
        if self.output_count:
            parts.append(
                f"{self.output_count} output chunk{_plural(self.output_count)}"
            )
        if self.reasoning_summaries:
            parts.append(
                f"reasoning: {_summary_list(self.reasoning_summaries, self.reasoning_count)}"
            )
        elif self.reasoning_count:
            parts.append(
                f"{self.reasoning_count} reasoning activity update{_plural(self.reasoning_count)}"
            )
        if self.message_count:
            parts.append(
                f"{self.message_count} message update{_plural(self.message_count)}"
            )
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

    def _line(self, line: str) -> str:
        self.detail_lines.append(line)
        return line


@dataclass
class ClaudeActivityRenderer:
    """Renders Claude Code stream-json content blocks as activity lines.

    Each call to ``record`` receives one content block (thinking / text /
    tool_use / tool_result) and returns a single live activity line, mirroring
    the ``record``/``compact_summary``/``detail_lines`` interface of
    ``CodexActivityRenderer``.
    """

    event_count: int = 0
    tool_count: int = 0
    thinking_count: int = 0
    text_count: int = 0
    error_count: int = 0
    tools: list[str] = field(default_factory=list)
    detail_lines: list[str] = field(default_factory=list)
    _structured_output_ids: set[str] = field(default_factory=set)

    def record(self, block: dict[str, Any]) -> str | None:
        if not isinstance(block, dict):
            return None
        block_type = block.get("type")
        if block_type == "thinking":
            text = _preview(str(block.get("thinking") or ""))
            if not text:
                return None
            self.event_count += 1
            self.thinking_count += 1
            return self._line(f"[thinking] {text}")
        if block_type == "text":
            text = _preview(str(block.get("text") or ""))
            if not text:
                return None
            self.event_count += 1
            self.text_count += 1
            return self._line(f"[note] {text}")
        if block_type == "tool_use":
            name = str(block.get("name") or "tool")
            if name == "StructuredOutput":
                # Internal mechanism that returns the final JSON answer; not a
                # real tool. Remember its id so its tool_result (including schema
                # retries) is suppressed too, rather than shown as a tool error.
                tool_id = block.get("id")
                if isinstance(tool_id, str):
                    self._structured_output_ids.add(tool_id)
                return None
            self.event_count += 1
            self.tool_count += 1
            self._remember(self.tools, name)
            brief = _claude_tool_brief(name, block.get("input"))
            return self._line(f"[tool] {name}" + (f": {brief}" if brief else ""))
        if block_type == "tool_result":
            if block.get("tool_use_id") in self._structured_output_ids:
                # Internal StructuredOutput round-trip, not a real tool result.
                return None
            if not block.get("is_error"):
                return None
            self.event_count += 1
            self.error_count += 1
            return self._line(
                f"[tool error] {_preview(_claude_tool_result_text(block))}"
            )
        return None

    def compact_summary(self) -> str:
        parts = [f"{self.event_count} update{_plural(self.event_count)}"]
        if self.tools:
            parts.append(f"tools: {_summary_list(self.tools, self.tool_count)}")
        if self.thinking_count:
            parts.append(
                f"{self.thinking_count} thinking step{_plural(self.thinking_count)}"
            )
        if self.error_count:
            parts.append(f"{self.error_count} tool error{_plural(self.error_count)}")
        return f"[Claude activity: {', '.join(parts)}]\n"

    def _remember(self, values: list[str], value: str) -> None:
        if value and value not in values and len(values) < MAX_SUMMARY_ITEMS:
            values.append(value)

    def _line(self, line: str) -> str:
        self.detail_lines.append(line)
        return line


def _claude_tool_brief(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return ""
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        return _basename(path) if isinstance(path, str) else ""
    if name == "Bash":
        return _preview(str(tool_input.get("command") or ""))
    if name == "Grep":
        return _preview(str(tool_input.get("pattern") or ""))
    if name == "Glob":
        return _preview(str(tool_input.get("pattern") or tool_input.get("glob") or ""))
    if name == "WebFetch":
        return _preview(str(tool_input.get("url") or ""))
    if name == "WebSearch":
        return _preview(str(tool_input.get("query") or ""))
    for value in tool_input.values():
        if isinstance(value, str) and value:
            return _preview(value)
    return ""


def _basename(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _claude_tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return ""


def compact_activity_transcript(
    transcript: str, activity_start: int | None, replacement: str
) -> str:
    if activity_start is None or activity_start < 0 or activity_start > len(transcript):
        return transcript + replacement
    return transcript[:activity_start] + replacement


def _event_type(event: dict[str, Any]) -> str:
    return _event_view(event)[0]


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


def is_web_search_event(event_type_lower: str, payload: dict[str, Any]) -> bool:
    payload_type = str(payload.get("type", "")).lower()
    return "web_search" in event_type_lower or "web_search" in payload_type


def web_search_activity_text(
    event_type: str,
    event: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    event_type_lower = event_type.lower()
    action = _action_payload(event, payload)
    action_type = _action_type(action, event, payload)
    query = _query_text(action, payload, event)
    url = (
        _first_scalar(action, "url", "uri")
        or _first_scalar(payload, "url", "uri")
        or _first_scalar(event, "url", "uri")
    )
    pattern = (
        _first_scalar(action, "pattern", "find_text", "term")
        or _first_scalar(payload, "pattern", "find_text", "term")
        or _first_scalar(event, "pattern", "find_text", "term")
    )

    if event_type_lower.endswith("_end") or event_type_lower.endswith(".end"):
        duration = _duration_text(
            _first_scalar(event, "duration_ms", "elapsed_ms")
            or _first_scalar(payload, "duration_ms", "elapsed_ms")
        )
        if duration:
            return f"completed in {duration}"
        return "completed"

    if action_type in ("find_in_page", "find", "find_on_page") or pattern:
        if pattern and url:
            return f"find in page: {_preview(pattern)} ({_preview(url)})"
        if pattern:
            return f"find in page: {_preview(pattern)}"
        if url:
            return f"find in page: {_preview(url)}"
        return "find in page"

    if action_type in ("search", "queries") or query:
        if query:
            return f"search: {_preview(query)}"
        return "search"

    if action_type in ("open_page", "open", "open_url") or url:
        if url:
            return f"open page: {_preview(url)}"
        return "open page"

    if event_type_lower in ("web_search", "web_search_begin"):
        return "search"

    if action_type:
        return action_type.replace("_", " ")
    return ""


def safe_event_metadata_text(event: dict[str, Any], payload: dict[str, Any]) -> str:
    action = _action_payload(event, payload)
    pairs: list[tuple[str, str]] = []

    for key in SAFE_METADATA_KEYS:
        if key == "action":
            value = _action_type(action, event, payload)
        elif key == "query":
            value = _query_text(action, payload, event)
        elif key == "url":
            value = (
                _first_scalar(action, "url", "uri")
                or _first_scalar(payload, "url", "uri")
                or _first_scalar(event, "url", "uri")
            )
        elif key == "pattern":
            value = _first_scalar(action, "pattern", "find_text", "term") or (
                _first_scalar(payload, "pattern", "find_text", "term")
                or _first_scalar(event, "pattern", "find_text", "term")
            )
        else:
            value = _first_scalar(payload, key) or _first_scalar(event, key)

        if value:
            pairs.append((key, _preview(value)))

    return ", ".join(f"{key}={value}" for key, value in pairs)


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


def _action_payload(event: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    for source in (payload, event):
        action = source.get("action")
        if isinstance(action, dict):
            return action
    return {}


def _action_type(
    action: dict[str, Any],
    event: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    return (
        _first_scalar(action, "type", "action", "kind")
        or _first_scalar(payload, "action_type", "action_kind")
        or _first_scalar(event, "action_type", "action_kind")
        or _scalar_action(payload)
        or _scalar_action(event)
    )


def _scalar_action(value: dict[str, Any]) -> str:
    action = value.get("action")
    if isinstance(action, str):
        return action
    return ""


def _query_text(*values: dict[str, Any]) -> str:
    for value in values:
        for key in ("query", "queries", "search_query", "search_queries"):
            text = _metadata_value_text(value.get(key))
            if text:
                return text
    return ""


def _metadata_value_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [
            text
            for item in value
            if (text := _metadata_value_text(_query_item_value(item)))
        ]
        return "; ".join(parts)
    if isinstance(value, dict):
        return _query_text(value) or _first_scalar(value, "q", "text", "value")
    return ""


def _query_item_value(item: Any) -> Any:
    if isinstance(item, dict):
        for key in ("query", "q", "text", "value"):
            if key in item:
                return item[key]
    return item


def _first_scalar(value: dict[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str):
            return item
        if isinstance(item, (int, float)):
            return str(item)
    return ""


def _duration_text(value: str) -> str:
    if not value:
        return ""
    try:
        duration_ms = float(value)
    except ValueError:
        return value
    if duration_ms >= 1000:
        return f"{duration_ms / 1000:.1f}s"
    return f"{duration_ms:g}ms"


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
