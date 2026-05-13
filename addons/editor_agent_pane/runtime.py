# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import os
import weakref
from concurrent.futures import Future
from typing import Any

import aqt
from aqt import gui_hooks
from aqt.editor import Editor, EditorMode
from aqt.operations.note import update_note
from aqt.qt import (
    QAction,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    Qt,
    QTextCursor,
    QVBoxLayout,
    QWidget,
    qconnect,
)
from aqt.utils import showWarning, tooltip

from .codex_client import CodexCliAgent, project_root_status, resolve_codex_path
from .patches import (
    EditorSnapshot,
    FieldSnapshot,
    NotePatch,
    PatchValidationError,
    render_patch_diff,
)

ADDON = "editor_agent_pane"
TOGGLE_COMMAND = "editorAgentPane"
DEFAULT_CONFIG = {
    "codex_path": "",
    "model": "",
    "project_folder": "",
    "timeout_seconds": 300,
}

_installed = False
_panes: weakref.WeakKeyDictionary[Editor, "EditorAgentPane"] = weakref.WeakKeyDictionary()


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
    return config


def _write_config(config: dict[str, Any]) -> None:
    assert aqt.mw is not None
    merged = dict(DEFAULT_CONFIG)
    merged.update(config)
    aqt.mw.addonManager.writeConfig(ADDON, merged)


