# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""check_sentence: a stdio MCP server the writer CLI calls before it answers.

Run as `python checker.py <data.json>`. It loads core and lexicon without the
package __init__, so it needs neither aqt nor a profile, and it applies the same
sentence_problems() the admission gate does. A sentence it accepts can still fail
on its translation, pinyin or hint, or as a contrast pair (the pair rules need
both examples).
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

SERVER = "frontier"
TOOL = "check_sentence"
TOOL_NAME = f"mcp__{SERVER}__{TOOL}"
TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "card_id": {"type": "integer", "description": "The target's card_id."},
        "sentence": {"type": "string", "description": "The example sentence."},
        "mode": {"type": "string", "enum": ["word", "contrast", "characters"]},
        "contrast_word": {
            "type": "string",
            "description": "Set only to check a contrast pair's second example.",
        },
    },
    "required": ["card_id", "sentence", "mode"],
    "additionalProperties": False,
}
DESCRIPTION = (
    "Run the code's lexical checks on one example sentence: the target occurs once "
    "as a whole word, and every other word, as the code segments it, is known. "
    "Returns ok, problems, and the segmented context words with any unknown ones."
)


def load_modules() -> tuple[Any, Any]:
    folder = Path(__file__).resolve().parent
    package = types.ModuleType("_frontier_checker")
    package.__path__ = [str(folder)]  # type: ignore[attr-defined]
    sys.modules[package.__name__] = package
    core = importlib.import_module(package.__name__ + ".core")
    lexicon = importlib.import_module(package.__name__ + ".lexicon")
    return core, lexicon


class Checker:
    def __init__(self, data: dict[str, Any], core: Any, lexicon: Any) -> None:
        self.core = core
        self.known = set(data["known"])
        self.lexicon = lexicon.Lexicon.load(Path(data["dictionary"]))
        self.targets = {int(k): v for k, v in data["targets"].items()}

    def check(self, args: dict[str, Any]) -> dict[str, Any]:
        target = self.targets.get(args.get("card_id"))  # type: ignore[arg-type]
        sentence = args.get("sentence")
        if target is None or not isinstance(sentence, str):
            return {"ok": False, "problems": ["Unknown card_id or missing sentence"]}
        source = self.core.Source(
            0,
            0,
            0,
            target["word"],
            "",
            "",
            avoid_sentence=target.get("avoid_sentence", ""),
            force_repair=bool(target.get("force_repair")),
            avoid_words=tuple(target.get("avoid_words", ())),
            rejected_sentences=tuple(target.get("rejected_sentences", ())),
        )
        mode = str(args.get("mode") or "word")
        if contrast := args.get("contrast_word"):
            # The same rules validate_candidate applies to a contrast example.
            source = self.core.Source(
                0,
                0,
                0,
                str(contrast),
                "",
                "contrast",
                avoid_words=tuple(w for w in source.avoid_words if w != contrast),
            )
            mode = "word"
        problems, words = self.core.sentence_problems(
            sentence, source, self.known, self.lexicon, mode
        )
        return {
            "ok": not problems,
            "problems": problems,
            "context_words": list(words),
            "unknown_words": [w for w in words if w not in self.known],
        }


def respond(message: dict[str, Any], checker: Checker) -> dict[str, Any] | None:
    method = message.get("method")
    if "id" not in message:
        return None  # notifications need no answer
    result: dict[str, Any]
    if method == "initialize":
        params = message.get("params") or {}
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER, "version": "1"},
        }
    elif method == "tools/list":
        result = {
            "tools": [
                {"name": TOOL, "description": DESCRIPTION, "inputSchema": TOOL_SCHEMA}
            ]
        }
    elif method == "tools/call":
        params = message.get("params") or {}
        if params.get("name") != TOOL:
            return error(message, -32602, "Unknown tool")
        verdict = checker.check(params.get("arguments") or {})
        result = {
            "content": [
                {"type": "text", "text": json.dumps(verdict, ensure_ascii=False)}
            ],
            "isError": False,
        }
    elif method == "ping":
        result = {}
    else:
        return error(message, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": message["id"], "result": result}


def error(message: dict[str, Any], code: int, text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": message.get("id"),
        "error": {"code": code, "message": text},
    }


def serve(data_path: str) -> None:
    core, lexicon = load_modules()
    data = json.loads(Path(data_path).read_text(encoding="utf-8"))
    checker = Checker(data, core, lexicon)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if reply := respond(message, checker):
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    serve(sys.argv[1])
