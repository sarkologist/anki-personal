# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import aqt
from aqt.qt import (
    QComboBox,
    QDialog,
    QFileDialog,
    QLabel,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.theme import theme_manager
from aqt.utils import showInfo

from .charts import charts_html
from .preparation import read_lesson
from .telemetry import clock, decisions_text, overview


class Diagnostics(QTabWidget):
    def __init__(self, parent: QWidget, read: Callable[[], dict[str, Any]]) -> None:
        super().__init__(parent)
        self.read = read
        self.setMinimumHeight(270)
        # AnkiWebView renders through the media server, which only exists in a
        # running Anki; tests check the generated page instead.
        self.chart_view: Any = None
        if aqt.mw is not None:
            from aqt.webview import AnkiWebView

            self.chart_view = AnkiWebView(self, title="frontier charts")
            if isinstance(parent, QDialog):
                # Release the view's hooks and media-server page with the dialog.
                qconnect(parent.finished, self.release_charts)
        self.chart_html = ""
        self.addTab(self.chart_view or QLabel("Charts need a running Anki."), "Charts")
        self.summary = QTextEdit()
        self.summary.setReadOnly(True)
        self.addTab(self.summary, "Overview")
        calls = QWidget()
        layout = QVBoxLayout(calls)
        # macOS accessibility inspection of the earlier live table crashed
        # inside Qt's cocoa plugin. Use text controls for the changing history.
        self.choices = QComboBox()
        self.calls = QTextEdit()
        self.calls.setReadOnly(True)
        self.details = QTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(130)
        layout.addWidget(self.choices)
        layout.addWidget(self.calls)
        layout.addWidget(self.details)
        self.addTab(calls, "Model calls")
        self.decisions = QTextEdit()
        self.decisions.setReadOnly(True)
        self.addTab(self.decisions, "Decisions")
        export = QPushButton("Export diagnostics")
        qconnect(export.clicked, self.export)
        self.setCornerWidget(export)
        qconnect(self.choices.currentIndexChanged, self.selected)
        self.rows: list[dict[str, Any]] = []
        self.refresh()

    def refresh(self) -> None:
        data = self.read()
        text = overview(
            **{
                k: data[k]
                for k in ("store", "state", "status", "busy", "prepared", "buffer")
            }
        )
        if self.summary.toPlainText() != text:
            self.summary.setPlainText(text)
        html = charts_html(data["store"], data["state"], dark=theme_manager.night_mode)
        if html != self.chart_html:
            self.chart_html = html
            if self.chart_view is not None:
                # Reloading resets scrolling; restore it once the page is ready.
                y = self.chart_view.page().scrollPosition().y()
                self.chart_view.setHtml(html)
                self.chart_view.eval(f"window.scrollTo(0, {y})")
        decisions = decisions_text(data["store"])
        if deferred := data.get("deferred"):
            decisions += "\n\nDeferred targets (latest retry times)\n" + "\n\n".join(
                f"{key[:12]} · retry after {clock(item.get('retry_after'))}\n{read_lesson(item.get('reason', '')).get('reason', '')}"
                for key, item in deferred.items()
            )
        if self.decisions.toPlainText() != decisions:
            self.decisions.setPlainText(decisions)
        rows = list(reversed(data["store"].get("calls", [])))
        if rows == self.rows:
            return
        selected = self.choices.currentData()
        self.rows = rows
        self.choices.blockSignals(True)
        self.choices.clear()
        lines = []
        for row in rows:
            elapsed = row.get("elapsed_seconds")
            seconds = f"{elapsed}s" if elapsed is not None else "unknown duration"
            line = f"{clock(row.get('started_at'))} · {row.get('phase', 'unknown')} · {row.get('provider', 'unknown')} · {seconds} · {row.get('prompt_chars', '?')} input chars · {row.get('status', 'unknown')}"
            self.choices.addItem(line, row.get("call_id"))
            lines.append(line)
        self.calls.setPlainText("\n\n".join(lines) or "No completed calls yet.")
        index = self.choices.findData(selected)
        self.choices.setCurrentIndex(max(0, index) if rows else -1)
        self.choices.blockSignals(False)
        self.selected()

    def release_charts(self) -> None:
        view, self.chart_view = self.chart_view, None
        if view is not None:
            view.cleanup()

    def selected(self, _index: int = -1) -> None:
        index = self.choices.currentIndex()
        self.details.setPlainText(
            json.dumps(self.rows[index], ensure_ascii=False, indent=2)
            if 0 <= index < len(self.rows)
            else "Select a completed call for details."
        )

    def export(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "Export Frontier diagnostics",
            "frontier-diagnostics.json",
            "JSON (*.json)",
        )
        if not filename:
            return
        try:
            Path(filename).write_text(
                json.dumps(self.read(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            showInfo(str(exc), parent=self)
