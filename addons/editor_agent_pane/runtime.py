# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
import time
import weakref
from collections.abc import Callable
from concurrent.futures import Future
from threading import Event
from typing import Any

import aqt
from aqt import gui_hooks
from aqt.editor import Editor, EditorMode
from aqt.operations.note import update_note, update_notes
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextCursor,
    Qt,
    QToolButton,
    QTimer,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import openFolder, showWarning, tooltip
from aqt.webview import AnkiWebView

from .activity import CodexActivityRenderer
from .agent_log import JsonLineAgentRunLogger, ensure_agent_log_folder
from .codex_client import (
    DEFAULT_PROJECT_FOLDER_ACCESS,
    PROJECT_FOLDER_ACCESS_READ_ONLY,
    PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE,
    AgentStopped,
    CodexCliAgent,
    normalize_project_folder_access,
    project_root_status,
    resolve_codex_path,
)
from .effort_options import (
    EFFORT_OPTIONS,
    effort_option_index,
    effort_options_with_legacy,
    effort_value,
)
from .latex_preview import LegacyLatexPreviewRenderer
from .model_options import (
    DEFAULT_PROVIDER,
    MODEL_OPTIONS,
    PROVIDER_CODEX,
    PROVIDER_OLLAMA,
    PROVIDER_OPTIONS,
    model_option_index,
    model_options_with_legacy,
    ollama_model_option_index,
    ollama_model_options_with_legacy,
    provider_option_index,
    provider_value,
)
from .note_images import collect_note_images
from .ollama_client import (
    DEFAULT_OLLAMA_HOST,
    OllamaCliAgent,
    discover_ollama_models,
    normalize_ollama_host,
    ollama_model_names,
    resolve_ollama_path,
)
from .patches import (
    EditorSnapshot,
    FieldSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    NotePatch,
    PatchValidationError,
    SelectedCardSnapshot,
    SelectedNoteSnapshot,
    SelectedTextSnapshot,
    validate_selected_text_snapshot,
)
from .recent_folders import (
    NO_PROJECT_FOLDER_LABEL,
    project_folder_choices,
    remember_project_folder,
)
from .surface import (
    js_append_to_activity,
    js_append_transcript,
    js_apply_agent_proposal,
    js_clear_proposal,
    js_clear_transcript,
    js_replace_element,
    js_set_proposal,
    multi_note_patch_card_ids,
    render_activity_line,
    render_activity_start,
    render_activity_summary,
    render_assistant_message,
    render_error_message,
    render_multi_note_card_proposal_diff,
    render_proposal_diff,
    render_user_message,
    selection_context_label_text,
    surface_body,
)
from .ui_text import AGENT_BUTTON_LABEL, AGENT_BUTTON_TIP, AGENT_PANE_SHORTCUT

ADDON = "editor_agent_pane"
TOGGLE_COMMAND = "editorAgentPane"
DEFAULT_SPLITTER_SIZES = [570, 80]
PROMPT_HISTORY_LIMIT = 10
SELECTION_CONTEXT_REFRESH_MS = 500
SELECTION_CONTEXT_JS = (
    "typeof getAgentSelectedTextContext === 'function' "
    "? getAgentSelectedTextContext() : null"
)
DEFAULT_CONFIG = {
    "provider": DEFAULT_PROVIDER,
    "codex_path": "",
    "codex_model": "",
    "ollama_path": "",
    "ollama_host": DEFAULT_OLLAMA_HOST,
    "ollama_model": "",
    "reasoning_effort": "",
    "custom_instructions": "",
    "custom_instructions_by_model": {},
    "instructions_collapsed": False,
    "prompt_history_by_model": {},
    "project_folder": "",
    "project_folder_access": DEFAULT_PROJECT_FOLDER_ACCESS,
    "recent_project_folders": [],
    "fast_mode": False,
    "stream_reasoning_summaries": True,
    "timeout_seconds": 300,
    "splitter_sizes": DEFAULT_SPLITTER_SIZES,
}
PROJECT_FOLDER_ACCESS_OPTIONS = (
    ("Writable project folder", PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE),
    ("Read-only project folder", PROJECT_FOLDER_ACCESS_READ_ONLY),
)

_installed = False
_panes: weakref.WeakKeyDictionary[Editor, "EditorAgentPane"] = (
    weakref.WeakKeyDictionary()
)
_browser_panes: weakref.WeakKeyDictionary[Any, "EditorAgentPane"] = (
    weakref.WeakKeyDictionary()
)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    gui_hooks.editor_did_init_buttons.append(_add_editor_button)
    gui_hooks.editor_did_init.append(_add_editor_menu_action)
    gui_hooks.editor_did_load_note.append(_on_editor_did_load_note)
    gui_hooks.browser_will_show.append(_on_browser_will_show)
    gui_hooks.browser_did_change_row.append(_on_browser_did_change_row)


def _config() -> dict[str, Any]:
    assert aqt.mw is not None
    saved = aqt.mw.addonManager.getConfig(ADDON) or {}
    config = {key: saved.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    if "codex_model" not in saved and "model" in saved:
        config["codex_model"] = str(saved["model"]).strip()
    if "codex_path" not in saved and config["codex_model"] == "gpt-5.2":
        config["codex_model"] = ""
    config["provider"] = provider_value(config["provider"])
    config["project_folder_access"] = normalize_project_folder_access(
        str(config["project_folder_access"])
    )
    config["ollama_host"] = normalize_ollama_host(str(config["ollama_host"]))
    config["stream_reasoning_summaries"] = _bool_config(
        config["stream_reasoning_summaries"],
        True,
    )
    config["fast_mode"] = _bool_config(config["fast_mode"], False)
    config["reasoning_effort"] = effort_value(config["reasoning_effort"])
    config["custom_instructions_by_model"] = _custom_instructions_by_model(
        config["custom_instructions_by_model"]
    )
    config["instructions_collapsed"] = _bool_config(
        config["instructions_collapsed"],
        False,
    )
    config["prompt_history_by_model"] = _prompt_history_by_model(
        config["prompt_history_by_model"]
    )
    return config


def _bool_config(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _write_config(config: dict[str, Any]) -> None:
    assert aqt.mw is not None
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    aqt.mw.addonManager.writeConfig(ADDON, merged)


def _custom_instructions_by_model(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, str]] = {}
    for provider, models in value.items():
        provider_key = str(provider).strip()
        if provider_key not in {PROVIDER_CODEX, PROVIDER_OLLAMA}:
            continue
        if not isinstance(models, dict):
            continue
        instructions_by_model: dict[str, str] = {}
        for model, instructions in models.items():
            if isinstance(instructions, str):
                instructions_by_model[str(model).strip()] = instructions
            elif instructions is not None:
                instructions_by_model[str(model).strip()] = str(instructions)
        normalized[provider_key] = instructions_by_model
    return normalized


def _custom_instructions_for_model(
    config: dict[str, Any],
    *,
    provider: object,
    model: object,
) -> str:
    instructions = _custom_instructions_by_model(
        config.get("custom_instructions_by_model", {})
    )
    provider_instructions = instructions.get(provider_value(provider), {})
    model_key = str(model).strip()
    if model_key in provider_instructions:
        return provider_instructions[model_key]
    return str(config.get("custom_instructions") or "")


def _set_custom_instructions_for_model(
    config: dict[str, Any],
    *,
    provider: object,
    model: object,
    instructions: str,
) -> None:
    scoped = _custom_instructions_by_model(
        config.get("custom_instructions_by_model", {})
    )
    provider_key = provider_value(provider)
    provider_instructions = dict(scoped.get(provider_key, {}))
    provider_instructions[str(model).strip()] = instructions
    scoped[provider_key] = provider_instructions
    config["custom_instructions_by_model"] = scoped


def _prompt_history_by_model(value: Any) -> dict[str, dict[str, list[str]]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, list[str]]] = {}
    for provider, models in value.items():
        provider_key = str(provider).strip()
        if provider_key not in {PROVIDER_CODEX, PROVIDER_OLLAMA}:
            continue
        if not isinstance(models, dict):
            continue
        history_by_model: dict[str, list[str]] = {}
        for model, history in models.items():
            if not isinstance(history, list):
                continue
            model_key = str(model).strip()
            messages: list[str] = []
            for item in history:
                if item is None:
                    continue
                message = item if isinstance(item, str) else str(item)
                message = message.strip()
                if message:
                    messages.append(message)
                if len(messages) >= PROMPT_HISTORY_LIMIT:
                    break
            if messages:
                history_by_model[model_key] = messages
        if history_by_model:
            normalized[provider_key] = history_by_model
    return normalized


