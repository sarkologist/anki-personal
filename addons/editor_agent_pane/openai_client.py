# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import requests

from .patches import (
    EditorSnapshot,
    NotePatch,
    PatchValidationError,
    validate_note_patch,
)
from .sources import SourceAccessError, read_source_file, search_source_files

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"

SYSTEM_PROMPT = """You are an Anki editor assistant.

Help improve the current note/card using the provided editor context and any
read-only project sources the user asks you to inspect. Never invent source
details. When you want to change the current note, call propose_note_patch.
Do not claim that a change has been applied; the user must approve proposals
inside Anki before anything changes.
"""


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_current_editor_context",
        "description": "Return the current Anki editor note/card context.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "search_project_sources",
        "description": "Search read-only text/code/markdown files in the selected project folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "read_project_source_file",
        "description": "Read a single source file by path relative to the selected project folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "propose_note_patch",
        "description": "Stage a patch for the current Anki note. The user must approve it before it is applied.",
        "parameters": {
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
                            "type": "array",
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
                    "additionalProperties": False,
                },
            },
            "required": ["summary", "field_updates"],
            "additionalProperties": False,
        },
    },
]


@dataclass(frozen=True)
class AgentResult:
    text: str
    proposals: tuple[NotePatch, ...]
    response_id: str | None


class ToolEnvironment:
    def __init__(
        self,
        *,
        snapshot: EditorSnapshot,
        project_root: str,
        max_source_file_bytes: int,
        max_source_search_results: int,
        max_source_files_scanned: int,
    ) -> None:
        self.snapshot = snapshot
        self.project_root = project_root
        self.max_source_file_bytes = max_source_file_bytes
        self.max_source_search_results = max_source_search_results
        self.max_source_files_scanned = max_source_files_scanned
        self.proposals: list[NotePatch] = []

    def call(self, name: str, arguments: dict[str, Any]) -> str:
        try:
            result = self._call(name, arguments)
            return json.dumps({"ok": True, "result": result}, ensure_ascii=False)
        except (PatchValidationError, SourceAccessError, ValueError) as exc:
            return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)

    def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "get_current_editor_context":
            return self.snapshot.as_tool_result()

        if name == "search_project_sources":
            if not self.project_root:
                raise SourceAccessError("No project folder is selected.")
            requested_max = int(arguments.get("max_results") or self.max_source_search_results)
            max_results = min(requested_max, self.max_source_search_results)
            hits = search_source_files(
                self.project_root,
                str(arguments.get("query", "")),
                max_results=max_results,
                max_files=self.max_source_files_scanned,
                max_file_bytes=self.max_source_file_bytes,
            )
            return [hit.__dict__ for hit in hits]

        if name == "read_project_source_file":
            if not self.project_root:
                raise SourceAccessError("No project folder is selected.")
            return {
                "path": str(arguments.get("path", "")),
                "text": read_source_file(
                    self.project_root,
                    str(arguments.get("path", "")),
                    max_bytes=self.max_source_file_bytes,
                ),
            }

        if name == "propose_note_patch":
            patch = validate_note_patch(arguments, self.snapshot)
            self.proposals.append(patch)
            return {
                "proposal_index": len(self.proposals) - 1,
                "summary": patch.summary,
            }

        raise ValueError(f"Unknown tool: {name}.")


class ResponsesAgent:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        previous_response_id: str | None = None,
        on_delta: Callable[[str], None] | None = None,
        on_tool: Callable[[str], None] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.previous_response_id = previous_response_id
        self.on_delta = on_delta
        self.on_tool = on_tool

    def send(self, prompt: str, tools: ToolEnvironment) -> AgentResult:
        input_items: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": prompt,
            }
        ]
        previous_response_id = self.previous_response_id
        final_text = ""
        response_id: str | None = None

        for _ in range(6):
            response = self._stream_response(
                input_items=input_items,
                previous_response_id=previous_response_id,
            )
            response_id = response.get("id") or response_id
            text = _extract_output_text(response)
            if text:
                final_text += text

            function_calls = _extract_function_calls(response)
            if not function_calls:
                self.previous_response_id = response_id
                return AgentResult(
                    text=final_text.strip(),
                    proposals=tuple(tools.proposals),
                    response_id=response_id,
                )

            input_items = []
            previous_response_id = response_id
            for call in function_calls:
                name = str(call.get("name", ""))
                if self.on_tool:
                    self.on_tool(name)
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                except json.JSONDecodeError as exc:
                    output = json.dumps({"ok": False, "error": str(exc)})
                else:
                    output = tools.call(name, arguments)
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.get("call_id"),
                        "output": output,
                    }
                )

        raise RuntimeError("The assistant used too many tool-call rounds.")

    def _stream_response(
        self,
        *,
        input_items: list[dict[str, Any]],
        previous_response_id: str | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "instructions": SYSTEM_PROMPT,
            "input": input_items,
            "tools": TOOLS,
            "stream": True,
        }
        if previous_response_id:
            payload["previous_response_id"] = previous_response_id

        response = requests.post(
            OPENAI_RESPONSES_URL,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            stream=True,
            timeout=(10, 180),
        )
        if response.status_code >= 400:
            raise RuntimeError(f"OpenAI API error {response.status_code}: {response.text}")

        output_text_parts: list[str] = []
        tool_calls: dict[int, dict[str, Any]] = {}
        completed_response: dict[str, Any] | None = None

        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data: "):
                continue
            data = raw_line[6:]
            if data == "[DONE]":
                break
            event = json.loads(data)
            event_type = event.get("type")

            if event_type == "response.output_text.delta":
                delta = str(event.get("delta", ""))
                output_text_parts.append(delta)
                if self.on_delta:
                    self.on_delta(delta)
            elif event_type == "response.output_item.added":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    tool_calls[int(event.get("output_index", 0))] = dict(item)
            elif event_type == "response.function_call_arguments.delta":
                index = int(event.get("output_index", 0))
                item = tool_calls.setdefault(index, {"type": "function_call"})
                item["arguments"] = str(item.get("arguments", "")) + str(
                    event.get("delta", "")
                )
            elif event_type == "response.output_item.done":
                item = event.get("item") or {}
                if item.get("type") == "function_call":
                    tool_calls[int(event.get("output_index", 0))] = dict(item)
            elif event_type == "response.completed":
                completed_response = event.get("response") or {}
            elif event_type == "error":
                raise RuntimeError(str(event.get("message") or event))

        if completed_response is None:
            completed_response = {"output": []}

        if output_text_parts and not _extract_output_text(completed_response):
            completed_response["output_text"] = "".join(output_text_parts)
        if tool_calls and not _extract_function_calls(completed_response):
            completed_response["output"] = list(tool_calls.values())
        return completed_response


def _extract_function_calls(response: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for item in response.get("output", [])
        if isinstance(item, dict) and item.get("type") == "function_call"
    ]


def _extract_output_text(response: dict[str, Any]) -> str:
    if text := response.get("output_text"):
        return str(text)

    parts: list[str] = []
    for item in response.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") in {
                "output_text",
                "text",
            }:
                parts.append(str(content.get("text", "")))
    return "".join(parts)
