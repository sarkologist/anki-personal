# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
import weakref
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from typing import Any

import aqt
from aqt import gui_hooks
from aqt.editor import Editor, EditorMode
from aqt.operations.note import update_note
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
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
    Qt,
    QTimer,
    QToolButton,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import openFolder, showWarning, tooltip
from aqt.webview import AnkiWebView

from . import queue_runtime, queue_window
from .agent_log import ensure_agent_log_folder
from .card_access import (
    CARD_ACCESS_OPTIONS,
    CardReadScope,
    normalize_card_access,
)
from .claude_client import resolve_claude_path
from .codex_client import (
    DEFAULT_PROJECT_FOLDER_ACCESS,
    PROJECT_FOLDER_ACCESS_READ_ONLY,
    PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE,
    normalize_project_folder_access,
    project_root_status,
    resolve_codex_path,
)
from .effort_options import (
    EFFORT_OPTIONS,
    claude_effort_options,
    codex_effort_options,
    effort_choice_for_options,
    effort_option_index,
    effort_options_with_legacy,
    effort_supported_by_options,
    effort_value,
)
from .model_options import (
    CLAUDE_MODEL_OPTIONS,
    DEFAULT_PROVIDER,
    KNOWN_PROVIDERS,
    MODEL_OPTIONS,
    PROVIDER_CLAUDE,
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
from .queue_store import (
    APPLIED,
    READY,
    Proposal,
    QueueTransitionError,
    Thread,
)
from .recent_folders import (
    NO_PROJECT_FOLDER_LABEL,
    project_folder_choices,
    remember_project_folder,
)
from .snapshots import Patch, Snapshot
from .surface import (
    js_apply_agent_proposal,
    selection_context_label_text,
    surface_body,
)
from .thread_panel import (
    MultiCardProposalDialog,
    PromptEdit,
    ThreadPanel,
    prepare_multi_note_updates,  # noqa: F401 (re-exported)
)
from .thread_view import (
    ACTIVE_STATUSES,
    REPLYABLE_STATUSES,
    REVIEWER_SELECTION_JS,
    SOURCE_ADD_CARDS,
    SOURCE_BROWSER,
    SOURCE_BROWSER_MULTI,
    SOURCE_EDIT_CURRENT,
    pick_thread,
    reviewer_card_html,
    selected_text_of,
    snapshot_card_ids,
    stale_reason,
    thread_context,
    thread_label,
    unsaved_note_stale_reason,
)
from .ui_text import AGENT_BUTTON_LABEL, AGENT_BUTTON_TIP, AGENT_PANE_SHORTCUT

ADDON = "editor_agent_pane"
TOGGLE_COMMAND = "editorAgentPane"
DEFAULT_SPLITTER_SIZES = [570, 80]
PROMPT_HISTORY_LIMIT = 10
SELECTION_CONTEXT_REFRESH_MS = 500
PROMPT_NOT_SENT = "The note changed before the request was sent, so it was not sent."
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
    "claude_path": "",
    "claude_model": "",
    "reasoning_effort": "",
    "custom_instructions": "",
    "custom_instructions_by_model": {},
    "instructions_collapsed": False,
    "prompt_history_by_model": {},
    "project_folder": "",
    "project_folder_access": DEFAULT_PROJECT_FOLDER_ACCESS,
    "card_access": "all",
    "recent_project_folders": [],
    "fast_mode": False,
    "stream_reasoning_summaries": True,
    "timeout_seconds": 300,
    "splitter_sizes": DEFAULT_SPLITTER_SIZES,
    "queue_concurrency": 1,
    "queue_notify": True,
    "queue_retention_days": 30,
    "queue_shortcut": queue_window.DEFAULT_SHORTCUT,
    "queue_window_geometry": "",
    "queue_window_splitter": [],
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
    gui_hooks.add_cards_did_add_note.append(_on_add_cards_did_add_note)
    queue_runtime.install(addon=ADDON, config=_config, panes=_all_panes)
    queue_window.install(config=_config, write_config=_write_config)


def _all_panes() -> Iterator[Any]:
    """Everything that shows queue threads: panes and the queue window."""
    yield from list(_panes.values())
    yield from list(_browser_panes.values())
    yield from queue_window.open_windows()


def _config() -> dict[str, Any]:
    assert aqt.mw is not None
    saved = aqt.mw.addonManager.getConfig(ADDON) or {}
    config = {key: saved.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    if "codex_model" not in saved and "model" in saved:
        config["codex_model"] = str(saved["model"]).strip()
    if "codex_path" not in saved and config["codex_model"] == "gpt-5.2":
        config["codex_model"] = ""
    config["provider"] = provider_value(config["provider"])
    config["card_access"] = normalize_card_access(config["card_access"])
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
        if provider_key not in KNOWN_PROVIDERS:
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
        if provider_key not in KNOWN_PROVIDERS:
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


def _effort_for_provider(provider: str, effort: str, model: str = "") -> str:
    """Drop an effort value the active provider or model does not support.

    Effort is shared across providers and models, so e.g. Codex's ``none`` must
    not leak into a Claude ``--effort`` argument, and ``ultra`` must not reach a
    Codex model that stops at ``xhigh`` (Codex answers such a run with a 400).
    """
    return effort_supported_by_options(effort, _effort_options(provider, model))


def _effort_options(provider: str, model: str) -> tuple[tuple[str, str], ...]:
    if provider == PROVIDER_CLAUDE:
        return claude_effort_options(model)
    return codex_effort_options(model)


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


def _on_add_cards_did_add_note(_note: Any) -> None:
    # Add Cards keeps its note id at None across adds, so the pane would not
    # notice the switch to the next note.
    for editor, pane in list(_panes.items()):
        if editor.editorMode is EditorMode.ADD_CARDS:
            pane.on_note_added()


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


class EditorAgentPane(ThreadPanel, QWidget):
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
        # The pane shows one thread from the queue store at a time; the store is
        # the source of truth for its transcript, status and proposal.
        self._thread_id: str | None = None
        self._threads_shown: list[Thread] = []
        # Threads without a note id (Add Cards, multi-card Browser selections)
        # stay listed in this pane for its lifetime.
        self._local_thread_ids: list[str] = []
        self._force_new_thread = False
        self._suppress_autopick = False
        self._updating_thread_combo = False
        self._last_rendered: tuple[str, str] | None = None
        self._activity_id: str | None = None
        self._activity_open = False
        self._context_generation = 0
        self._shown_context = self._context_key()
        self._selected_text_snapshot: SelectedTextSnapshot | None = None
        self._selection_context_refresh_pending = False
        self._selection_context_request_id = 0
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
        self._load_threads()

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
        qconnect(
            self.instructions_reset_button.clicked, self._reset_custom_instructions
        )
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

        self.thread_combo = QComboBox()
        self.thread_combo.setEditable(False)
        self.thread_combo.setToolTip("Agent requests for this note")
        qconnect(self.thread_combo.currentIndexChanged, self._on_thread_combo_changed)
        self.thread_combo.setVisible(False)
        layout.addWidget(self.thread_combo)

        self.surface = AnkiWebView(self)
        self.surface.stdHtml(
            surface_body(),
            js=[
                "js/mathjax.js",
                "js/vendor/mathjax/tex-chtml-full.js",
            ],
            context=self,
        )
        self.surface.set_bridge_command(self._on_bridge_cmd, self)

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
        self.discard_button = QPushButton("Discard")
        self.discard_button.setEnabled(False)
        qconnect(
            self.discard_button.clicked,
            lambda _checked=False: self._discard_pending_patch(),
        )
        action_row.addWidget(self.apply_button)
        action_row.addWidget(self.discard_button)
        layout.addLayout(action_row)

        send_row = QHBoxLayout()
        self.new_request_button = QPushButton("New request")
        self.new_request_button.setToolTip(
            "Start a new request instead of replying to the one shown"
        )
        qconnect(self.new_request_button.clicked, self._start_new_request)
        self.queue_button = QPushButton("Queue")
        self.queue_button.setToolTip(
            "Open the agent queue for this note "
            f"({queue_window.DEFAULT_SHORTCUT} opens all requests)"
        )
        qconnect(
            self.queue_button.clicked, lambda _checked=False: self._open_queue_window()
        )
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        qconnect(self.stop_button.clicked, self._stop_running_agent)
        self.send_button = QPushButton("Send")
        qconnect(self.send_button.clicked, self._send)
        send_row.addStretch(1)
        send_row.addWidget(self.queue_button)
        send_row.addWidget(self.new_request_button)
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
        self.claude_path_edit = QLineEdit()
        self.claude_path_edit.setPlaceholderText(resolve_claude_path(""))
        self.claude_path_label = QLabel("Claude CLI")
        form.addRow(self.claude_path_label, self.claude_path_edit)
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
        self.access_label = QLabel("Folder access")
        form.addRow(self.access_label, self.access_combo)
        self.card_access_combo = QComboBox()
        for label, value in CARD_ACCESS_OPTIONS:
            self.card_access_combo.addItem(label, value)
        self.card_access_combo.setToolTip(
            "Read-only access to additional cards. Deck access uses the current "
            "card's home deck (no subdecks), or the decks of selected cards. "
            "In Add Cards it uses the selected target deck. Current card only "
            "keeps the existing editor/selected-card context. Changing access "
            "applies to the next request; replies keep their request's access."
        )
        qconnect(
            self.card_access_combo.currentIndexChanged, self._on_card_access_changed
        )
        form.addRow("Card access", self.card_access_combo)
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
            self.claude_path_edit.setText(str(config["claude_path"]))
            self._set_model_choice(
                self._saved_model_for_provider(config["provider"], config)
            )
            self._set_effort_choice(str(config["reasoning_effort"]))
            self._set_project_folder_choices(
                str(config["project_folder"]),
                config["recent_project_folders"],
            )
            self._set_project_folder_access(str(config["project_folder_access"]))
            self.card_access_combo.setCurrentIndex(
                next(
                    i
                    for i, (_, value) in enumerate(CARD_ACCESS_OPTIONS)
                    if value == config["card_access"]
                )
            )
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
        config["claude_path"] = self.claude_path_edit.text().strip()
        config["reasoning_effort"] = self._reasoning_effort()
        self._save_current_instructions(config)
        config["instructions_collapsed"] = self._instructions_are_collapsed()
        config["project_folder"] = project_folder
        config["project_folder_access"] = self._project_folder_access()
        config["card_access"] = self._card_access()
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
            elif provider == PROVIDER_CLAUDE:
                for label, value in model_options_with_legacy(
                    model, CLAUDE_MODEL_OPTIONS
                ):
                    self.model_combo.addItem(label, value)
                self.model_combo.setCurrentIndex(
                    model_option_index(model, CLAUDE_MODEL_OPTIONS)
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
        provider = provider_value(provider)
        if provider == PROVIDER_OLLAMA:
            return str(config["ollama_model"])
        if provider == PROVIDER_CLAUDE:
            return str(config["claude_model"])
        return str(config["codex_model"])

    def _save_current_model_choice(self, config: dict[str, Any]) -> None:
        provider = getattr(self, "_model_provider", PROVIDER_CODEX)
        if provider == PROVIDER_OLLAMA:
            config["ollama_model"] = self._model_text()
        elif provider == PROVIDER_CLAUDE:
            config["claude_model"] = self._model_text()
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
        # The new model may reason to a different depth, so re-offer the effort
        # levels it takes and persist the (possibly reset) choice. The pulldown
        # only reaches the config on save, so start from the live pick.
        self._set_effort_choice(self._reasoning_effort())
        config["reasoning_effort"] = self._reasoning_effort()
        _write_config(config)
        self._load_instructions_for_current_choice(config)
        self.refresh_context_label()

    def _on_provider_changed(self, _index: int) -> None:
        config = _config()
        loading = getattr(self, "_loading_settings", False)
        # Effort is shared across providers, so the pick carries over - but the
        # pulldown only reaches the config on save, so read it before the new
        # provider rebuilds it. While loading, the config is the only truth.
        effort = (
            str(config["reasoning_effort"]) if loading else self._reasoning_effort()
        )
        if not loading:
            self._save_current_instructions(config)
            self._save_current_model_choice(config)
            config["provider"] = self._provider()
        self._set_model_choice(self._saved_model_for_provider(self._provider(), config))
        self._set_effort_choice(effort)
        if not loading:
            config["reasoning_effort"] = self._reasoning_effort()
            _write_config(config)
        self._load_instructions_for_current_choice(config)
        self._update_provider_controls()
        self.refresh_context_label()
        if (
            not getattr(self, "_loading_settings", False)
            and self._provider() == PROVIDER_OLLAMA
        ):
            self._refresh_ollama_models()

    def _update_provider_controls(self) -> None:
        provider = self._provider()
        is_codex = provider == PROVIDER_CODEX
        is_ollama = provider == PROVIDER_OLLAMA
        is_claude = provider == PROVIDER_CLAUDE
        # Codex and Claude are agentic CLIs that can work in a project folder and
        # take a reasoning-effort setting; Ollama is local context-only.
        folder_capable = is_codex or is_claude
        self._set_form_row_visible(
            self.codex_path_label, self.codex_path_edit, is_codex
        )
        self._set_form_row_visible(
            self.claude_path_label, self.claude_path_edit, is_claude
        )
        self._set_form_row_visible(
            self.ollama_path_label, self.ollama_path_edit, is_ollama
        )
        self._set_form_row_visible(
            self.ollama_host_label, self.ollama_host_edit, is_ollama
        )
        self.model_refresh_button.setVisible(is_ollama)
        self._set_form_row_visible(self.effort_label, self.effort_combo, folder_capable)
        self._set_form_row_visible(
            self.fast_mode_label, self.fast_mode_checkbox, is_codex
        )
        self._set_form_row_visible(self.access_label, self.access_combo, folder_capable)
        self._set_form_row_visible(
            self.reasoning_label, self.reasoning_checkbox, is_codex
        )
        self.project_row_widget.setVisible(folder_capable)
        self.codex_path_edit.setEnabled(is_codex)
        self.claude_path_edit.setEnabled(is_claude)
        self.ollama_path_edit.setEnabled(is_ollama)
        self.ollama_host_edit.setEnabled(is_ollama)
        self.model_refresh_button.setEnabled(is_ollama)
        self.effort_combo.setEnabled(folder_capable)
        self.fast_mode_checkbox.setEnabled(is_codex)
        self.access_combo.setEnabled(folder_capable)
        self.reasoning_checkbox.setEnabled(is_codex)
        self.project_row_widget.setEnabled(folder_capable)

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
        options = self._effort_options_for_provider()
        effort = effort_choice_for_options(effort, options)
        self.effort_combo.clear()
        for label, value in effort_options_with_legacy(effort, options):
            self.effort_combo.addItem(label, value)
        self.effort_combo.setCurrentIndex(effort_option_index(effort, options))

    def _effort_options_for_provider(self) -> tuple[tuple[str, str], ...]:
        """Effort levels the selected provider *and* model both support."""
        return _effort_options(self._provider(), self._model_text())

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

    def _card_access(self) -> str:
        return normalize_card_access(self.card_access_combo.currentData())

    def _on_card_access_changed(self, _index: int) -> None:
        if getattr(self, "_loading_settings", False):
            return
        # A thread's permitted scope is part of its settings, so the next Send
        # starts a new thread rather than replying with the old access.
        self._force_new_thread = True
        self._save_settings()
        self.refresh_context_label()

    def _card_read_scope(
        self, snapshot: EditorSnapshot | MultiCardSnapshot
    ) -> CardReadScope:
        access = self._card_access()
        deck_ids: set[int] = set()
        if access == "deck":
            if isinstance(snapshot, MultiCardSnapshot):
                deck_ids.update(
                    card.deck_id for card in snapshot.cards if card.deck_id is not None
                )
            else:
                assert self.editor is not None
                if self.editor.card is not None:
                    deck_ids.add(int(self.editor.card.current_deck_id()))
                elif self.editor.editorMode is EditorMode.ADD_CARDS:
                    chooser = getattr(self.editor.parentWindow, "deck_chooser", None)
                    if chooser is not None:
                        deck_ids.add(int(chooser.selected_deck_id))
                elif self.editor.note is not None and self.editor.note.id:
                    deck_ids.update(
                        int(card.current_deck_id()) for card in self.editor.note.cards()
                    )
        return CardReadScope(access, tuple(sorted(deck_ids)))

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
        self._on_context_changed()

    def on_note_added(self) -> None:
        """Add Cards added the note and loaded a blank one: start afresh."""
        self._context_generation += 1
        self._set_selected_text_snapshot(None)
        self._start_new_request()

    def on_browser_context_changed(self) -> None:
        self.refresh_context_label()
        self._on_context_changed()

    def _on_context_changed(self) -> None:
        key = self._context_key()
        if key == self._shown_context:
            return
        self._shown_context = key
        self._context_generation += 1
        self._force_new_thread = False
        self._suppress_autopick = False
        self._load_threads()

    def _context_key(self) -> tuple[str, Any]:
        """What the pane is looking at: a note id, or a multi-card selection."""
        if self.browser is not None:
            return ("cards", self._current_browser_multi_card_ids())
        note = self.editor.note if self.editor is not None else None
        return ("note", int(note.id) if note is not None and note.id else None)

    def _current_browser_multi_card_ids(self) -> tuple[int, ...]:
        browser = self.browser
        if browser is None or _browser_selection_count(browser) <= 1:
            return ()
        try:
            return tuple(int(card_id) for card_id in browser.selected_cards())
        except Exception:
            return ()

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

    def _threads_for_context(self) -> list[Thread]:
        service = self._service()
        if service is None:
            return []
        store = service.store
        kind, value = self._shown_context
        if kind == "note" and value is not None:
            return store.threads_for_note(value)
        threads = [
            thread
            for thread_id in self._local_thread_ids
            if (thread := store.get_thread(thread_id)) is not None
        ]
        if kind == "cards":
            threads = [
                thread
                for thread in threads
                if (snapshot := self._thread_snapshot(thread)) is not None
                and snapshot_card_ids(snapshot) == value
            ]
        return threads

    # showing threads

    def _load_threads(self, select: str | None = None) -> None:
        """List the current note's threads and show ``select`` (or the best one)."""
        threads = self._threads_for_context()
        self._threads_shown = threads
        ids = {thread.id for thread in threads}
        if select is None and not self._suppress_autopick:
            picked = pick_thread(threads)
            select = picked.id if picked is not None else None
        self._thread_id = select if select in ids else None
        self._refresh_thread_combo()
        self._render_selected_thread()

    def _refresh_thread_combo(self) -> None:
        combo = self.thread_combo
        threads = sorted(self._threads_shown, key=lambda t: t.created_at, reverse=True)
        self._updating_thread_combo = True
        try:
            combo.clear()
            selected_index = -1
            for index, thread in enumerate(threads):
                combo.addItem(thread_label(thread), thread.id)
                if thread.id == self._thread_id:
                    selected_index = index
            combo.setCurrentIndex(selected_index)
        finally:
            self._updating_thread_combo = False
        combo.setVisible(len(threads) > 1)

    def _on_thread_combo_changed(self, index: int) -> None:
        if self._updating_thread_combo or index < 0:
            return
        thread_id = self.thread_combo.itemData(index)
        if not thread_id or thread_id == self._thread_id:
            return
        self._force_new_thread = False
        self._suppress_autopick = False
        self._load_threads(select=str(thread_id))

    def on_thread_changed(self, thread_id: str) -> None:
        """A thread changed in the store (run progress, apply, discard...)."""
        self._sync_multi_proposal_dialog(thread_id)
        if thread_id == self._thread_id:
            self._load_threads(select=thread_id)
            return
        service = self._service()
        if service is None:
            return
        thread = service.store.get_thread(thread_id)
        if thread is None:
            return
        kind, value = self._shown_context
        belongs = thread.id in self._local_thread_ids or (
            kind == "note" and value is not None and thread.note_id == value
        )
        if belongs:
            self._load_threads(select=self._thread_id)

    def _broadcast_thread_changed(self, thread_id: str) -> None:
        self._load_threads(select=thread_id)
        queue_runtime.thread_changed(thread_id, exclude=self)

    def _open_queue_window(self) -> None:
        """The queue window, filtered to this note (it has a show-all toggle)."""
        note = self.editor.note if self.editor is not None else None
        note_id = int(note.id) if note is not None and note.id else None
        queue_window.open_window(note_id=note_id)

    def _start_new_request(self) -> None:
        """Deselect the shown thread; the next Send starts a new one."""
        self._force_new_thread = False
        self._suppress_autopick = True
        self._thread_id = None
        self._refresh_thread_combo()
        self._render_selected_thread()

    # sending

    def _send(self) -> None:
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        if self._service() is None:
            showWarning("The agent queue is not open yet.", parent=self)
            return
        # Cleared now so a second Send cannot repeat it; restored if the
        # request is abandoned before it reaches the store.
        self.prompt.clear()
        self._save_settings()
        generation = self._context_generation
        reply_to = self._reply_target()
        if reply_to is not None:
            self._after_note_saved(
                lambda: self._reply_to_thread(reply_to, prompt, generation)
            )
            return
        self._query_selected_text_context(
            lambda selected_text: self._send_after_selection_snapshot(
                prompt,
                generation,
                selected_text,
            ),
        )

    def _restore_prompt(self, prompt: str, reason: str | None = None) -> None:
        """Put back a prompt whose request was not stored, unless the user has
        typed something new since."""
        if not self.prompt.toPlainText().strip():
            self.prompt.setPlainText(prompt)
        if reason:
            tooltip(reason, parent=self)

    def _reply_target(self) -> str | None:
        """The selected thread, when Send should reply to it."""
        if self._force_new_thread:
            return None
        thread = self._selected_thread()
        if thread is None or thread.status not in REPLYABLE_STATUSES:
            return None
        return thread.id

    def _after_note_saved(self, callback: Callable[[], None]) -> None:
        if self.editor is None:
            callback()
            return
        self.editor.call_after_note_saved(callback, keepFocus=True)

    def _send_after_selection_snapshot(
        self,
        prompt: str,
        generation: int,
        selected_text: Any,
    ) -> None:
        if generation != self._context_generation:
            self._restore_prompt(prompt, PROMPT_NOT_SENT)
            return
        self._query_reviewer_context(
            lambda reviewer: self._send_after_reviewer_context(
                prompt, generation, selected_text, reviewer
            )
        )

    def _send_after_reviewer_context(
        self,
        prompt: str,
        generation: int,
        selected_text: Any,
        reviewer: dict[str, Any] | None,
    ) -> None:
        if generation != self._context_generation:
            self._restore_prompt(prompt, PROMPT_NOT_SENT)
            return
        self._after_note_saved(
            lambda: self._create_thread(prompt, generation, selected_text, reviewer)
        )

    def _query_reviewer_context(
        self, callback: Callable[[dict[str, Any] | None], None]
    ) -> None:
        """Capture what the reviewer showed, when this is Edit Current on its card."""
        editor = self.editor
        reviewer = getattr(aqt.mw, "reviewer", None) if aqt.mw is not None else None
        card = getattr(reviewer, "card", None)
        if (
            editor is None
            or editor.editorMode is not EditorMode.EDIT_CURRENT
            or card is None
            or editor.card is None
            or int(editor.card.id) != int(card.id)
        ):
            callback(None)
            return
        state = getattr(reviewer, "state", None)
        context: dict[str, Any] = {
            "side": state if state in ("question", "answer") else None,
            "question_html": reviewer_card_html(card.question),
            "answer_html": reviewer_card_html(card.answer),
            "selection": "",
        }
        web = getattr(reviewer, "web", None)
        if web is None:
            callback(context)
            return

        def on_selection(selection: Any) -> None:
            context["selection"] = (
                selection.strip() if isinstance(selection, str) else ""
            )
            callback(context)

        web.evalWithCallback(REVIEWER_SELECTION_JS, on_selection)

    def _create_thread(
        self,
        prompt: str,
        generation: int,
        selected_text: Any = None,
        reviewer: dict[str, Any] | None = None,
    ) -> None:
        if generation != self._context_generation:
            self._restore_prompt(prompt, PROMPT_NOT_SENT)
            return
        service = self._service()
        if service is None:
            self._restore_prompt(prompt)
            showWarning("The agent queue is not open yet.", parent=self)
            return
        snapshot: Snapshot
        try:
            browser = getattr(self, "browser", None)
            if browser is not None:
                snapshot = browser_multi_card_snapshot(browser)
                source = SOURCE_BROWSER_MULTI
            else:
                assert self.editor is not None
                snapshot = editor_snapshot(self.editor, selected_text)
                source = _editor_source(self.editor)
            card_scope = self._card_read_scope(snapshot)
        except RuntimeError as exc:
            self._restore_prompt(prompt)
            showWarning(str(exc), parent=self)
            return
        settings = self._thread_settings(card_scope)
        if settings is None:
            self._restore_prompt(prompt)
            return
        note_id = snapshot.note_id if isinstance(snapshot, EditorSnapshot) else None
        note = self.editor.note if self.editor is not None else None
        note_mod = getattr(note, "mod", None) if note_id else None
        context = thread_context(
            source,
            snapshot,
            reviewer=reviewer if source == SOURCE_EDIT_CURRENT else None,
            note_mod=int(note_mod) if isinstance(note_mod, int) else None,
            deck_id=self._default_deck_id(snapshot),
        )
        thread = service.store.create_thread(context, settings, prompt, note_id=note_id)
        if note_id is None:
            self._local_thread_ids.append(thread.id)
        self._force_new_thread = False
        self._suppress_autopick = False
        self._set_selected_text_snapshot(selected_text_of(snapshot))
        self._record_prompt(settings, prompt)
        service.runner.enqueue_kick()
        self._load_threads(select=thread.id)

    def _default_deck_id(self, snapshot: Snapshot) -> int | None:
        """The deck new notes default to: the source card's home deck, or the
        Add Cards target deck."""
        try:
            if isinstance(snapshot, MultiCardSnapshot):
                return next(
                    (card.deck_id for card in snapshot.cards if card.deck_id),
                    None,
                )
            editor = self.editor
            if editor is None:
                return None
            if editor.card is not None:
                return int(editor.card.current_deck_id())
            if editor.editorMode is EditorMode.ADD_CARDS:
                chooser = getattr(
                    getattr(editor, "parentWindow", None), "deck_chooser", None
                )
                return int(chooser.selected_deck_id) if chooser is not None else None
            note = editor.note
            if note is not None and note.id:
                cards = note.cards()
                return int(cards[0].current_deck_id()) if cards else None
        except Exception:
            return None
        return None

    def _reply_to_thread(self, thread_id: str, prompt: str, generation: int) -> None:
        if generation != self._context_generation:
            self._restore_prompt(prompt, PROMPT_NOT_SENT)
            return
        service = self._service()
        if service is None:
            self._restore_prompt(prompt)
            showWarning("The agent queue is not open yet.", parent=self)
            return
        # A reply runs with the form as it is now, so changing the provider or
        # model in the pane applies to the next run of the shown thread.
        settings = self._reply_settings(service.store.get_thread(thread_id))
        if settings is None:
            self._restore_prompt(prompt)
            return
        try:
            service.store.reply(thread_id, prompt)
        except QueueTransitionError as exc:
            self._restore_prompt(prompt)
            showWarning(str(exc), parent=self)
            return
        service.store.set_settings(thread_id, settings)
        self._record_prompt(settings, prompt)
        service.runner.enqueue_kick()
        self._broadcast_thread_changed(thread_id)

    def _reply_settings(self, thread: Thread | None) -> dict[str, Any] | None:
        if thread is None:
            return None
        snapshot = self._thread_snapshot(thread)
        if snapshot is None:
            return None
        return self._thread_settings(self._card_read_scope(snapshot))

    def _record_prompt(self, settings: dict[str, Any], prompt: str) -> None:
        config = _config()
        _record_prompt_history_for_model(
            config,
            provider=settings.get("provider", ""),
            model=settings.get("model", ""),
            prompt=prompt,
        )
        _write_config(config)

    def _thread_settings(self, card_scope: CardReadScope) -> dict[str, Any] | None:
        """Snapshot of the form for a new thread (see queue_runner.normalize_settings)."""
        config = _config()
        provider = self._provider()
        model = self._model_text() or self._saved_model_for_provider(provider, config)
        if provider == PROVIDER_OLLAMA and not model:
            showWarning("Select an Ollama model first.", parent=self)
            return None
        # Effort is shared across providers and models; drop a value the active
        # pair does not support (e.g. Codex's "none" must not reach Claude, and
        # "ultra" must not reach a Codex model that stops at "xhigh").
        effort = _effort_for_provider(
            provider,
            self._reasoning_effort()
            if provider in (PROVIDER_CODEX, PROVIDER_CLAUDE)
            else "",
            model,
        )
        is_codex = provider == PROVIDER_CODEX
        return {
            "provider": provider,
            "model": model,
            "reasoning_effort": effort,
            "card_access": card_scope.access,
            "deck_ids": list(card_scope.deck_ids),
            "project_folder": (
                "" if provider == PROVIDER_OLLAMA else self._project_folder_text()
            ),
            "project_folder_access": self._project_folder_access(),
            "custom_instructions": self._custom_instructions_text(),
            "fast_mode": self._fast_mode() if is_codex else False,
            "stream_reasoning_summaries": (
                self._stream_reasoning_summaries() if is_codex else False
            ),
            "timeout_seconds": int(config["timeout_seconds"]),
            "paths": {
                "codex_path": self.codex_path_edit.text().strip()
                or str(config["codex_path"]),
                "claude_path": self.claude_path_edit.text().strip()
                or str(config["claude_path"]),
                "ollama_path": self.ollama_path_edit.text().strip()
                or str(config["ollama_path"]),
                "ollama_host": self.ollama_host_edit.text().strip()
                or str(config["ollama_host"]),
            },
        }

    def _after_thread_rendered(
        self,
        thread: Thread | None,
        previous: tuple[str, str] | None,
        snapshot: Snapshot | None,
        shown: Patch | None,
    ) -> None:
        """Open the multi-card review when the shown thread finishes running."""
        if thread is None:
            return
        became_ready = (
            thread.status == READY
            and previous is not None
            and previous[0] == thread.id
            and previous[1] in ACTIVE_STATUSES
        )
        if not (became_ready and isinstance(shown, MultiNotePatch)):
            return
        # Patch and version from one read, so the dialog knows what it shows.
        found = self._current_proposal(thread.id)
        if found is None:
            return
        _thread, proposal, found_snapshot, patch = found
        if (
            isinstance(patch, MultiNotePatch)
            and patch.note_updates
            and isinstance(found_snapshot, MultiCardSnapshot)
        ):
            self._show_multi_proposal_dialog(
                thread.id, found_snapshot, patch, proposal.version
            )

    def _apply_note_update(
        self, thread: Thread, proposal: Proposal, snapshot: Snapshot, patch: NotePatch
    ) -> None:
        """Fill the editor (after saving what was typed) rather than writing the
        note behind its back."""
        if self.editor is None:
            super()._apply_note_update(thread, proposal, snapshot, patch)
            return
        self.editor.call_after_note_saved(
            lambda: self._apply_patch_after_save(thread.id, proposal.version),
            keepFocus=True,
        )

    def _apply_patch_after_save(self, thread_id: str, version: int) -> None:
        found = self._current_proposal(thread_id)
        if found is None or self.editor is None:
            return
        thread, proposal, snapshot, patch = found
        if proposal.version != version or not isinstance(patch, NotePatch):
            return
        if thread.note_id is not None:
            note = self.editor.note
            if note is None or not note.id or int(note.id) != thread.note_id:
                showWarning(
                    "The editor is no longer showing the note this proposal is for.",
                    parent=self,
                )
                return
            reason = stale_reason(proposal.base, self._note_states([thread.note_id]))
        else:
            # Add Cards: no note id, so check the editor still holds the note
            # the proposal was made for, not the next one after Add.
            note = self.editor.note
            if note is None:
                return
            reason = unsaved_note_stale_reason(
                snapshot,
                int(note.mid),
                [str(html) for _name, html in note.items()],
                list(note.tags),
            )
        if reason is not None:
            showWarning(reason, parent=self)
            return
        creates = patch.note_creates
        try:
            # With new notes, the current note is saved in the same undoable op.
            apply_patch_to_editor(self.editor, patch, save=not creates)
        except PatchValidationError as exc:
            showWarning(str(exc), parent=self)
            return
        if creates:
            in_add_cards = self.editor.editorMode is EditorMode.ADD_CARDS
            self._apply_changes(
                thread_id,
                thread.status,
                version,
                [] if in_add_cards else [self.editor.note],
                creates,
            )
            return
        if self._finish_thread(thread_id, thread.status, APPLIED):
            tooltip("Agent proposal applied.", parent=self)


def _editor_source(editor: Editor) -> str:
    if editor.editorMode is EditorMode.ADD_CARDS:
        return SOURCE_ADD_CARDS
    if editor.editorMode is EditorMode.BROWSER:
        return SOURCE_BROWSER
    return SOURCE_EDIT_CURRENT


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


def apply_patch_to_editor(
    editor: Editor, patch: NotePatch, *, save: bool = True
) -> None:
    """Fill the editor with the patch's update to the current note.

    Outside Add Cards the note is then saved, unless ``save`` is False (the
    caller saves it as part of a larger op).
    """
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

    if save and editor.editorMode is not EditorMode.ADD_CARDS:
        update_note(parent=editor.widget, note=note).run_in_background(initiator=editor)


def _editor_mode_name(editor: Editor) -> str:
    if editor.editorMode is EditorMode.ADD_CARDS:
        return "add"
    if editor.editorMode is EditorMode.BROWSER:
        return "browse"
    return "review"