def _add_editor_button(buttons: list[str], editor: Editor) -> None:
    buttons.append(
        editor.addButton(
            None,
            TOGGLE_COMMAND,
            _toggle_pane,
            tip="Open the editor agent pane",
            label="Agent",
            keys="Ctrl+Alt+Shift+E",
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


class EditorAgentPane(QWidget):
    def __init__(self, editor: Editor) -> None:
        super().__init__(editor.parentWindow)
        self.editor = editor
        self.history: list[tuple[str, str]] = []
        self.pending_patch: NotePatch | None = None
        self.pending_snapshot: EditorSnapshot | None = None

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
        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("Codex default")
        form.addRow("Model", self.model_edit)
        layout.addLayout(form)

        project_row = QHBoxLayout()
        self.project_edit = QLineEdit()
        self.project_edit.setPlaceholderText("Optional read-only project folder")
        browse = QPushButton("Browse")
        qconnect(browse.clicked, self._browse_project)
        project_row.addWidget(self.project_edit, 1)
        project_row.addWidget(browse)
        layout.addLayout(project_row)

        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Ask about the current card or source material.")
        layout.addWidget(self.transcript, 1)

        self.proposal = QPlainTextEdit()
        self.proposal.setReadOnly(True)
        self.proposal.setPlaceholderText("Proposed note changes appear here.")
        self.proposal.setMaximumHeight(180)
        layout.addWidget(self.proposal)

        action_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply proposal")
        self.apply_button.setEnabled(False)
        qconnect(self.apply_button.clicked, self._apply_pending_patch)
        discard_button = QPushButton("Discard")
        qconnect(discard_button.clicked, self._discard_pending_patch)
        action_row.addWidget(self.apply_button)
        action_row.addWidget(discard_button)
        layout.addLayout(action_row)

        self.prompt = QPlainTextEdit()
        self.prompt.setPlaceholderText("Message")
        self.prompt.setMaximumHeight(90)
        layout.addWidget(self.prompt)

        send_row = QHBoxLayout()
        self.send_button = QPushButton("Send")
        qconnect(self.send_button.clicked, self._send)
        send_row.addStretch(1)
        send_row.addWidget(self.send_button)
        layout.addLayout(send_row)

    def _load_settings(self) -> None:
        config = _config()
        self.codex_path_edit.setText(str(config["codex_path"]))
        self.model_edit.setText(str(config["model"]))
        self.project_edit.setText(str(config["project_folder"]))

    def _save_settings(self) -> None:
        config = _config()
        config["codex_path"] = self.codex_path_edit.text().strip()
        config["model"] = self.model_edit.text().strip()
        config["project_folder"] = self.project_edit.text().strip()
        _write_config(config)

    def _browse_project(self) -> None:
        start = self.project_edit.text().strip() or os.path.expanduser("~")
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select project folder",
            start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if folder:
            self.project_edit.setText(folder)
            self._save_settings()

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
        self.context_label.setText(
            f"{mode} - note {int(note.id) if note.id else 'new'} - "
            f"{notetype_name}\n{project_root_status(self.project_edit.text())}"
        )

    def _append_transcript(self, text: str) -> None:
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
        self.transcript.insertPlainText(text)
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)

    def _send(self) -> None:
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            return
        self.prompt.clear()
        self._save_settings()
        self._append_transcript(f"\nYou: {prompt}\nAssistant: ")
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
        model = self.model_edit.text().strip() or str(config["model"])
        project_root = self.project_edit.text().strip()
        codex_path = self.codex_path_edit.text().strip() or str(config["codex_path"])
        self.send_button.setEnabled(False)
        self.apply_button.setEnabled(False)
        self.pending_patch = None
        self.pending_snapshot = None
        self.proposal.clear()
        self._append_transcript("\n[Codex CLI running in read-only mode]\n")

        def task() -> tuple[str, tuple[NotePatch, ...]]:
            agent = CodexCliAgent(
                codex_path=codex_path,
                model=model,
                timeout_seconds=int(config["timeout_seconds"]),
            )
            result = agent.send(
                prompt=prompt,
                snapshot=snapshot,
                project_root=project_root,
                history=self.history,
            )
            return result.text, result.proposals

        def on_done(future: Future) -> None:
            self.send_button.setEnabled(True)
            try:
                text, proposals = future.result()
            except Exception as exc:
                self._append_transcript(f"\nError: {exc}\n")
                return
            if text:
                self._append_transcript(text)
            self._append_transcript("\n")
            self.history.append((prompt, text))
            if proposals:
                self.pending_patch = proposals[-1]
                self.pending_snapshot = snapshot
                self.proposal.setPlainText(render_patch_diff(snapshot, proposals[-1]))
                self.apply_button.setEnabled(True)

        assert aqt.mw is not None
        aqt.mw.taskman.run_in_background(task, on_done, uses_collection=False)

    def _discard_pending_patch(self) -> None:
        self.pending_patch = None
        self.pending_snapshot = None
        self.proposal.clear()
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

    return EditorSnapshot(
        mode=_editor_mode_name(editor),
        note_id=int(note.id) if note.id else None,
        notetype_id=int(note.mid),
        notetype_name=str(notetype.get("name", "")),
        fields=fields,
        tags=tuple(note.tags),
        current_field=current_field,
        card_id=card_id,
    )


def apply_patch_to_editor(editor: Editor, patch: NotePatch) -> None:
    note = editor.note
    if note is None:
        raise PatchValidationError("No current note.")
    if patch.note_id not in (None, int(note.id) if note.id else None):
        raise PatchValidationError("The current note has changed since the proposal.")
    if patch.notetype_id != int(note.mid):
        raise PatchValidationError("The current note type has changed since the proposal.")

    names = [str(name) for name, _html in note.items()]
    for field_name, html in patch.field_updates.items():
        try:
            index = names.index(field_name)
        except ValueError as exc:
            raise PatchValidationError(f"Unknown field: {field_name}.") from exc
        note.fields[index] = html

    note.tags = list(patch.tag_patch.apply(tuple(note.tags)))

    if editor.editorMode is EditorMode.ADD_CARDS:
        editor.loadNoteKeepingFocus()
    else:
        update_note(parent=editor.widget, note=note).success(
            lambda _changes: editor.loadNoteKeepingFocus()
        ).run_in_background(initiator=editor)


def _editor_mode_name(editor: Editor) -> str:
    if editor.editorMode is EditorMode.ADD_CARDS:
        return "add"
    if editor.editorMode is EditorMode.BROWSER:
        return "browse"
    return "review"
