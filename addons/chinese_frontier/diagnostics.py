# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import json
from typing import Any, Callable

from aqt.theme import theme_manager
from aqt.utils import showInfo, tooltip

from . import panels
from .charts import charts_html
from .panels import Panel, field
from .preparation import read_lesson
from .telemetry import clock, decisions_text, overview

NAME = "diagnostics"


class Diagnostics:
    """The diagnostics tabs of a settings panel. Field names start with
    "diagnostics."; message() answers the ones the page sends."""

    def __init__(self, read: Callable[[], dict[str, Any]]) -> None:
        self.read = read
        self.rows: list[dict[str, Any]] = []
        self.selected: Any = None

    def spec(self) -> dict[str, Any]:
        current = self.fields()

        def own(kind: str, name: str, **props: Any) -> dict[str, Any]:
            name = f"{NAME}.{name}"
            return field(kind, name, **props, **current[name])

        def tab(label: str, *body: dict[str, Any]) -> dict[str, Any]:
            return {"label": label, "body": list(body)}

        # The page shows the charts in a scriptless frame; the rest is text.
        return field(
            "tabs",
            NAME,
            tabs=[
                tab("Charts", own("frame", "charts", height=420)),
                tab("Overview", own("readonly", "overview", height=300)),
                tab(
                    "Model calls",
                    own("select", "call", notify=True),
                    own("readonly", "calls", height=200),
                    own("readonly", "details", height=130),
                ),
                tab("Decisions", own("readonly", "decisions", height=300)),
            ],
            corner=[field("button", f"{NAME}.export", label="Export diagnostics")],
        )

    def fields(self) -> dict[str, dict[str, Any]]:
        data = self.read()
        text = overview(
            **{
                k: data[k]
                for k in ("store", "state", "status", "busy", "prepared", "buffer")
            }
        )
        if feedback := data.get("feedback"):
            text += "\n\n" + feedback
        decisions = decisions_text(data["store"])
        if deferred := data.get("deferred"):
            decisions += "\n\nDeferred targets (latest retry times)\n" + "\n\n".join(
                f"{key[:12]} · retry after {clock(item.get('retry_after'))}\n{read_lesson(item.get('reason', '')).get('reason', '')}"
                for key, item in deferred.items()
            )
        self.rows = list(reversed(data["store"].get("calls", [])))
        lines = []
        for row in self.rows:
            elapsed = row.get("elapsed_seconds")
            seconds = f"{elapsed}s" if elapsed is not None else "unknown duration"
            lines.append(
                f"{clock(row.get('started_at'))} · {row.get('phase', 'unknown')} · {row.get('provider', 'unknown')} · {seconds} · {row.get('prompt_chars', '?')} input chars · {row.get('status', 'unknown')}"
            )
        ids = [row.get("call_id") for row in self.rows]
        if self.selected not in ids:
            self.selected = ids[0] if ids else None
        return {
            f"{NAME}.overview": {"value": text},
            f"{NAME}.charts": {
                "html": charts_html(
                    data["store"], data["state"], dark=theme_manager.night_mode
                )
            },
            f"{NAME}.decisions": {"value": decisions},
            f"{NAME}.call": {
                "items": [{"value": id, "label": line} for id, line in zip(ids, lines)],
                "value": self.selected,
            },
            f"{NAME}.calls": {"value": "\n\n".join(lines) or "No completed calls yet."},
            f"{NAME}.details": self.details(),
        }

    def details(self) -> dict[str, Any]:
        row = next((r for r in self.rows if r.get("call_id") == self.selected), None)
        return {
            "value": json.dumps(row, ensure_ascii=False, indent=2)
            if row is not None
            else "Select a completed call for details."
        }

    def refresh(self, panel: Panel) -> None:
        panel.update(self.fields())

    def message(self, message: dict[str, Any]) -> Any:
        name = message.get("name")
        if message.get("type") == "change" and name == f"{NAME}.call":
            self.selected = message.get("values", {}).get(name)
            return panels.update({f"{NAME}.details": self.details()})
        if message.get("type") == "action" and name == f"{NAME}.export":
            self.export()
        return None

    def export(self) -> None:
        data = json.dumps(self.read(), ensure_ascii=False, indent=2)
        try:
            path = panels.save_file("frontier-diagnostics.json", data.encode("utf-8"))
        except OSError as exc:
            showInfo(str(exc))
            return
        if path:
            tooltip(f"Exported to {path}")
