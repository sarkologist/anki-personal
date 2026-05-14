# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
import weakref
from collections.abc import Callable
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
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import showWarning, tooltip
from aqt.webview import AnkiWebView

from .activity import CodexActivityRenderer
from .codex_client import (
    DEFAULT_PROJECT_FOLDER_ACCESS,
    PROJECT_FOLDER_ACCESS_READ_ONLY,
    PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE,
    CodexCliAgent,
    normalize_project_folder_access,
    project_root_status,
    resolve_codex_path,
)
from .model_options import MODEL_OPTIONS, model_option_index, model_options_with_legacy
from .note_images import collect_note_images
from .patches import (
    EditorSnapshot,
    FieldSnapshot,
    NotePatch,
    PatchValidationError,
)
from .recent_folders import project_folder_choices, remember_project_folder
from .surface import (
    js_append_to_activity,
    js_append_transcript,
    js_apply_agent_proposal,
    js_clear_proposal,
    js_replace_element,
    js_set_proposal,
    render_activity_line,
    render_activity_start,
    render_activity_summary,
    render_assistant_message,
    render_error_message,
    render_proposal_diff,
    render_user_message,
    surface_body,
)
from .ui_text import AGENT_BUTTON_LABEL, AGENT_BUTTON_TIP, AGENT_PANE_SHORTCUT