def _prompt_history_for_model(
    config: dict[str, Any],
    *,
    provider: object,
    model: object,
) -> tuple[str, ...]:
    history = _prompt_history_by_model(config.get("prompt_history_by_model", {}))
    provider_history = history.get(provider_value(provider), {})
    return tuple(provider_history.get(str(model).strip(), ()))


def _record_prompt_history_for_model(
    config: dict[str, Any],
    *,
    provider: object,
    model: object,
    prompt: str,
) -> None:
    clean_prompt = prompt.strip()
    if not clean_prompt:
        return
    scoped = _prompt_history_by_model(config.get("prompt_history_by_model", {}))
    provider_key = provider_value(provider)
    model_key = str(model).strip()
    provider_history = dict(scoped.get(provider_key, {}))
    provider_history[model_key] = (
        [clean_prompt] + list(provider_history.get(model_key, ()))
    )[:PROMPT_HISTORY_LIMIT]
    scoped[provider_key] = provider_history
    config["prompt_history_by_model"] = scoped


def _validated_splitter_sizes(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != len(DEFAULT_SPLITTER_SIZES):
        return list(DEFAULT_SPLITTER_SIZES)

    sizes: list[int] = []
    for size in value:
        if not isinstance(size, int) or size < 1:
            return list(DEFAULT_SPLITTER_SIZES)
        sizes.append(size)
    return sizes


def _format_agent_turn_duration(elapsed_seconds: float) -> str:
    seconds = max(0.0, elapsed_seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    total_seconds = int(round(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    return f"{minutes}m {remaining_seconds:02d}s"


def _activity_summary_with_elapsed(
    summary: str,
    *,
    elapsed_seconds: float,
    verb: str = "took",
) -> str:
    elapsed = _format_agent_turn_duration(elapsed_seconds)
    clean = summary.strip()
    suffix = f"{verb} {elapsed}"
    if clean.startswith("[") and " activity: " in clean and clean.endswith("]"):
        return f"{clean[:-1]}, {suffix}]\n"
    return f"{clean} ({suffix})\n"


def _activity_status_summary(
    status: str,
    *,
    elapsed_seconds: float,
    role: str = "Codex",
) -> str:
    elapsed = _format_agent_turn_duration(elapsed_seconds)
    return f"[{role} activity: {status} after {elapsed}]\n"


def _add_editor_button(buttons: list[str], editor: Editor) -> None:
    buttons.append(
        editor.addButton(
            None,
            TOGGLE_COMMAND,
            _toggle_pane,
            tip=AGENT_BUTTON_TIP,
            label=AGENT_BUTTON_LABEL,
            keys=AGENT_PANE_SHORTCUT,
            disables=False,
        )
    )


def _add_editor_menu_action(editor: Editor) -> None:
    form = getattr(editor.parentWindow, "form", None)
    menu = getattr(form, "menu_Edit", None)
    if menu is None:
        return
    action = QAction("Agent", menu)
    qconnect(action.triggered, lambda _checked=False: _toggle_pane(editor))
    menu.addAction(action)


def _on_editor_did_load_note(editor: Editor) -> None:
    if pane := _panes.get(editor):
        pane.on_editor_context_changed()


def _on_browser_did_change_row(browser: Any) -> None:
    editor = getattr(browser, "editor", None)
    if editor is not None and (pane := _panes.get(editor)):
        pane.on_editor_context_changed()
    _sync_browser_multi_card_pane(browser)


def _toggle_pane(editor: Editor) -> None:
    pane = _panes.get(editor)
    if pane is None:
        pane = EditorAgentPane(editor)
        _panes[editor] = pane
    pane.toggle()


def _on_browser_will_show(browser: Any) -> None:
    _ensure_browser_multi_card_pane(browser)
    _sync_browser_multi_card_pane(browser)


def _ensure_browser_multi_card_pane(browser: Any) -> "EditorAgentPane":
    pane = _browser_panes.get(browser)
    if pane is not None:
        return pane
    pane = EditorAgentPane(browser=browser, embedded=True)
    _browser_panes[browser] = pane
    fields_area = browser.form.fieldsArea
    layout = fields_area.layout()
    if layout is not None:
        layout.addWidget(pane, 1)
    pane.hide()
    return pane


def _browser_selection_count(browser: Any) -> int:
    table = getattr(browser, "table", None)
    if table is None:
        return 0
    return int(table.len_selection())


def _sync_browser_multi_card_pane(browser: Any) -> None:
    pane = _browser_panes.get(browser)
    if pane is None:
        return

    editor = getattr(browser, "editor", None)
    fields_area = browser.form.fieldsArea
    splitter_widget = browser.form.splitter.widget(1)
    multi_selected = _browser_selection_count(browser) > 1
    if multi_selected:
        if splitter_widget is not None:
            splitter_widget.setVisible(True)
        fields_area.show()
        if editor is not None and getattr(editor, "web", None) is not None:
            editor.web.hide()
        pane.show()
        pane.on_browser_context_changed()
    else:
        pane.hide()
        if editor is not None and getattr(editor, "web", None) is not None:
            editor.web.show()


class PromptEdit(QPlainTextEdit):
    def __init__(
        self,
        send_callback: Callable[[], None],
        history_callback: Callable[[], tuple[str, ...]],
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._send_callback = send_callback
        self._history_callback = history_callback
        self._history_index: int | None = None
        self._draft_text: str | None = None

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._reset_history_navigation()
            self._send_callback()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Up and self._cursor_on_first_line():
            if self._show_previous_history_item():
                event.accept()
                return
        if event.key() == Qt.Key.Key_Down and self._cursor_on_last_line():
            if self._show_next_history_item():
                event.accept()
                return
        if event.key() not in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._reset_history_navigation()
        super().keyPressEvent(event)

    def _cursor_on_first_line(self) -> bool:
        return self.textCursor().blockNumber() == 0

    def _cursor_on_last_line(self) -> bool:
        return self.textCursor().blockNumber() == self.document().blockCount() - 1

    def _show_previous_history_item(self) -> bool:
        history = self._history_callback()
        if not history:
            return False
        if self._history_index is None:
            self._draft_text = self.toPlainText()
            self._history_index = 0
        elif self._history_index < len(history) - 1:
            self._history_index += 1
        self._replace_text(history[self._history_index])
        return True

    def _show_next_history_item(self) -> bool:
        if self._history_index is None:
            return False
        history = self._history_callback()
        if self._history_index > 0 and self._history_index <= len(history):
            self._history_index -= 1
            self._replace_text(history[self._history_index])
            return True
        draft = self._draft_text or ""
        self._reset_history_navigation()
        self._replace_text(draft)
        return True

    def _replace_text(self, text: str) -> None:
        self.setPlainText(text)
        self.moveCursor(QTextCursor.MoveOperation.End)

    def _reset_history_navigation(self) -> None:
        self._history_index = None
        self._draft_text = None


class MultiCardProposalDialog(QDialog):
    def __init__(
        self,
        *,
        parent: QWidget,
        snapshot: MultiCardSnapshot,
        patch: MultiNotePatch,
        apply_callback: Callable[[set[int]], bool],
        discard_callback: Callable[[], None],
    ) -> None:
        super().__init__(parent, Qt.WindowType.Window)
        self.snapshot = snapshot
        self.patch = patch
        self.apply_callback = apply_callback
        self.discard_callback = discard_callback
        self.cards = tuple(
            card
            for card in snapshot.cards
            if patch.update_for_note(card.note_id) is not None
        )
        self.index = 0
        self.checked_note_ids = set(patch.affected_note_ids())

        self.setWindowTitle("Agent proposal")
        self.resize(1000, 720)
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.setLayout(layout)

        top_row = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.page_label = QLabel("")
        qconnect(self.previous_button.clicked, self._previous_card)
        qconnect(self.next_button.clicked, self._next_card)
        top_row.addWidget(self.previous_button)
        top_row.addWidget(self.next_button)
        top_row.addWidget(self.page_label, 1)
        layout.addLayout(top_row)

        self.apply_note_checkbox = QCheckBox("")
        qconnect(self.apply_note_checkbox.toggled, self._on_note_checked)
        layout.addWidget(self.apply_note_checkbox)

        self.surface = AnkiWebView(self)
        self.surface.stdHtml(
            surface_body(),
            js=[
                "js/mathjax.js",
                "js/vendor/mathjax/tex-chtml-full.js",
            ],
            context=self,
        )
        layout.addWidget(self.surface, 1)

        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton(
            "Apply checked",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        self.discard_button = buttons.addButton(
            "Discard",
            QDialogButtonBox.ButtonRole.RejectRole,
        )
        assert self.apply_button is not None
        assert self.discard_button is not None
        qconnect(self.apply_button.clicked, self._apply_checked)
        qconnect(self.discard_button.clicked, self._discard)
        layout.addWidget(buttons)

        self._show_current_card()

    def _current_card(self) -> SelectedCardSnapshot:
        return self.cards[self.index]

    def _show_current_card(self) -> None:
        if not self.cards:
            self.page_label.setText("No affected cards")
            self.apply_note_checkbox.setEnabled(False)
            self.apply_button.setEnabled(False)
            self.surface.eval(js_clear_proposal())
            return

        card = self._current_card()
        note_cards = self.snapshot.cards_for_note(card.note_id)
        self.page_label.setText(f"Card {self.index + 1} of {len(self.cards)}")
        self.previous_button.setEnabled(self.index > 0)
        self.next_button.setEnabled(self.index < len(self.cards) - 1)
        self.apply_note_checkbox.blockSignals(True)
        self.apply_note_checkbox.setText(
            f"Apply changes to note {card.note_id} "
            f"({len(note_cards)} selected card{'s' if len(note_cards) != 1 else ''})"
        )
        self.apply_note_checkbox.setChecked(card.note_id in self.checked_note_ids)
        self.apply_note_checkbox.blockSignals(False)
        self.surface.eval(
            js_set_proposal(
                render_multi_note_card_proposal_diff(
                    self.snapshot,
                    self.patch,
                    card.card_id,
                )
            )
        )

    def _previous_card(self) -> None:
        if self.index > 0:
            self.index -= 1
            self._show_current_card()

    def _next_card(self) -> None:
        if self.index < len(self.cards) - 1:
            self.index += 1
            self._show_current_card()

    def _on_note_checked(self, checked: bool) -> None:
        card = self._current_card()
        if checked:
            self.checked_note_ids.add(card.note_id)
        else:
            self.checked_note_ids.discard(card.note_id)

    def _apply_checked(self) -> None:
        if self.apply_callback(set(self.checked_note_ids)):
            self.accept()

    def _discard(self) -> None:
        self.discard_callback()
        self.reject()


class EditorAgentPane(QWidget):
    def __init__(
        self,
        editor: Editor | None = None,
        *,
        browser: Any | None = None,
        embedded: bool = False,
    ) -> None:
        if editor is None and browser is None:
            raise ValueError("EditorAgentPane requires an editor or browser.")
        parent = editor.parentWindow if editor is not None else browser
        super().__init__(parent)
        self.editor = editor
        self.browser = browser
        self.embedded = embedded
        self.history: list[tuple[str, str]] = []
        self.pending_patch: NotePatch | MultiNotePatch | None = None
        self.pending_snapshot: EditorSnapshot | MultiCardSnapshot | None = None
        self._activity_id: str | None = None
        self._activity_counter = 0
        self._activity_open = False
        self._agent_stop_event: Event | None = None
        self._context_generation = 0
        self._last_browser_note_id = self._current_browser_note_id()
        self._last_browser_multi_card_ids = self._current_browser_multi_card_ids()
        self._selected_text_snapshot: SelectedTextSnapshot | None = None
        self._selection_context_refresh_pending = False
        self._selection_context_request_id = 0
        self._activity_role = "Codex"
        self._model_provider = PROVIDER_CODEX
        self._instructions_scope = (PROVIDER_CODEX, "")
        self._ollama_models: tuple[str, ...] = ()
        self._ollama_models_unavailable = False
        self._loading_settings = False
        self._setting_model_choice = False
        self._instructions_collapsed = False
        self._multi_proposal_dialog: MultiCardProposalDialog | None = None

        self.dock: QDockWidget | None = None
        if not embedded:
            assert editor is not None
            self.dock = QDockWidget("Agent", editor.parentWindow)
            self.dock.setObjectName("EditorAgentPane")
            self.dock.setAllowedAreas(
                Qt.DockWidgetArea.LeftDockWidgetArea
                | Qt.DockWidgetArea.RightDockWidgetArea
            )
            self.dock.setWidget(self)

            dock_parent = editor.parentWindow
            if isinstance(dock_parent, QMainWindow):
                dock_parent.addDockWidget(
                    Qt.DockWidgetArea.RightDockWidgetArea, self.dock
                )
            else:
                self.dock.setFloating(True)
            self.dock.hide()

        self._build_ui()
        self._load_settings()
        self._selection_context_timer = QTimer(self)
        self._selection_context_timer.setInterval(SELECTION_CONTEXT_REFRESH_MS)
        qconnect(
            self._selection_context_timer.timeout,
            self._refresh_selection_context_from_editor,
        )
        if self.dock is not None:
            qconnect(self.dock.visibilityChanged, self._on_dock_visibility_changed)
        qconnect(self.text_splitter.splitterMoved, self._save_splitter_sizes)
        self.refresh_context_label()

    def _build_ui(self) -> None:
        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.setLayout(layout)

        self.context_label = QLabel("")
        self.context_label.setWordWrap(True)
        layout.addWidget(self.context_label)

        layout.addLayout(self._build_settings_form())
        self._add_project_folder_row(layout)

        instructions_header = QHBoxLayout()
        self.instructions_toggle_button = QToolButton()
        self.instructions_toggle_button.setAutoRaise(True)
        qconnect(
            self.instructions_toggle_button.clicked,
            self._toggle_instructions_collapsed,
        )
        instructions_label = QLabel("Instructions")
        self.instructions_reset_button = QPushButton("Reset")
        qconnect(self.instructions_reset_button.clicked, self._reset_custom_instructions)
        instructions_header.addWidget(self.instructions_toggle_button)
        instructions_header.addWidget(instructions_label)
        instructions_header.addStretch(1)
        instructions_header.addWidget(self.instructions_reset_button)
        layout.addLayout(instructions_header)
        self.instructions_edit = QPlainTextEdit()
        self.instructions_edit.setPlaceholderText("Optional custom instructions")
        self.instructions_edit.setMinimumHeight(70)
        self.instructions_edit.setMaximumHeight(110)
        layout.addWidget(self.instructions_edit)

        self.surface = AnkiWebView(self)
        self.surface.stdHtml(
            surface_body(),
            js=[
                "js/mathjax.js",
                "js/vendor/mathjax/tex-chtml-full.js",
            ],
            context=self,
        )

        self.prompt = PromptEdit(
            self._send,
            self._prompt_history_for_current_choice,
            self,
        )
        self.prompt.setPlaceholderText("Message")
        self.prompt.setMinimumHeight(50)
        self.selection_context_label = QLabel("")
        self.selection_context_label.setWordWrap(True)
        self.selection_context_label.setTextFormat(Qt.TextFormat.PlainText)
        self.selection_context_label.setVisible(False)

        prompt_container = QWidget()
        prompt_layout = QVBoxLayout()
        prompt_layout.setContentsMargins(0, 0, 0, 0)
        prompt_layout.setSpacing(4)
        prompt_container.setLayout(prompt_layout)
        prompt_layout.addWidget(self.selection_context_label)
        prompt_layout.addWidget(self.prompt)

        self.text_splitter = QSplitter(Qt.Orientation.Vertical)
        self.text_splitter.setChildrenCollapsible(False)
        self.text_splitter.addWidget(self.surface)
        self.text_splitter.addWidget(prompt_container)
        self.text_splitter.setStretchFactor(0, 6)
        self.text_splitter.setStretchFactor(1, 1)
        self.text_splitter.setSizes(list(DEFAULT_SPLITTER_SIZES))
        layout.addWidget(self.text_splitter, 1)

        action_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply proposal")
        self.apply_button.setEnabled(False)
        qconnect(self.apply_button.clicked, self._apply_pending_patch)
        discard_button = QPushButton("Discard")
        qconnect(discard_button.clicked, self._discard_pending_patch)
        action_row.addWidget(self.apply_button)
        action_row.addWidget(discard_button)
        layout.addLayout(action_row)

        send_row = QHBoxLayout()
        self.reset_chat_button = QPushButton("Reset chat")
        qconnect(self.reset_chat_button.clicked, self._reset_chat)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        qconnect(self.stop_button.clicked, self._stop_running_agent)
        self.send_button = QPushButton("Send")
        qconnect(self.send_button.clicked, self._send)
        send_row.addStretch(1)
        send_row.addWidget(self.reset_chat_button)
        send_row.addWidget(self.stop_button)
        send_row.addWidget(self.send_button)
        layout.addLayout(send_row)

    def _build_settings_form(self) -> QFormLayout:
        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.provider_combo = QComboBox()
        self.provider_combo.setEditable(False)
        for label, value in PROVIDER_OPTIONS:
            self.provider_combo.addItem(label, value)
        qconnect(self.provider_combo.currentIndexChanged, self._on_provider_changed)
        form.addRow("Provider", self.provider_combo)
        self.codex_path_edit = QLineEdit()
        self.codex_path_edit.setPlaceholderText(resolve_codex_path(""))
        self.codex_path_label = QLabel("Codex CLI")
        form.addRow(self.codex_path_label, self.codex_path_edit)
        self.ollama_path_edit = QLineEdit()
        self.ollama_path_edit.setPlaceholderText(resolve_ollama_path(""))
        self.ollama_path_label = QLabel("Ollama CLI")
        form.addRow(self.ollama_path_label, self.ollama_path_edit)
        self.ollama_host_edit = QLineEdit()
        self.ollama_host_edit.setPlaceholderText(DEFAULT_OLLAMA_HOST)
        self.ollama_host_label = QLabel("Ollama host")
        form.addRow(self.ollama_host_label, self.ollama_host_edit)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(False)
        for label, value in MODEL_OPTIONS:
            self.model_combo.addItem(label, value)
        qconnect(self.model_combo.currentIndexChanged, self._on_model_changed)
        model_row = QHBoxLayout()
        self.model_refresh_button = QPushButton("Refresh")
        qconnect(
            self.model_refresh_button.clicked,
            lambda _checked=False: self._refresh_ollama_models(show_error=True),
        )
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.model_refresh_button)
        form.addRow("Model", model_row)
        self.effort_combo = QComboBox()
        self.effort_combo.setEditable(False)
        for label, value in EFFORT_OPTIONS:
            self.effort_combo.addItem(label, value)
        self.effort_label = QLabel("Effort")
        form.addRow(self.effort_label, self.effort_combo)
        self.fast_mode_checkbox = QCheckBox("Fast mode")
        qconnect(self.fast_mode_checkbox.toggled, self._on_fast_mode_toggled)
        self.fast_mode_label = QLabel("")
        form.addRow(self.fast_mode_label, self.fast_mode_checkbox)
        self.access_combo = QComboBox()
        self.access_combo.setEditable(False)
        for label, value in PROJECT_FOLDER_ACCESS_OPTIONS:
            self.access_combo.addItem(label, value)
        qconnect(
            self.access_combo.currentIndexChanged,
            lambda _index: self.refresh_context_label(),
        )
        self.access_label = QLabel("Access")
        form.addRow(self.access_label, self.access_combo)
        self.reasoning_checkbox = QCheckBox("Show reasoning summaries")
        self.reasoning_label = QLabel("")
        form.addRow(self.reasoning_label, self.reasoning_checkbox)
        open_logs = QPushButton("Open logs")
        qconnect(open_logs.clicked, self._open_logs_folder)
        form.addRow("Logs", open_logs)
        return form

    def _add_project_folder_row(self, layout: QVBoxLayout) -> None:
        self.project_row_widget = QWidget()
        project_row = QHBoxLayout()
        project_row.setContentsMargins(0, 0, 0, 0)
        self.project_row_widget.setLayout(project_row)
        self.project_edit = QComboBox()
        self.project_edit.setEditable(True)
        self.project_edit.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        project_line_edit = self.project_edit.lineEdit()
        assert project_line_edit is not None
        project_line_edit.setPlaceholderText("Optional project folder")
        qconnect(
            self.project_edit.currentTextChanged,
            lambda _text: self.refresh_context_label(),
        )
        browse = QPushButton("Browse")
        qconnect(browse.clicked, self._browse_project)
        project_row.addWidget(self.project_edit, 1)
        project_row.addWidget(browse)
        layout.addWidget(self.project_row_widget)

    def _load_settings(self) -> None:
        config = _config()
        self._loading_settings = True
        try:
            self._set_provider_choice(str(config["provider"]))
            self.codex_path_edit.setText(str(config["codex_path"]))
            self.ollama_path_edit.setText(str(config["ollama_path"]))
            self.ollama_host_edit.setText(str(config["ollama_host"]))
            self._set_model_choice(self._saved_model_for_provider(config["provider"], config))
            self._set_effort_choice(str(config["reasoning_effort"]))
            self._set_project_folder_choices(
                str(config["project_folder"]),
                config["recent_project_folders"],
            )
            self._set_project_folder_access(str(config["project_folder_access"]))
            self.fast_mode_checkbox.setChecked(bool(config["fast_mode"]))
            self.reasoning_checkbox.setChecked(
                bool(config["stream_reasoning_summaries"])
            )
            self._load_instructions_for_current_choice(config)
            self._set_instructions_collapsed(bool(config["instructions_collapsed"]))
            self.text_splitter.setSizes(
                _validated_splitter_sizes(config["splitter_sizes"])
            )
            self._update_provider_controls()
        finally:
            self._loading_settings = False
        if self._provider() == PROVIDER_OLLAMA:
            self._refresh_ollama_models()

    def _save_settings(self) -> None:
        config = _config()
        project_folder = self._project_folder_text()
        self._save_current_model_choice(config)
        config["provider"] = self._provider()
        config["codex_path"] = self.codex_path_edit.text().strip()
        config["ollama_path"] = self.ollama_path_edit.text().strip()
        config["ollama_host"] = normalize_ollama_host(self.ollama_host_edit.text())
        config["reasoning_effort"] = self._reasoning_effort()
        self._save_current_instructions(config)
        config["instructions_collapsed"] = self._instructions_are_collapsed()
        config["project_folder"] = project_folder
        config["project_folder_access"] = self._project_folder_access()
        config["fast_mode"] = self._fast_mode()
        config["stream_reasoning_summaries"] = self._stream_reasoning_summaries()
        config["recent_project_folders"] = remember_project_folder(
            project_folder,
            config["recent_project_folders"],
        )
        _write_config(config)
        self._set_project_folder_choices(
            project_folder,
            config["recent_project_folders"],
        )

    def _on_fast_mode_toggled(self, _checked: bool) -> None:
        if getattr(self, "_loading_settings", False):
            return
        self._save_settings()

    def _set_provider_choice(self, provider: str) -> None:
        index = provider_option_index(provider)
        self.provider_combo.setCurrentIndex(index)

    def _set_model_choice(self, model: str) -> None:
        self._setting_model_choice = True
        try:
            self.model_combo.clear()
            provider = self._provider()
            if provider == PROVIDER_OLLAMA:
                for label, value in ollama_model_options_with_legacy(
                    model,
                    self._ollama_models,
                    unavailable=self._ollama_models_unavailable,
                ):
                    self.model_combo.addItem(label, value)
                self.model_combo.setCurrentIndex(
                    ollama_model_option_index(
                        model,
                        self._ollama_models,
                        unavailable=self._ollama_models_unavailable,
                    )
                )
            else:
                for label, value in model_options_with_legacy(model):
                    self.model_combo.addItem(label, value)
                self.model_combo.setCurrentIndex(model_option_index(model))
        finally:
            self._setting_model_choice = False
        self._model_provider = provider

    def _model_text(self) -> str:
        data = self.model_combo.currentData()
        return str(data).strip() if data is not None else ""

    def _provider(self) -> str:
        data = self.provider_combo.currentData()
        return provider_value(data if data is not None else "")

    def _saved_model_for_provider(
        self,
        provider: object,
        config: dict[str, Any],
    ) -> str:
        if provider_value(provider) == PROVIDER_OLLAMA:
            return str(config["ollama_model"])
        return str(config["codex_model"])

    def _save_current_model_choice(self, config: dict[str, Any]) -> None:
        if getattr(self, "_model_provider", PROVIDER_CODEX) == PROVIDER_OLLAMA:
            config["ollama_model"] = self._model_text()
        else:
            config["codex_model"] = self._model_text()

    def _current_instructions_scope(self) -> tuple[str, str]:
        return self._provider(), self._model_text()

    def _load_instructions_for_current_choice(
        self,
        config: dict[str, Any],
    ) -> None:
        provider, model = self._current_instructions_scope()
        self._instructions_scope = (provider, model)
        self.instructions_edit.setPlainText(
            _custom_instructions_for_model(
                config,
                provider=provider,
                model=model,
            )
        )

    def _save_current_instructions(self, config: dict[str, Any]) -> None:
        provider, model = getattr(
            self,
            "_instructions_scope",
            self._current_instructions_scope(),
        )
        _set_custom_instructions_for_model(
            config,
            provider=provider,
            model=model,
            instructions=self._custom_instructions_text(),
        )

    def _prompt_history_for_current_choice(self) -> tuple[str, ...]:
        provider, model = self._provider(), self._model_text()
        return _prompt_history_for_model(_config(), provider=provider, model=model)

    def _set_instructions_collapsed(self, collapsed: bool) -> None:
        self._instructions_collapsed = collapsed
        self.instructions_edit.setVisible(not collapsed)
        self.instructions_reset_button.setVisible(not collapsed)
        self.instructions_toggle_button.setArrowType(
            Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.DownArrow
        )
        self.instructions_toggle_button.setToolTip(
            "Show instructions" if collapsed else "Hide instructions"
        )

    def _instructions_are_collapsed(self) -> bool:
        return bool(getattr(self, "_instructions_collapsed", False))

    def _toggle_instructions_collapsed(self) -> None:
        self._set_instructions_collapsed(not self._instructions_are_collapsed())
        if not getattr(self, "_loading_settings", False):
            self._save_settings()

    def _on_model_changed(self, _index: int) -> None:
        if getattr(self, "_loading_settings", False) or getattr(
            self, "_setting_model_choice", False
        ):
            return
        config = _config()
        self._save_current_instructions(config)
        self._save_current_model_choice(config)
        config["provider"] = self._provider()
        _write_config(config)
        self._load_instructions_for_current_choice(config)
        self.refresh_context_label()

    def _on_provider_changed(self, _index: int) -> None:
        config = _config()
        if not getattr(self, "_loading_settings", False):
            self._save_current_instructions(config)
            self._save_current_model_choice(config)
            config["provider"] = self._provider()
            _write_config(config)
        self._set_model_choice(self._saved_model_for_provider(self._provider(), config))
        self._load_instructions_for_current_choice(config)
        self._update_provider_controls()
        self.refresh_context_label()
        if not getattr(self, "_loading_settings", False) and self._provider() == PROVIDER_OLLAMA:
            self._refresh_ollama_models()

    def _update_provider_controls(self) -> None:
        codex_enabled = self._provider() == PROVIDER_CODEX
        self._set_form_row_visible(
            self.codex_path_label, self.codex_path_edit, codex_enabled
        )
        self._set_form_row_visible(
            self.ollama_path_label, self.ollama_path_edit, not codex_enabled
        )
        self._set_form_row_visible(
            self.ollama_host_label, self.ollama_host_edit, not codex_enabled
        )
        self.model_refresh_button.setVisible(not codex_enabled)
        self._set_form_row_visible(self.effort_label, self.effort_combo, codex_enabled)
        self._set_form_row_visible(
            self.fast_mode_label, self.fast_mode_checkbox, codex_enabled
        )
        self._set_form_row_visible(self.access_label, self.access_combo, codex_enabled)
        self._set_form_row_visible(
            self.reasoning_label, self.reasoning_checkbox, codex_enabled
        )
        self.project_row_widget.setVisible(codex_enabled)
        self.codex_path_edit.setEnabled(codex_enabled)
        self.ollama_path_edit.setEnabled(not codex_enabled)
        self.ollama_host_edit.setEnabled(not codex_enabled)
        self.model_refresh_button.setEnabled(not codex_enabled)
        self.effort_combo.setEnabled(codex_enabled)
        self.fast_mode_checkbox.setEnabled(codex_enabled)
        self.access_combo.setEnabled(codex_enabled)
        self.reasoning_checkbox.setEnabled(codex_enabled)
        self.project_row_widget.setEnabled(codex_enabled)

    def _set_form_row_visible(
        self,
        label: QWidget,
        field: QWidget,
        visible: bool,
    ) -> None:
        label.setVisible(visible)
        field.setVisible(visible)

    def _refresh_ollama_models(self, show_error: bool = False) -> None:
        if self._provider() != PROVIDER_OLLAMA:
            return
        assert aqt.mw is not None
        taskman = aqt.mw.taskman
        ollama_path = self.ollama_path_edit.text().strip()
        ollama_host = self.ollama_host_edit.text().strip()

        def task() -> tuple[tuple[str, ...], str | None]:
            try:
                models = discover_ollama_models(
                    ollama_path=ollama_path,
                    ollama_host=ollama_host,
                )
            except Exception as exc:
                return (), str(exc)
            return ollama_model_names(models), None

        def on_done(future: Future) -> None:
            model_names, error = future.result()
            config = _config()
            if error is None:
                self._ollama_models = model_names
                self._ollama_models_unavailable = False
                if not str(config["ollama_model"]).strip() and model_names:
                    config["ollama_model"] = model_names[0]
                    _write_config(config)
            else:
                self._ollama_models = ()
                self._ollama_models_unavailable = True
            if self._provider() == PROVIDER_OLLAMA:
                model_changed = self._model_text() != str(config["ollama_model"])
                if model_changed:
                    self._save_current_instructions(config)
                self._set_model_choice(str(config["ollama_model"]))
                if model_changed:
                    self._load_instructions_for_current_choice(config)
                    _write_config(config)
                if error is not None and show_error:
                    tooltip(str(error), parent=self)

        taskman.run_in_background(task, on_done, uses_collection=False)

    def _set_effort_choice(self, effort: str) -> None:
        self.effort_combo.clear()
        for label, value in effort_options_with_legacy(effort):
            self.effort_combo.addItem(label, value)
        self.effort_combo.setCurrentIndex(effort_option_index(effort))

    def _reasoning_effort(self) -> str:
        data = self.effort_combo.currentData()
        return effort_value(data) if data is not None else ""

    def _set_project_folder_access(self, project_folder_access: str) -> None:
        access = normalize_project_folder_access(project_folder_access)
        for index in range(self.access_combo.count()):
            if self.access_combo.itemData(index) == access:
                self.access_combo.setCurrentIndex(index)
                return
        self.access_combo.setCurrentIndex(0)

    def _project_folder_access(self) -> str:
        data = self.access_combo.currentData()
        return normalize_project_folder_access(str(data) if data is not None else "")

    def _fast_mode(self) -> bool:
        return self.fast_mode_checkbox.isChecked()

    def _stream_reasoning_summaries(self) -> bool:
        return self.reasoning_checkbox.isChecked()

    def _set_project_folder_choices(
        self,
        project_folder: str,
        recent_folders: Any,
    ) -> None:
        self.project_edit.clear()
        self.project_edit.addItems(
            project_folder_choices(project_folder, recent_folders)
        )
        if project_folder.strip():
            self.project_edit.setEditText(project_folder.strip())
        else:
            self.project_edit.setCurrentIndex(0)

    def _project_folder_text(self) -> str:
        project_folder = self.project_edit.currentText().strip()
        if project_folder == NO_PROJECT_FOLDER_LABEL:
            return ""
        return project_folder

    def _custom_instructions_text(self) -> str:
        return self.instructions_edit.toPlainText().strip()

    def _reset_custom_instructions(self) -> None:
        self.instructions_edit.clear()
        self._save_settings()

    def _save_splitter_sizes(self, _pos: int, _index: int) -> None:
        config = _config()
        config["splitter_sizes"] = self.text_splitter.sizes()
        _write_config(config)

    def _browse_project(self) -> None:
        start = self._project_folder_text() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select project folder",
            start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self.project_edit.setEditText(folder)
            self._save_settings()
            self.refresh_context_label()

    def _open_logs_folder(self) -> None:
        assert aqt.mw is not None
        path = ensure_agent_log_folder(aqt.mw.addonManager, ADDON)
        openFolder(str(path))

    def toggle(self) -> None:
        if self.dock is None:
            self.setVisible(not self.isVisible())
            if self.isVisible():
                self.prompt.setFocus()
                self.refresh_context_label()
                if self._provider() == PROVIDER_OLLAMA:
                    self._refresh_ollama_models()
            return
        self.dock.setVisible(not self.dock.isVisible())
        if self.dock.isVisible():
            self.prompt.setFocus()
            self.refresh_context_label()
            if self._provider() == PROVIDER_OLLAMA:
                self._refresh_ollama_models()
            self._start_selection_context_refresh()
        else:
            self._stop_selection_context_refresh()

    def refresh_context_label(self) -> None:
        if self.browser is not None:
            try:
                snapshot = browser_multi_card_snapshot(self.browser)
            except RuntimeError:
                self.context_label.setText("Select multiple cards to use the agent.")
                return
            self.context_label.setText(
                f"browse - {len(snapshot.cards)} selected cards - "
                f"{len(snapshot.notes)} notes\n{self._agent_context_status()}"
            )
            return

        assert self.editor is not None
        note = self.editor.note
        if not note:
            self.context_label.setText("No current note.")
            return
        try:
            notetype = self.editor.note_type()
            notetype_name = str(notetype.get("name", ""))
        except Exception:
            notetype_name = str(note.mid)
        mode = _editor_mode_name(self.editor)
        self.context_label.setText(
            f"{mode} - note {int(note.id) if note.id else 'new'} - "
            f"{notetype_name}\n{self._agent_context_status()}"
        )

    def _agent_context_status(self) -> str:
        if self._provider() == PROVIDER_OLLAMA:
            model = self._model_text()
            if model:
                return f"Local Ollama model: {model}"
            return "No Ollama model selected."
        return project_root_status(
            self._project_folder_text(),
            self._project_folder_access(),
        )

    def on_editor_context_changed(self) -> None:
        self.refresh_context_label()
        self._set_selected_text_snapshot(None)
        if self.editor is None:
            return
        if self.editor.editorMode is not EditorMode.BROWSER:
            return
        note_id = self._current_browser_note_id()
        if note_id == self._last_browser_note_id:
            return
        self._last_browser_note_id = note_id
        self._clear_chat_context()

    def on_browser_context_changed(self) -> None:
        self.refresh_context_label()
        card_ids = self._current_browser_multi_card_ids()
        if card_ids == self._last_browser_multi_card_ids:
            return
        self._last_browser_multi_card_ids = card_ids
        self._clear_chat_context()

    def _current_browser_note_id(self) -> int | None:
        if self.editor is None:
            return None
        if self.editor.editorMode is not EditorMode.BROWSER:
            return None
        note = self.editor.note
        if not note or not note.id:
            return None
        return int(note.id)

    def _current_browser_multi_card_ids(self) -> tuple[int, ...]:
        browser = self.browser
        if browser is None or _browser_selection_count(browser) <= 1:
            return ()
        try:
            return tuple(int(card_id) for card_id in browser.selected_cards())
        except Exception:
            return ()

    def _reset_chat(self) -> None:
        self._clear_chat_context()

    def _clear_chat_context(self) -> None:
        self._context_generation += 1
        if self._agent_stop_event is not None:
            self._agent_stop_event.set()
            self._agent_stop_event = None
        self.history.clear()
        self.pending_patch = None
        self.pending_snapshot = None
        self._activity_id = None
        self._activity_open = False
        self._set_selected_text_snapshot(None)
        self.send_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.surface.eval(js_clear_transcript())
        self._clear_proposal()

    def _on_dock_visibility_changed(self, visible: bool) -> None:
        if visible:
            self._start_selection_context_refresh()
        else:
            self._stop_selection_context_refresh()

    def _pane_is_visible(self) -> bool:
        if self.dock is not None:
            return self.dock.isVisible()
        return self.isVisible()

    def _start_selection_context_refresh(self) -> None:
        self._refresh_selection_context_from_editor()
        self._selection_context_timer.start()

    def _stop_selection_context_refresh(self) -> None:
        self._selection_context_request_id += 1
        self._selection_context_timer.stop()
        self._selection_context_refresh_pending = False
        self._set_selected_text_snapshot(None)

    def _query_selected_text_context(self, callback: Callable[[Any], None]) -> None:
        web = getattr(self.editor, "web", None)
        if web is None:
            callback(None)
            return
        web.evalWithCallback(SELECTION_CONTEXT_JS, callback)

    def _refresh_selection_context_from_editor(self) -> None:
        if self._selection_context_refresh_pending:
            return
        if not self._pane_is_visible() or self.editor is None:
            self._set_selected_text_snapshot(None)
            return
        self._selection_context_refresh_pending = True
        self._selection_context_request_id += 1
        request_id = self._selection_context_request_id
        generation = self._context_generation
        self._query_selected_text_context(
            lambda selected_text: self._handle_selection_context_result(
                request_id,
                generation,
                selected_text,
            )
        )

    def _handle_selection_context_result(
        self,
        request_id: int,
        generation: int,
        selected_text: Any,
    ) -> None:
        self._selection_context_refresh_pending = False
        if (
            request_id != self._selection_context_request_id
            or generation != self._context_generation
            or not self._pane_is_visible()
        ):
            return
        if self.editor is None:
            self._set_selected_text_snapshot(None)
        else:
            self._set_selected_text_snapshot(
                validated_selected_text_for_editor(self.editor, selected_text)
            )

    def _set_selected_text_snapshot(
        self,
        snapshot: SelectedTextSnapshot | None,
    ) -> None:
        self._selected_text_snapshot = snapshot
        if snapshot is None:
            self.selection_context_label.clear()
            self.selection_context_label.setVisible(False)
            return
        self.selection_context_label.setText(
            f"Selection context: {selection_context_label_text(snapshot)}"
        )
        self.selection_context_label.setVisible(True)

    def _append_transcript(self, fragment: str) -> None:
        self.surface.eval(js_append_transcript(fragment))

    def _replace_activity_with_summary(
        self, summary: str, detail_lines: tuple[str, ...]
    ) -> None:
        if self._activity_id:
            self.surface.eval(
                js_replace_element(
                    self._activity_id,
                    render_activity_summary(
                        self._activity_id,
                        summary,
                        detail_lines,
                        role=getattr(self, "_activity_role", "Codex"),
                    ),
                )
            )
        self._activity_id = None
        self._activity_open = False

    def _append_activity_line(self, line: str) -> None:
        if self._activity_open and self._activity_id:
            self.surface.eval(
                js_append_to_activity(self._activity_id, render_activity_line(line))
            )

    def _append_activity_line_if_current(self, generation: int, line: str) -> None:
        if generation == self._context_generation:
            self._append_activity_line(line)

    def _clear_proposal(self) -> None:
        self.surface.eval(js_clear_proposal())

    def _set_proposal(
        self,
        snapshot: EditorSnapshot,
        patch: NotePatch,
        notetype: dict[str, Any],
    ) -> None:
        assert self.editor is not None
        renderer = LegacyLatexPreviewRenderer(col=self.editor.mw.col, notetype=notetype)
        self.surface.eval(
            js_set_proposal(render_proposal_diff(snapshot, patch, renderer.render))
        )

    def _send(self) -> None:
        if self._agent_stop_event is not None:
            return
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        self.prompt.clear()
        self._save_settings()
        generation = self._context_generation
        self._query_selected_text_context(
            lambda selected_text: self._send_after_selection_snapshot(
                prompt,
                generation,
                selected_text,
            ),
        )

    def _send_after_selection_snapshot(
        self,
        prompt: str,
        generation: int,
        selected_text: Any,
    ) -> None:
        if generation != self._context_generation:
            return
        if self.editor is None:
            self._start_agent_request(prompt, generation, selected_text)
            return
        self.editor.call_after_note_saved(
            lambda: self._start_agent_request(prompt, generation, selected_text),
            keepFocus=True,
        )

    def _start_agent_request(
        self,
        prompt: str,
        generation: int | None = None,
        selected_text: Any = None,
    ) -> None:
        if generation is None:
            generation = self._context_generation
        if generation != self._context_generation:
            return
        try:
            browser = getattr(self, "browser", None)
            if browser is not None:
                snapshot = browser_multi_card_snapshot(browser)
                notetype: dict[str, Any] = {}
            else:
                assert self.editor is not None
                snapshot = editor_snapshot(self.editor, selected_text)
                notetype = dict(self.editor.note_type())
        except RuntimeError as exc:
            showWarning(str(exc), parent=self)
            return
        selected_snapshot = (
            snapshot.selected_text if isinstance(snapshot, EditorSnapshot) else None
        )

        config = _config()
        provider = self._provider()
        model = self._model_text() or self._saved_model_for_provider(provider, config)
        prompt_history_scope = (provider, model)
        if provider == PROVIDER_OLLAMA and not model:
            showWarning("Select an Ollama model first.", parent=self)
            return
        provider_label = "Ollama" if provider == PROVIDER_OLLAMA else "Codex"
        reasoning_effort = self._reasoning_effort() if provider == PROVIDER_CODEX else ""
        project_root = self._project_folder_text()
        project_folder_access = self._project_folder_access()
        custom_instructions = self._custom_instructions_text()
        codex_path = self.codex_path_edit.text().strip() or str(config["codex_path"])
        ollama_path = self.ollama_path_edit.text().strip() or str(config["ollama_path"])
        ollama_host = self.ollama_host_edit.text().strip() or str(config["ollama_host"])
        fast_mode = self._fast_mode() if provider == PROVIDER_CODEX else False
        stream_reasoning_summaries = (
            self._stream_reasoning_summaries() if provider == PROVIDER_CODEX else False
        )
        self._set_selected_text_snapshot(selected_snapshot)
        self._append_transcript(render_user_message(prompt, selected_snapshot))
        stop_event = Event()
        history = list(self.history)
        started_at = time.monotonic()
        self._agent_stop_event = stop_event
        self.send_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.apply_button.setEnabled(False)
        self.pending_patch = None
        self.pending_snapshot = None
        self._clear_proposal()
        self._activity_counter += 1
        self._activity_id = f"agent-activity-{self._activity_counter}"
        self._activity_open = True
        self._activity_role = provider_label
        self._append_transcript(
            render_activity_start(
                self._activity_id,
                role=provider_label,
                status=(
                    "[Live Codex activity]"
                    if provider == PROVIDER_CODEX
                    else "[Running local Ollama model]"
                ),
            )
        )
        activity = CodexActivityRenderer(
            stream_reasoning_summaries=stream_reasoning_summaries
        )
        assert aqt.mw is not None
        taskman = aqt.mw.taskman
        run_logger = JsonLineAgentRunLogger(aqt.mw.addonManager.get_logger(ADDON))

        def on_stream_event(event: dict[str, Any]) -> None:
            line = activity.record(event)
            if line is not None:
                taskman.run_on_main(
                    lambda line=line, generation=generation: (
                        self._append_activity_line_if_current(generation, line)
                    )
                )

        def task() -> tuple[
            str,
            str,
            tuple[NotePatch | MultiNotePatch, ...],
            str,
            tuple[str, ...],
        ]:
            if provider == PROVIDER_OLLAMA:
                agent = OllamaCliAgent(
                    ollama_path=ollama_path,
                    ollama_host=ollama_host,
                    model=model,
                    timeout_seconds=int(config["timeout_seconds"]),
                    custom_instructions=custom_instructions,
                )
                result = agent.send(
                    prompt=prompt,
                    snapshot=snapshot,
                    project_root="",
                    history=history,
                    run_logger=run_logger,
                    stop_requested=stop_event.is_set,
                )
                return (
                    result.text,
                    result.html,
                    result.proposals,
                    "[Ollama activity: local model run]\n",
                    (),
                )

            agent = CodexCliAgent(
                codex_path=codex_path,
                model=model,
                timeout_seconds=int(config["timeout_seconds"]),
                project_folder_access=project_folder_access,
                custom_instructions=custom_instructions,
                fast_mode=fast_mode,
                reasoning_effort=reasoning_effort,
                stream_reasoning_summaries=stream_reasoning_summaries,
            )
            result = agent.send(
                prompt=prompt,
                snapshot=snapshot,
                project_root=project_root,
                history=history,
                event_callback=on_stream_event,
                run_logger=run_logger,
                stop_requested=stop_event.is_set,
            )
            return (
                result.text,
                result.html,
                result.proposals,
                activity.compact_summary(),
                tuple(activity.detail_lines),
            )

        def on_done(future: Future) -> None:
            self._handle_agent_done(
                future,
                generation=generation,
                stop_event=stop_event,
                prompt=prompt,
                snapshot=snapshot,
                notetype=notetype,
                activity=activity,
                started_at=started_at,
                prompt_history_scope=prompt_history_scope,
            )

        taskman.run_in_background(task, on_done, uses_collection=False)

    def _handle_agent_done(
        self,
        future: Future,
        *,
        generation: int,
        stop_event: Event,
        prompt: str,
        snapshot: EditorSnapshot | MultiCardSnapshot,
        notetype: dict[str, Any],
        activity: CodexActivityRenderer,
        started_at: float | None = None,
        prompt_history_scope: tuple[str, str] | None = None,
    ) -> None:
        if generation != self._context_generation:
            return
        elapsed_seconds = (
            time.monotonic() - started_at if started_at is not None else None
        )
        if self._agent_stop_event is stop_event:
            self._agent_stop_event = None
            self.send_button.setEnabled(True)
            self.stop_button.setEnabled(False)
        activity_role = getattr(self, "_activity_role", "Codex")
        try:
            (
                text,
                message_html,
                proposals,
                activity_summary,
                activity_details,
            ) = future.result()
        except AgentStopped:
            summary = (
                _activity_status_summary(
                    "stopped",
                    elapsed_seconds=elapsed_seconds,
                    role=activity_role,
                )
                if elapsed_seconds is not None
                else f"{activity_role} run stopped."
            )
            self._replace_activity_with_summary(
                summary,
                tuple(activity.detail_lines),
            )
            return
        except Exception as exc:
            if elapsed_seconds is not None:
                self._replace_activity_with_summary(
                    _activity_status_summary(
                        "failed",
                        elapsed_seconds=elapsed_seconds,
                        role=activity_role,
                    ),
                    tuple(activity.detail_lines),
                )
            else:
                self._activity_open = False
            self._append_transcript(render_error_message(str(exc)))
            return
        if elapsed_seconds is not None:
            activity_summary = _activity_summary_with_elapsed(
                activity_summary,
                elapsed_seconds=elapsed_seconds,
            )
        self._replace_activity_with_summary(activity_summary, activity_details)
        if text or message_html:
            self._append_transcript(render_assistant_message(message_html, text))
        self.history.append((prompt, text))
        if prompt_history_scope is not None:
            config = _config()
            _record_prompt_history_for_model(
                config,
                provider=prompt_history_scope[0],
                model=prompt_history_scope[1],
                prompt=prompt,
            )
            _write_config(config)
        if proposals:
            self.pending_patch = proposals[-1]
            self.pending_snapshot = snapshot
            if isinstance(snapshot, MultiCardSnapshot) and isinstance(
                proposals[-1], MultiNotePatch
            ):
                self._set_multi_proposal(snapshot, proposals[-1])
            else:
                assert isinstance(snapshot, EditorSnapshot)
                assert isinstance(proposals[-1], NotePatch)
                self._set_proposal(snapshot, proposals[-1], notetype)
            self.apply_button.setEnabled(True)

    def _stop_running_agent(self) -> None:
        if self._agent_stop_event is None:
            return
        self._agent_stop_event.set()
        self.stop_button.setEnabled(False)
        self._append_activity_line("[status] stopping agent")

    def _discard_pending_patch(self) -> None:
        self.pending_patch = None
        self.pending_snapshot = None
        self._clear_proposal()
        self.apply_button.setEnabled(False)
        if hasattr(self.apply_button, "setText"):
            self.apply_button.setText("Apply proposal")

    def _apply_pending_patch(self) -> None:
        patch = self.pending_patch
        if patch is None:
            return
        if isinstance(patch, MultiNotePatch):
            snapshot = self.pending_snapshot
            if not isinstance(snapshot, MultiCardSnapshot):
                return
            self._show_multi_proposal_dialog(snapshot, patch)
            return
        if self.editor is None:
            return
        generation = self._context_generation
        self.editor.call_after_note_saved(
            lambda: self._apply_patch_after_save(patch, generation),
            keepFocus=True,
        )

    def _apply_patch_after_save(self, patch: NotePatch, generation: int) -> None:
        if generation != self._context_generation:
            return
        try:
            apply_patch_to_editor(self.editor, patch)
        except PatchValidationError as exc:
            showWarning(str(exc), parent=self)
            return
        self._discard_pending_patch()
        tooltip("Agent proposal applied.", parent=self)

    def _set_multi_proposal(
        self,
        snapshot: MultiCardSnapshot,
        patch: MultiNotePatch,
    ) -> None:
        card_ids = multi_note_patch_card_ids(snapshot, patch)
        if not card_ids:
            self._clear_proposal()
            self.apply_button.setEnabled(False)
            return
        self.surface.eval(
            js_set_proposal(
                render_multi_note_card_proposal_diff(
                    snapshot,
                    patch,
                    card_ids[0],
                )
            )
        )
        if hasattr(self.apply_button, "setText"):
            self.apply_button.setText("Review proposal")
        self._show_multi_proposal_dialog(snapshot, patch)

    def _show_multi_proposal_dialog(
        self,
        snapshot: MultiCardSnapshot,
        patch: MultiNotePatch,
    ) -> None:
        if self._multi_proposal_dialog is not None:
            self._multi_proposal_dialog.raise_()
            self._multi_proposal_dialog.activateWindow()
            return
        dialog = MultiCardProposalDialog(
            parent=self,
            snapshot=snapshot,
            patch=patch,
            apply_callback=self._apply_checked_multi_patch,
            discard_callback=self._discard_pending_patch,
        )
        self._multi_proposal_dialog = dialog

        def on_finished(_result: int) -> None:
            self._multi_proposal_dialog = None

        qconnect(dialog.finished, on_finished)
        dialog.show()

    def _apply_checked_multi_patch(self, checked_note_ids: set[int]) -> bool:
        snapshot = self.pending_snapshot
        patch = self.pending_patch
        if not isinstance(snapshot, MultiCardSnapshot) or not isinstance(
            patch, MultiNotePatch
        ):
            return False
        browser = self.browser
        if browser is None:
            return False
        try:
            notes = prepare_multi_note_updates(
                browser.col,
                snapshot,
                patch,
                checked_note_ids,
            )
        except PatchValidationError as exc:
            showWarning(str(exc), parent=self)
            return False
        if not notes:
            showWarning("No checked cards have proposed changes.", parent=self)
            return False

        def on_success(_changes: Any) -> None:
            self._discard_pending_patch()
            tooltip("Agent proposal applied.", parent=self)

        update_notes(parent=self, notes=notes).success(on_success).run_in_background(
            initiator=self
        )
        return True


def editor_field_snapshots(editor: Editor) -> tuple[FieldSnapshot, ...]:
    note = editor.note
    if note is None:
        raise RuntimeError("No current note.")
    return tuple(
        FieldSnapshot(name=str(name), html=str(html)) for name, html in note.items()
    )


def validated_selected_text_for_editor(
    editor: Editor,
    selected_text: Any,
) -> SelectedTextSnapshot | None:
    try:
        fields = editor_field_snapshots(editor)
    except RuntimeError:
        return None
    return validate_selected_text_snapshot(selected_text, fields)


def editor_snapshot(editor: Editor, selected_text: Any = None) -> EditorSnapshot:
    note = editor.note
    if note is None:
        raise RuntimeError("No current note.")
    notetype = editor.note_type()
    fields = editor_field_snapshots(editor)
    current_field = None
    if editor.currentField is not None and 0 <= editor.currentField < len(fields):
        current_field = fields[editor.currentField].name

    card_id = None
    if editor.card is not None:
        card_id = int(editor.card.id)

    images = collect_note_images(editor.mw.col.media, int(note.mid), fields)

    return EditorSnapshot(
        mode=_editor_mode_name(editor),
        note_id=int(note.id) if note.id else None,
        notetype_id=int(note.mid),
        notetype_name=str(notetype.get("name", "")),
        fields=fields,
        tags=tuple(note.tags),
        current_field=current_field,
        card_id=card_id,
        images=images,
        selected_text=validate_selected_text_snapshot(selected_text, fields),
    )


def browser_multi_card_snapshot(browser: Any) -> MultiCardSnapshot:
    card_ids = tuple(int(card_id) for card_id in browser.selected_cards())
    if len(card_ids) <= 1:
        raise RuntimeError("Select multiple cards to use the agent.")

    cards: list[SelectedCardSnapshot] = []
    notes: list[SelectedNoteSnapshot] = []
    seen_note_ids: set[int] = set()
    for card_id in card_ids:
        try:
            card = browser.col.get_card(card_id)
            note = card.note()
            notetype = card.note_type()
            template = card.template()
        except Exception as exc:
            raise RuntimeError("A selected card could not be loaded.") from exc

        notetype_id = int(note.mid)
        notetype_name = str(notetype.get("name", notetype_id))
        deck_id = (
            int(card.current_deck_id()) if hasattr(card, "current_deck_id") else None
        )
        deck_name = None
        if deck_id is not None:
            try:
                deck_name = str(browser.col.decks.name(card.current_deck_id()))
            except Exception:
                deck_name = str(deck_id)
        cards.append(
            SelectedCardSnapshot(
                card_id=card_id,
                note_id=int(note.id),
                notetype_id=notetype_id,
                notetype_name=notetype_name,
                ord=int(card.ord),
                template_name=str(template.get("name", f"Card {int(card.ord) + 1}")),
                deck_id=deck_id,
                deck_name=deck_name,
            )
        )

        if int(note.id) not in seen_note_ids:
            seen_note_ids.add(int(note.id))
            notes.append(
                SelectedNoteSnapshot(
                    note_id=int(note.id),
                    notetype_id=notetype_id,
                    notetype_name=notetype_name,
                    fields=tuple(
                        FieldSnapshot(name=str(name), html=str(html))
                        for name, html in note.items()
                    ),
                    tags=tuple(note.tags),
                )
            )

    return MultiCardSnapshot(cards=tuple(cards), notes=tuple(notes))


def apply_patch_to_editor(editor: Editor, patch: NotePatch) -> None:
    note = editor.note
    if note is None:
        raise PatchValidationError("No current note.")
    if editor.web is None:
        raise PatchValidationError("Editor webview is not available.")
    if patch.note_id not in (None, int(note.id) if note.id else None):
        raise PatchValidationError("The current note has changed since the proposal.")
    if patch.notetype_id != int(note.mid):
        raise PatchValidationError(
            "The current note type has changed since the proposal."
        )

    names = [str(name) for name, _html in note.items()]
    field_updates = []
    for field_name, html in patch.field_updates.items():
        try:
            index = names.index(field_name)
        except ValueError as exc:
            raise PatchValidationError(f"Unknown field: {field_name}.") from exc
        field_updates.append({"index": index, "html": html})
        note.fields[index] = html

    tags = None
    if patch.tag_patch.has_changes():
        tags = list(patch.tag_patch.apply(tuple(note.tags)))
        note.tags = tags

    editor.web.eval(js_apply_agent_proposal(field_updates, tags))
    if field_updates:
        editor.checkValid()

    if editor.editorMode is not EditorMode.ADD_CARDS:
        update_note(parent=editor.widget, note=note).run_in_background(initiator=editor)


def prepare_multi_note_updates(
    col: Any,
    snapshot: MultiCardSnapshot,
    patch: MultiNotePatch,
    checked_note_ids: set[int],
) -> list[Any]:
    notes = []
    for update in patch.note_updates:
        if update.note_id not in checked_note_ids:
            continue
        snapshot_note = snapshot.note_by_id(update.note_id)
        try:
            note = col.get_note(update.note_id)
        except Exception as exc:
            raise PatchValidationError(
                f"Selected note {update.note_id} no longer exists."
            ) from exc
        if int(note.mid) != snapshot_note.notetype_id:
            raise PatchValidationError(
                f"Selected note {update.note_id} has changed note type."
            )

        current_fields = tuple(
            FieldSnapshot(name=str(name), html=str(html)) for name, html in note.items()
        )
        current_by_name = {field.name: field.html for field in current_fields}
        field_names = [field.name for field in current_fields]
        for field_name, html in update.field_updates.items():
            if field_name not in current_by_name:
                raise PatchValidationError(f"Unknown field: {field_name}.")
            if current_by_name[field_name] != snapshot_note.field_html(field_name):
                raise PatchValidationError(
                    f"Selected note {update.note_id} has changed since the proposal."
                )
            note.fields[field_names.index(field_name)] = html

        if update.tag_patch.has_changes():
            if tuple(note.tags) != snapshot_note.tags:
                raise PatchValidationError(
                    f"Selected note {update.note_id} tags have changed since the proposal."
                )
            note.tags = list(update.tag_patch.apply(tuple(note.tags)))

        notes.append(note)
    return notes


def _editor_mode_name(editor: Editor) -> str:
    if editor.editorMode is EditorMode.ADD_CARDS:
        return "add"
    if editor.editorMode is EditorMode.BROWSER:
        return "browse"
    return "review"