ADDON = "editor_agent_pane"
TOGGLE_COMMAND = "editorAgentPane"
DEFAULT_SPLITTER_SIZES = [570, 80]
DEFAULT_CONFIG = {
    "codex_path": "",
    "model": "",
    "custom_instructions": "",
    "project_folder": "",
    "project_folder_access": DEFAULT_PROJECT_FOLDER_ACCESS,
    "recent_project_folders": [],
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


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    gui_hooks.editor_did_init_buttons.append(_add_editor_button)
    gui_hooks.editor_did_init.append(_add_editor_menu_action)
    gui_hooks.editor_did_load_note.append(_on_editor_did_load_note)


def _config() -> dict[str, Any]:
    assert aqt.mw is not None
    saved = aqt.mw.addonManager.getConfig(ADDON) or {}
    config = {key: saved.get(key, default) for key, default in DEFAULT_CONFIG.items()}
    if "codex_path" not in saved and config["model"] == "gpt-5.2":
        config["model"] = ""
    config["project_folder_access"] = normalize_project_folder_access(
        str(config["project_folder_access"])
    )
    config["stream_reasoning_summaries"] = _bool_config(
        config["stream_reasoning_summaries"],
        True,
    )
    return config


def _bool_config(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _write_config(config: dict[str, Any]) -> None:
    assert aqt.mw is not None
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    aqt.mw.addonManager.writeConfig(ADDON, merged)


def _validated_splitter_sizes(value: Any) -> list[int]:
    if not isinstance(value, list) or len(value) != len(DEFAULT_SPLITTER_SIZES):
        return list(DEFAULT_SPLITTER_SIZES)

    sizes: list[int] = []
    for size in value:
        if not isinstance(size, int) or size < 1:
            return list(DEFAULT_SPLITTER_SIZES)
        sizes.append(size)
    return sizes


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
        pane.refresh_context_label()


def _toggle_pane(editor: Editor) -> None:
    pane = _panes.get(editor)
    if pane is None:
        pane = EditorAgentPane(editor)
        _panes[editor] = pane
    pane.toggle()


class PromptEdit(QPlainTextEdit):
    def __init__(self, send_callback: Callable[[], None], parent: QWidget) -> None:
        super().__init__(parent)
        self._send_callback = send_callback

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & Qt.KeyboardModifier.ShiftModifier
        ):
            self._send_callback()
            event.accept()
            return
        super().keyPressEvent(event)


class EditorAgentPane(QWidget):
    def __init__(self, editor: Editor) -> None:
        super().__init__(editor.parentWindow)
        self.editor = editor
        self.history: list[tuple[str, str]] = []
        self.pending_patch: NotePatch | None = None
        self.pending_snapshot: EditorSnapshot | None = None
        self._activity_id: str | None = None
        self._activity_counter = 0
        self._activity_open = False

        self.dock = QDockWidget("Agent", editor.parentWindow)
        self.dock.setObjectName("EditorAgentPane")
        self.dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.dock.setWidget(self)

        parent = editor.parentWindow
        if isinstance(parent, QMainWindow):
            parent.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.dock)
        else:
            self.dock.setFloating(True)
        self.dock.hide()

        self._build_ui()
        self._load_settings()
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

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self.codex_path_edit = QLineEdit()
        self.codex_path_edit.setPlaceholderText(resolve_codex_path(""))
        form.addRow("Codex CLI", self.codex_path_edit)
        self.model_combo = QComboBox()
        self.model_combo.setEditable(False)
        for label, value in MODEL_OPTIONS:
            self.model_combo.addItem(label, value)
        form.addRow("Model", self.model_combo)
        self.access_combo = QComboBox()
        self.access_combo.setEditable(False)
        for label, value in PROJECT_FOLDER_ACCESS_OPTIONS:
            self.access_combo.addItem(label, value)
        qconnect(
            self.access_combo.currentIndexChanged,
            lambda _index: self.refresh_context_label(),
        )
        form.addRow("Access", self.access_combo)
        self.reasoning_checkbox = QCheckBox("Show reasoning summaries")
        form.addRow("", self.reasoning_checkbox)
        layout.addLayout(form)

        project_row = QHBoxLayout()
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
        layout.addLayout(project_row)

        instructions_header = QHBoxLayout()
        instructions_label = QLabel("Instructions")
        instructions_reset = QPushButton("Reset")
        qconnect(instructions_reset.clicked, self._reset_custom_instructions)
        instructions_header.addWidget(instructions_label)
        instructions_header.addStretch(1)
        instructions_header.addWidget(instructions_reset)
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

        self.prompt = PromptEdit(self._send, self)
        self.prompt.setPlaceholderText("Message")
        self.prompt.setMinimumHeight(50)

        self.text_splitter = QSplitter(Qt.Orientation.Vertical)
        self.text_splitter.setChildrenCollapsible(False)
        self.text_splitter.addWidget(self.surface)
        self.text_splitter.addWidget(self.prompt)
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
        self.send_button = QPushButton("Send")
        qconnect(self.send_button.clicked, self._send)
        send_row.addStretch(1)
        send_row.addWidget(self.send_button)
        layout.addLayout(send_row)

    def _load_settings(self) -> None:
        config = _config()
        self.codex_path_edit.setText(str(config["codex_path"]))
        self._set_model_choice(str(config["model"]))
        self._set_project_folder_choices(
            str(config["project_folder"]),
            config["recent_project_folders"],
        )
        self._set_project_folder_access(str(config["project_folder_access"]))
        self.reasoning_checkbox.setChecked(bool(config["stream_reasoning_summaries"]))
        self.instructions_edit.setPlainText(str(config["custom_instructions"] or ""))
        self.text_splitter.setSizes(_validated_splitter_sizes(config["splitter_sizes"]))

    def _save_settings(self) -> None:
        config = _config()
        project_folder = self._project_folder_text()
        config["codex_path"] = self.codex_path_edit.text().strip()
        config["model"] = self._model_text()
        config["custom_instructions"] = self._custom_instructions_text()
        config["project_folder"] = project_folder
        config["project_folder_access"] = self._project_folder_access()
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

    def _set_model_choice(self, model: str) -> None:
        self.model_combo.clear()
        for label, value in model_options_with_legacy(model):
            self.model_combo.addItem(label, value)
        self.model_combo.setCurrentIndex(model_option_index(model))

    def _model_text(self) -> str:
        data = self.model_combo.currentData()
        return str(data).strip() if data is not None else ""

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
        self.project_edit.setEditText(project_folder.strip())

    def _project_folder_text(self) -> str:
        return self.project_edit.currentText().strip()

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

    def toggle(self) -> None:
        self.dock.setVisible(not self.dock.isVisible())
        if self.dock.isVisible():
            self.prompt.setFocus()
            self.refresh_context_label()

    def refresh_context_label(self) -> None:
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
        project_status = project_root_status(
            self._project_folder_text(),
            self._project_folder_access(),
        )
        self.context_label.setText(
            f"{mode} - note {int(note.id) if note.id else 'new'} - "
            f"{notetype_name}\n{project_status}"
        )

    def _append_transcript(self, fragment: str) -> None:
        self.surface.eval(js_append_transcript(fragment))

    def _replace_activity_with_summary(self, summary: str) -> None:
        if self._activity_id:
            self.surface.eval(
                js_replace_element(
                    self._activity_id,
                    render_activity_summary(self._activity_id, summary),
                )
            )
        self._activity_id = None
        self._activity_open = False

    def _append_activity_line(self, line: str) -> None:
        if self._activity_open and self._activity_id:
            self.surface.eval(
                js_append_to_activity(self._activity_id, render_activity_line(line))
            )

    def _clear_proposal(self) -> None:
        self.surface.eval(js_clear_proposal())

    def _set_proposal(self, snapshot: EditorSnapshot, patch: NotePatch) -> None:
        self.surface.eval(js_set_proposal(render_proposal_diff(snapshot, patch)))

    def _send(self) -> None:
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        self.prompt.clear()
        self._save_settings()
        self._append_transcript(render_user_message(prompt))
        self.editor.call_after_note_saved(
            lambda: self._start_agent_request(prompt),
            keepFocus=True,
        )

    def _start_agent_request(self, prompt: str) -> None:
        try:
            snapshot = editor_snapshot(self.editor)
        except RuntimeError as exc:
            showWarning(str(exc), parent=self)
            return

        config = _config()
        model = self._model_text() or str(config["model"])
        project_root = self._project_folder_text()
        project_folder_access = self._project_folder_access()
        custom_instructions = self._custom_instructions_text()
        codex_path = self.codex_path_edit.text().strip() or str(config["codex_path"])
        stream_reasoning_summaries = self._stream_reasoning_summaries()
        self.send_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.pending_patch = None
        self.pending_snapshot = None
        self._clear_proposal()
        self._activity_counter += 1
        self._activity_id = f"agent-activity-{self._activity_counter}"
        self._activity_open = True
        self._append_transcript(render_activity_start(self._activity_id))
        activity = CodexActivityRenderer(
            stream_reasoning_summaries=stream_reasoning_summaries
        )
        assert aqt.mw is not None
        taskman = aqt.mw.taskman

        def on_stream_event(event: dict[str, Any]) -> None:
            line = activity.record(event)
            if line is not None:
                taskman.run_on_main(lambda line=line: self._append_activity_line(line))

        def task() -> tuple[str, str, tuple[NotePatch, ...], str]:
            agent = CodexCliAgent(
                codex_path=codex_path,
                model=model,
                timeout_seconds=int(config["timeout_seconds"]),
                project_folder_access=project_folder_access,
                custom_instructions=custom_instructions,
            )
            result = agent.send(
                prompt=prompt,
                snapshot=snapshot,
                project_root=project_root,
                history=self.history,
                event_callback=on_stream_event,
            )
            return (
                result.text,
                result.html,
                result.proposals,
                activity.compact_summary(),
            )

        def on_done(future: Future) -> None:
            self.send_button.setEnabled(True)
            try:
                text, message_html, proposals, activity_summary = future.result()
            except Exception as exc:
                self._activity_open = False
                self._append_transcript(render_error_message(str(exc)))
                return
            self._replace_activity_with_summary(activity_summary)
            if text or message_html:
                self._append_transcript(render_assistant_message(message_html, text))
            self.history.append((prompt, text))
            if proposals:
                self.pending_patch = proposals[-1]
                self.pending_snapshot = snapshot
                self._set_proposal(snapshot, proposals[-1])
                self.apply_button.setEnabled(True)

        taskman.run_in_background(task, on_done, uses_collection=False)

    def _discard_pending_patch(self) -> None:
        self.pending_patch = None
        self.pending_snapshot = None
        self._clear_proposal()
        self.apply_button.setEnabled(False)

    def _apply_pending_patch(self) -> None:
        patch = self.pending_patch
        if patch is None:
            return
        self.editor.call_after_note_saved(
            lambda: self._apply_patch_after_save(patch),
            keepFocus=True,
        )

    def _apply_patch_after_save(self, patch: NotePatch) -> None:
        try:
            apply_patch_to_editor(self.editor, patch)
        except PatchValidationError as exc:
            showWarning(str(exc), parent=self)
            return
        self._discard_pending_patch()
        tooltip("Agent proposal applied.", parent=self)


def editor_snapshot(editor: Editor) -> EditorSnapshot:
    note = editor.note
    if note is None:
        raise RuntimeError("No current note.")
    notetype = editor.note_type()
    fields = tuple(
        FieldSnapshot(name=str(name), html=str(html)) for name, html in note.items()
    )
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
    )


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
        update_note(parent=editor.widget, note=note).run_in_background(
            initiator=editor
        )


def _editor_mode_name(editor: Editor) -> str:
    if editor.editorMode is EditorMode.ADD_CARDS:
        return "add"
    if editor.editorMode is EditorMode.BROWSER:
        return "browse"
    return "review"
