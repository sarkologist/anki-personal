# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import importlib
import io
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[2]
ADDONS = ROOT / "addons"
if str(ADDONS) not in sys.path:
    sys.path.insert(0, str(ADDONS))

from editor_agent_pane import ollama_client  # noqa: E402
from editor_agent_pane.activity import (  # noqa: E402
    CodexActivityRenderer,
    compact_activity_transcript,
)
from editor_agent_pane.agent_log import (  # noqa: E402
    MAX_PREVIEW_CHARS,
    JsonLineAgentRunLogger,
    ensure_agent_log_folder,
)
from editor_agent_pane.claude_client import (  # noqa: E402
    ClaudeCliAgent,
    claude_effort_value,
    resolve_claude_path,
)
from editor_agent_pane.codex_client import (  # noqa: E402
    PROJECT_FOLDER_ACCESS_READ_ONLY,
    PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE,
    AgentStopped,
    CodexCliAgent,
    project_root_status,
    resolve_codex_path,
)
from editor_agent_pane.effort_options import (  # noqa: E402
    CLAUDE_EFFORT_OPTIONS,
    EFFORT_OPTIONS,
    effort_option_index,
    effort_options_with_legacy,
)
from editor_agent_pane.latex_preview import (  # noqa: E402
    LatexPreviewError,
    LegacyLatexPreviewRenderer,
    PreviewExtractedLatex,
    PreviewExtractedLatexOutput,
)
from editor_agent_pane.model_options import (  # noqa: E402
    CLAUDE_MODEL_OPTIONS,
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
from editor_agent_pane.note_images import collect_note_images  # noqa: E402
from editor_agent_pane.ollama_client import (  # noqa: E402
    DEFAULT_OLLAMA_HOST,
    OllamaCliAgent,
    OllamaModelInfo,
    discover_ollama_models,
    normalize_ollama_host,
    parse_ollama_list,
    resolve_ollama_path,
)
from editor_agent_pane.patches import (  # noqa: E402
    EditorSnapshot,
    FieldSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    NoteImageSnapshot,
    PatchValidationError,
    SelectedCardSnapshot,
    SelectedNoteSnapshot,
    SelectedTextSnapshot,
    validate_multi_note_patch,
    validate_note_patch,
    validate_selected_text_snapshot,
)
from editor_agent_pane.recent_folders import (  # noqa: E402
    MAX_RECENT_PROJECT_FOLDERS,
    NO_PROJECT_FOLDER_LABEL,
    project_folder_choices,
    remember_project_folder,
)
from editor_agent_pane.sanitize import sanitize_html  # noqa: E402
from editor_agent_pane.sources import (  # noqa: E402
    SourceAccessError,
    read_source_file,
    search_source_files,
)
from editor_agent_pane.surface import (  # noqa: E402
    js_apply_agent_proposal,
    js_clear_transcript,
    multi_note_patch_card_ids,
    render_activity_summary,
    render_assistant_message,
    render_error_message,
    render_multi_note_card_proposal_diff,
    render_proposal_diff,
    render_user_message,
    selection_context_label_text,
)
from editor_agent_pane.ui_text import (  # noqa: E402
    AGENT_BUTTON_LABEL,
    AGENT_BUTTON_TIP,
    AGENT_PANE_SHORTCUT,
)


def snapshot() -> EditorSnapshot:
    return EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(name="Front", html="old front"),
            FieldSnapshot(name="Back", html="old back"),
        ),
        tags=("keep", "remove-me"),
        selected_text=SelectedTextSnapshot(
            field_name="Front",
            field_index=0,
            input_kind="rich_text",
            text="old",
            html="<b>old</b>",
        ),
    )


def multi_snapshot() -> MultiCardSnapshot:
    return MultiCardSnapshot(
        cards=(
            SelectedCardSnapshot(
                card_id=11,
                note_id=101,
                notetype_id=7,
                notetype_name="Basic",
                ord=0,
                template_name="Card 1",
                deck_id=1,
                deck_name="Default",
            ),
            SelectedCardSnapshot(
                card_id=12,
                note_id=101,
                notetype_id=7,
                notetype_name="Basic",
                ord=1,
                template_name="Card 2",
                deck_id=1,
                deck_name="Default",
            ),
            SelectedCardSnapshot(
                card_id=21,
                note_id=202,
                notetype_id=8,
                notetype_name="Cloze",
                ord=0,
                template_name="Cloze",
                deck_id=2,
                deck_name="Filtered",
            ),
        ),
        notes=(
            SelectedNoteSnapshot(
                note_id=101,
                notetype_id=7,
                notetype_name="Basic",
                fields=(
                    FieldSnapshot(name="Front", html="old front"),
                    FieldSnapshot(name="Back", html="old back"),
                ),
                tags=("keep", "remove-me"),
            ),
            SelectedNoteSnapshot(
                note_id=202,
                notetype_id=8,
                notetype_name="Cloze",
                fields=(FieldSnapshot(name="Text", html="old {{c1::text}}"),),
                tags=("cloze",),
            ),
        ),
    )


class _MediaThatMustNotBeWritten:
    def write_data(self, _filename: str, _data: bytes) -> None:
        raise AssertionError("proposal preview must not write generated LaTeX media")


class _CollectionThatMustNotReceiveMediaWrites:
    media = _MediaThatMustNotBeWritten()


class FakeStdin:
    def __init__(self) -> None:
        self.text = ""
        self.closed = False

    def write(self, text: str) -> int:
        self.text += text
        return len(text)

    def close(self) -> None:
        self.closed = True


class FakePopen:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int | None = 0,
        on_kill: Callable[[], None] | None = None,
        on_terminate: Callable[[], None] | None = None,
    ) -> None:
        self.stdin = FakeStdin()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = returncode
        self.killed = False
        self.terminated = False
        self.on_kill = on_kill
        self.on_terminate = on_terminate

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        if self.on_kill is not None:
            self.on_kill()
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        if self.on_terminate is not None:
            self.on_terminate()
        self.terminated = True
        self.returncode = -15


def write_codex_response(command: list[str], payload: dict[str, Any] | str) -> Path:
    output_path = Path(command[command.index("--output-last-message") + 1])
    text = payload if isinstance(payload, str) else json.dumps(payload)
    output_path.write_text(text, encoding="utf-8")
    return output_path


class FakeRunLogger:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def record(self, event: str, **payload: Any) -> None:
        self.records.append({"event": event, **payload})

    def first(self, event: str) -> dict[str, Any]:
        for record in self.records:
            if record["event"] == event:
                return record
        raise AssertionError(f"missing log event: {event}")

    def all(self, event: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record["event"] == event]


class CapturingLogger:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)


class FakeAddonManagerForLogs:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.requested_addons: list[str] = []

    def logs_folder(self, addon: str) -> Path:
        self.requested_addons.append(addon)
        return self.path


class FakeMediaManager:
    def __init__(self, media_dir: Path, refs_by_html: dict[str, list[str]]) -> None:
        self._media_dir = media_dir
        self._refs_by_html = refs_by_html

    def dir(self) -> str:
        return str(self._media_dir)

    def files_in_str(
        self,
        mid: int,
        string: str,
        include_remote: bool = False,
    ) -> list[str]:
        assert mid == 7
        assert include_remote is False
        return list(self._refs_by_html.get(string, []))


class FakeSurface:
    def __init__(self) -> None:
        self.evals: list[str] = []

    def eval(self, js: str) -> None:
        self.evals.append(js)


class FakeButton:
    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.visible = True
        self.tooltip = ""

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setVisible(self, visible: bool) -> None:
        self.visible = visible

    def setToolTip(self, tooltip: str) -> None:
        self.tooltip = tooltip


class FakeToolButton(FakeButton):
    def __init__(self) -> None:
        super().__init__()
        self.arrow_type: object | None = None

    def setArrowType(self, arrow_type: object) -> None:
        self.arrow_type = arrow_type


class FakePrompt:
    def __init__(self, text: str) -> None:
        self.text = text
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True
        self.text = ""


class FakeKeyEvent:
    def __init__(self, key: object, modifiers: object = 0) -> None:
        self._key = key
        self._modifiers = modifiers
        self.accepted = False

    def key(self) -> object:
        return self._key

    def modifiers(self) -> object:
        return self._modifiers

    def accept(self) -> None:
        self.accepted = True


class FakeLabel:
    def __init__(self) -> None:
        self.text = ""
        self.visible = False

    def setText(self, text: str) -> None:
        self.text = text

    def clear(self) -> None:
        self.text = ""

    def setVisible(self, visible: bool) -> None:
        self.visible = visible


class FakeCombo:
    def __init__(self) -> None:
        self.items: list[tuple[str, Any]] = []
        self.current_index = 0
        self.enabled = True
        self.visible = True

    def clear(self) -> None:
        self.items.clear()
        self.current_index = 0

    def addItem(self, label: str, value: Any = None) -> None:
        self.items.append((label, value))

    def addItems(self, items: list[str]) -> None:
        for item in items:
            self.addItem(item, item)

    def setCurrentIndex(self, index: int) -> None:
        self.current_index = index

    def currentData(self) -> Any:
        if not self.items:
            return None
        return self.items[self.current_index][1]

    def itemData(self, index: int) -> Any:
        return self.items[index][1]

    def count(self) -> int:
        return len(self.items)

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setVisible(self, visible: bool) -> None:
        self.visible = visible


class FakeLineEdit:
    def __init__(self, text: str = "") -> None:
        self._text = text
        self.enabled = True
        self.visible = True

    def text(self) -> str:
        return self._text

    def setText(self, text: str) -> None:
        self._text = text

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def setVisible(self, visible: bool) -> None:
        self.visible = visible


class FakeInstructionsEdit:
    def __init__(self, text: str = "") -> None:
        self.text = text
        self.visible = True

    def toPlainText(self) -> str:
        return self.text

    def setPlainText(self, text: str) -> None:
        self.text = text

    def clear(self) -> None:
        self.text = ""

    def setVisible(self, visible: bool) -> None:
        self.visible = visible


class FakeProjectCombo:
    def __init__(self, current_text: str = "") -> None:
        self.items: list[str] = []
        self.current_text = current_text
        self.current_index: int | None = None

    def clear(self) -> None:
        self.items.clear()

    def addItems(self, items: list[str]) -> None:
        self.items.extend(items)

    def setCurrentIndex(self, index: int) -> None:
        self.current_index = index
        self.current_text = self.items[index]

    def setEditText(self, text: str) -> None:
        self.current_text = text

    def currentText(self) -> str:
        return self.current_text


class FakeDock:
    def __init__(self, visible: bool = True) -> None:
        self.visible = visible

    def isVisible(self) -> bool:
        return self.visible


class ImmediateTaskman:
    def __init__(self) -> None:
        self.uses_collection: bool | None = None

    def run_in_background(
        self,
        task: Callable[[], object],
        on_done: Callable[[Any], None],
        *,
        uses_collection: bool,
    ) -> None:
        self.uses_collection = uses_collection
        result = task()
        on_done(types.SimpleNamespace(result=lambda: result))

    def run_on_main(self, callback: Callable[[], None]) -> None:
        callback()


class FakeAddonManager:
    def __init__(self) -> None:
        self.written_config: list[tuple[str, dict[str, Any]]] = []

    def get_logger(self, _addon: str) -> CapturingLogger:
        return CapturingLogger()

    def writeConfig(self, addon: str, config: dict[str, Any]) -> None:
        self.written_config.append((addon, config))


class FakeNote:
    def __init__(
        self,
        *,
        note_id: int = 123,
        mid: int = 7,
        fields: tuple[tuple[str, str], ...] = (("Front", "front"), ("Back", "back")),
        tags: tuple[str, ...] = (),
    ) -> None:
        self.id = note_id
        self.mid = mid
        self.tags = list(tags)
        self.fields = [html for _name, html in fields]
        self._fields = fields

    def items(self) -> tuple[tuple[str, str], ...]:
        return self._fields


class FakeMutableNote(FakeNote):
    def __init__(
        self,
        *,
        field_names: tuple[str, ...],
        note_id: int,
        mid: int,
        fields: tuple[str, ...],
        tags: tuple[str, ...] = (),
    ) -> None:
        super().__init__(
            note_id=note_id,
            mid=mid,
            fields=tuple(zip(field_names, fields, strict=True)),
            tags=tags,
        )
        self.field_names = field_names

    def items(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.field_names, self.fields, strict=True))


class FakeCardForSnapshot:
    def __init__(
        self,
        *,
        card_id: int,
        note: FakeMutableNote,
        ord: int,
        template_name: str,
        deck_id: int = 1,
        deck_name: str = "Default",
    ) -> None:
        self.id = card_id
        self._note = note
        self.ord = ord
        self.deck_id = deck_id
        self.deck_name = deck_name

    def note(self) -> FakeMutableNote:
        return self._note

    def note_type(self) -> dict[str, Any]:
        return {
            "id": self._note.mid,
            "name": "Basic" if self._note.mid == 7 else "Cloze",
            "tmpls": [{"name": self.template()["name"]}],
        }

    def template(self) -> dict[str, Any]:
        return {"name": f"Card {self.ord + 1}"}

    def current_deck_id(self) -> int:
        return self.deck_id


class FakeDecks:
    def name(self, deck_id: int) -> str:
        return "Default" if deck_id == 1 else f"Deck {deck_id}"


class FakeCollectionForCards:
    def __init__(self, cards: dict[int, FakeCardForSnapshot]) -> None:
        self.cards = cards
        self.decks = FakeDecks()

    def get_card(self, card_id: int) -> FakeCardForSnapshot:
        return self.cards[card_id]


class FakeCollectionForNotes:
    def __init__(self, notes: dict[int, FakeMutableNote]) -> None:
        self.notes = notes

    def get_note(self, note_id: int) -> FakeMutableNote:
        return self.notes[note_id]


class FakeVisibleWidget:
    def __init__(self, visible: bool = True) -> None:
        self.visible = visible
        self.enabled = True

    def show(self) -> None:
        self.visible = True

    def hide(self) -> None:
        self.visible = False

    def setVisible(self, visible: bool) -> None:
        self.visible = visible

    def isVisible(self) -> bool:
        return self.visible

    def setEnabled(self, enabled: bool) -> None:
        self.enabled = enabled


class FakeSplitter:
    def __init__(self, widget: FakeVisibleWidget) -> None:
        self._widget = widget

    def widget(self, index: int) -> FakeVisibleWidget:
        assert index == 1
        return self._widget


class FakeSelectionTable:
    def __init__(self, count: int) -> None:
        self.count = count

    def len_selection(self) -> int:
        return self.count


class FakeBrowserForPane:
    pass


class FakeBrowserPane(FakeVisibleWidget):
    def __init__(self) -> None:
        super().__init__(False)
        self.context_changes = 0

    def on_browser_context_changed(self) -> None:
        self.context_changes += 1


class FutureThatMustNotBeRead:
    def __init__(self) -> None:
        self.read = False

    def result(self) -> object:
        self.read = True
        raise AssertionError("stale agent completion should be ignored")


class FutureWithResult:
    def __init__(self, result: object) -> None:
        self._result = result

    def result(self) -> object:
        return self._result


class FutureWithException:
    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def result(self) -> object:
        raise self.exc


def _import_runtime_with_aqt_stubs(monkeypatch: pytest.MonkeyPatch) -> Any:
    sys.modules.pop("editor_agent_pane.runtime", None)

    class Widget:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def keyPressEvent(self, _event: Any) -> None:
            pass

    class EditorMode:
        ADD_CARDS = object()
        BROWSER = object()

    class Qt:
        class DockWidgetArea:
            LeftDockWidgetArea = 1
            RightDockWidgetArea = 2

        class Key:
            Key_Return = 1
            Key_Enter = 2
            Key_Up = 3
            Key_Down = 4

        class KeyboardModifier:
            ShiftModifier = 4

        class Orientation:
            Vertical = 1

        class TextFormat:
            PlainText = 1

        class ArrowType:
            RightArrow = 1
            DownArrow = 2

    class QTextCursor:
        class MoveOperation:
            End = 1

    class QFileDialog:
        class Option:
            ShowDirsOnly = 1

    aqt_module = types.ModuleType("aqt")
    aqt_module.__path__ = []
    gui_hooks = types.SimpleNamespace(
        browser_will_show=[],
        browser_did_change_row=[],
        editor_did_init=[],
        editor_did_init_buttons=[],
        editor_did_load_note=[],
    )
    aqt_module.gui_hooks = gui_hooks
    aqt_module.mw = None

    editor_module = types.ModuleType("aqt.editor")
    editor_module.Editor = Widget
    editor_module.EditorMode = EditorMode
    aqt_module.editor = editor_module

    operations_module = types.ModuleType("aqt.operations")
    operations_module.__path__ = []
    note_module = types.ModuleType("aqt.operations.note")
    note_module.update_note = lambda *args, **kwargs: None
    note_module.update_notes = lambda *args, **kwargs: None
    operations_module.note = note_module
    aqt_module.operations = operations_module

    qt_module = types.ModuleType("aqt.qt")
    for name in (
        "QAction",
        "QCheckBox",
        "QComboBox",
        "QDialog",
        "QDialogButtonBox",
        "QDockWidget",
        "QFormLayout",
        "QHBoxLayout",
        "QLabel",
        "QLineEdit",
        "QMainWindow",
        "QPlainTextEdit",
        "QPushButton",
        "QSplitter",
        "QTextCursor",
        "QToolButton",
        "QTimer",
        "QVBoxLayout",
        "QWidget",
    ):
        setattr(qt_module, name, Widget)
    qt_module.QTextCursor = QTextCursor
    qt_module.QFileDialog = QFileDialog
    qt_module.Qt = Qt
    qt_module.qconnect = lambda *args, **kwargs: None

    utils_module = types.ModuleType("aqt.utils")
    utils_module.openFolder = lambda *args, **kwargs: None
    utils_module.showWarning = lambda *args, **kwargs: None
    utils_module.tooltip = lambda *args, **kwargs: None

    webview_module = types.ModuleType("aqt.webview")
    webview_module.AnkiWebView = Widget

    monkeypatch.setitem(sys.modules, "aqt", aqt_module)
    monkeypatch.setitem(sys.modules, "aqt.gui_hooks", gui_hooks)
    monkeypatch.setitem(sys.modules, "aqt.editor", editor_module)
    monkeypatch.setitem(sys.modules, "aqt.operations", operations_module)
    monkeypatch.setitem(sys.modules, "aqt.operations.note", note_module)
    monkeypatch.setitem(sys.modules, "aqt.qt", qt_module)
    monkeypatch.setitem(sys.modules, "aqt.utils", utils_module)
    monkeypatch.setitem(sys.modules, "aqt.webview", webview_module)

    return importlib.import_module("editor_agent_pane.runtime")


def _pane_for_runtime(
    runtime: Any,
    *,
    mode: object,
    current_note_id: int | None,
    last_browser_note_id: int | None,
) -> Any:
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    note = None
    if current_note_id is not None:
        note = types.SimpleNamespace(id=current_note_id)
    pane.editor = types.SimpleNamespace(editorMode=mode, note=note)
    pane.history = [("old prompt", "old answer")]
    pane.pending_patch = object()
    pane.pending_snapshot = object()
    pane._activity_id = "agent-activity-1"
    pane._activity_open = True
    pane._agent_stop_event = None
    pane._context_generation = 0
    pane._last_browser_note_id = last_browser_note_id
    pane._selected_text_snapshot = None
    pane._selection_context_refresh_pending = False
    pane._selection_context_request_id = 0
    pane.surface = FakeSurface()
    pane.send_button = FakeButton(False)
    pane.stop_button = FakeButton(True)
    pane.apply_button = FakeButton(True)
    pane.prompt = FakePrompt("draft prompt")
    pane.selection_context_label = FakeLabel()
    pane.refresh_calls = 0

    def refresh_context_label() -> None:
        pane.refresh_calls += 1

    pane.refresh_context_label = refresh_context_label
    return pane


def _pane_for_agent_request(runtime: Any, tmp_path: Path) -> Any:
    media_dir = tmp_path / "collection.media"
    media_dir.mkdir()
    note = FakeNote(fields=(("Front", "<p>front</p>"), ("Back", "<p>back</p>")))
    editor = types.SimpleNamespace(
        editorMode=runtime.EditorMode.BROWSER,
        note=note,
        currentField=0,
        card=None,
        mw=types.SimpleNamespace(
            col=types.SimpleNamespace(media=FakeMediaManager(media_dir, {}))
        ),
    )
    editor.note_type = lambda: {"name": "Basic"}

    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.editor = editor
    pane.history = []
    pane.pending_patch = None
    pane.pending_snapshot = None
    pane._activity_id = None
    pane._activity_counter = 0
    pane._activity_open = False
    pane._agent_stop_event = None
    pane._context_generation = 0
    pane._last_browser_note_id = 123
    pane._selected_text_snapshot = None
    pane._selection_context_refresh_pending = False
    pane._selection_context_request_id = 0
    pane.surface = FakeSurface()
    pane.send_button = FakeButton(True)
    pane.stop_button = FakeButton(False)
    pane.apply_button = FakeButton(True)
    pane.selection_context_label = FakeLabel()
    pane.codex_path_edit = types.SimpleNamespace(text=lambda: "")
    pane.ollama_path_edit = types.SimpleNamespace(text=lambda: "")
    pane.ollama_host_edit = types.SimpleNamespace(text=lambda: "")
    pane.claude_path_edit = types.SimpleNamespace(text=lambda: "")
    pane._provider = lambda: PROVIDER_CODEX
    pane._saved_model_for_provider = (
        lambda provider, config: str(config["ollama_model"])
        if provider == PROVIDER_OLLAMA
        else str(config["codex_model"])
    )
    pane._model_text = lambda: ""
    pane._reasoning_effort = lambda: ""
    pane._project_folder_text = lambda: ""
    pane._project_folder_access = lambda: PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE
    pane._custom_instructions_text = lambda: ""
    pane._fast_mode = lambda: False
    pane._stream_reasoning_summaries = lambda: True
    return pane


def _prompt_edit_for_history(
    runtime: Any,
    history: tuple[str, ...],
    *,
    text: str = "draft",
    cursor_block: int = 0,
    block_count: int = 1,
) -> Any:
    prompt = runtime.PromptEdit.__new__(runtime.PromptEdit)
    prompt.text = text
    prompt.cursor_block = cursor_block
    prompt.block_count = block_count
    prompt.moved_to_end = False
    prompt.sent = False
    prompt._send_callback = lambda: setattr(prompt, "sent", True)
    prompt._history_callback = lambda: history
    prompt._history_index = None
    prompt._draft_text = None
    prompt.toPlainText = lambda: prompt.text

    def set_plain_text(value: str) -> None:
        prompt.text = value
        prompt.block_count = value.count("\n") + 1
        prompt.cursor_block = prompt.block_count - 1

    prompt.setPlainText = set_plain_text
    prompt.textCursor = lambda: types.SimpleNamespace(
        blockNumber=lambda: prompt.cursor_block
    )
    prompt.document = lambda: types.SimpleNamespace(
        blockCount=lambda: prompt.block_count
    )
    prompt.moveCursor = lambda _operation: setattr(prompt, "moved_to_end", True)
    return prompt


def test_prompt_edit_up_down_recall_history_and_restore_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    prompt = _prompt_edit_for_history(
        runtime,
        ("Newest prompt", "Older prompt"),
        text="draft prompt",
    )

    up = FakeKeyEvent(runtime.Qt.Key.Key_Up)
    prompt.keyPressEvent(up)

    assert up.accepted is True
    assert prompt.text == "Newest prompt"
    assert prompt.moved_to_end is True

    prompt.keyPressEvent(FakeKeyEvent(runtime.Qt.Key.Key_Up))
    assert prompt.text == "Older prompt"

    prompt.keyPressEvent(FakeKeyEvent(runtime.Qt.Key.Key_Down))
    assert prompt.text == "Newest prompt"

    prompt.keyPressEvent(FakeKeyEvent(runtime.Qt.Key.Key_Down))
    assert prompt.text == "draft prompt"
    assert prompt._history_index is None


def test_prompt_edit_arrows_do_not_recall_history_away_from_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    prompt = _prompt_edit_for_history(
        runtime,
        ("Newest prompt",),
        text="line 1\nline 2",
        cursor_block=1,
        block_count=2,
    )

    up = FakeKeyEvent(runtime.Qt.Key.Key_Up)
    prompt.keyPressEvent(up)

    assert up.accepted is False
    assert prompt.text == "line 1\nline 2"

    prompt = _prompt_edit_for_history(
        runtime,
        ("Newest prompt",),
        text="line 1\nline 2",
        cursor_block=0,
        block_count=2,
    )
    down = FakeKeyEvent(runtime.Qt.Key.Key_Down)
    prompt.keyPressEvent(down)

    assert down.accepted is False
    assert prompt.text == "line 1\nline 2"


def test_browser_note_change_clears_agent_chat_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.BROWSER,
        current_note_id=456,
        last_browser_note_id=123,
    )

    pane.on_editor_context_changed()

    assert pane.refresh_calls == 1
    assert pane._last_browser_note_id == 456
    assert pane._context_generation == 1
    assert pane.history == []
    assert pane.pending_patch is None
    assert pane.pending_snapshot is None
    assert pane._activity_id is None
    assert pane._activity_open is False
    assert pane.send_button.enabled is True
    assert pane.stop_button.enabled is False
    assert pane.apply_button.enabled is False
    assert pane.surface.evals == [
        "window.agentPane.clearTranscript();",
        "window.agentPane.clearProposal();",
    ]


def test_browser_same_note_keeps_agent_chat_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.BROWSER,
        current_note_id=123,
        last_browser_note_id=123,
    )

    pane.on_editor_context_changed()

    assert pane.refresh_calls == 1
    assert pane.history == [("old prompt", "old answer")]
    assert pane.pending_patch is not None
    assert pane._context_generation == 0
    assert pane.surface.evals == []


def test_non_browser_note_change_keeps_agent_chat_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.ADD_CARDS,
        current_note_id=456,
        last_browser_note_id=123,
    )

    pane.on_editor_context_changed()

    assert pane.refresh_calls == 1
    assert pane.history == [("old prompt", "old answer")]
    assert pane.pending_patch is not None
    assert pane._context_generation == 0
    assert pane.surface.evals == []


def test_browser_note_change_cancels_active_run_and_ignores_stale_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.BROWSER,
        current_note_id=None,
        last_browser_note_id=123,
    )
    stop_event = runtime.Event()
    pane._agent_stop_event = stop_event

    pane.on_editor_context_changed()

    assert stop_event.is_set()
    assert pane._agent_stop_event is None
    assert pane._context_generation == 1

    pane._start_agent_request("old prompt", generation=0)

    stale_future = FutureThatMustNotBeRead()
    pane._handle_agent_done(
        stale_future,
        generation=0,
        stop_event=stop_event,
        prompt="old",
        snapshot=snapshot(),
        notetype={},
        activity=object(),
    )

    assert stale_future.read is False
    assert pane.history == []
    assert pane.surface.evals == [
        "window.agentPane.clearTranscript();",
        "window.agentPane.clearProposal();",
    ]


def test_reset_chat_button_clears_agent_chat_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.ADD_CARDS,
        current_note_id=123,
        last_browser_note_id=None,
    )

    pane._reset_chat()

    assert pane._context_generation == 1
    assert pane.history == []
    assert pane.pending_patch is None
    assert pane.pending_snapshot is None
    assert pane._activity_id is None
    assert pane._activity_open is False
    assert pane.send_button.enabled is True
    assert pane.stop_button.enabled is False
    assert pane.apply_button.enabled is False
    assert pane.prompt.text == "draft prompt"
    assert pane.prompt.cleared is False
    assert pane.surface.evals == [
        "window.agentPane.clearTranscript();",
        "window.agentPane.clearProposal();",
    ]


def test_reset_chat_button_cancels_active_run_and_ignores_stale_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.ADD_CARDS,
        current_note_id=123,
        last_browser_note_id=None,
    )
    stop_event = runtime.Event()
    pane._agent_stop_event = stop_event

    pane._reset_chat()

    assert stop_event.is_set()
    assert pane._agent_stop_event is None
    assert pane._context_generation == 1

    pane._start_agent_request("old prompt", generation=0)

    stale_future = FutureThatMustNotBeRead()
    pane._handle_agent_done(
        stale_future,
        generation=0,
        stop_event=stop_event,
        prompt="old",
        snapshot=snapshot(),
        notetype={},
        activity=object(),
    )

    assert stale_future.read is False
    assert pane.history == []
    assert pane.prompt.text == "draft prompt"
    assert pane.prompt.cleared is False
    assert pane.surface.evals == [
        "window.agentPane.clearTranscript();",
        "window.agentPane.clearProposal();",
    ]


def test_agent_done_summarizes_success_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.ADD_CARDS,
        current_note_id=123,
        last_browser_note_id=None,
    )
    stop_event = runtime.Event()
    pane._agent_stop_event = stop_event
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 12.4)

    pane._handle_agent_done(
        FutureWithResult(
            (
                "Done",
                "<p>Done</p>",
                (),
                "[Codex activity: 2 stream events]\n",
                ("[tool] rg canonical",),
            )
        ),
        generation=0,
        stop_event=stop_event,
        prompt="Explain",
        snapshot=snapshot(),
        notetype={},
        activity=types.SimpleNamespace(detail_lines=[]),
        started_at=5.0,
    )

    assert any("took 7.4s" in eval for eval in pane.surface.evals)
    assert pane.history == [("old prompt", "old answer"), ("Explain", "Done")]


def test_agent_done_records_prompt_history_with_captured_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.ADD_CARDS,
        current_note_id=123,
        last_browser_note_id=None,
    )
    stop_event = runtime.Event()
    pane._agent_stop_event = stop_event
    config = dict(runtime.DEFAULT_CONFIG)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "_config", lambda: config)
    monkeypatch.setattr(runtime, "_write_config", lambda data: saved.update(data))

    pane._handle_agent_done(
        FutureWithResult(
            (
                "Done",
                "<p>Done</p>",
                (),
                "[Codex activity: 2 stream events]\n",
                (),
            )
        ),
        generation=0,
        stop_event=stop_event,
        prompt="Explain",
        snapshot=snapshot(),
        notetype={},
        activity=types.SimpleNamespace(detail_lines=[]),
        prompt_history_scope=(PROVIDER_CODEX, "gpt-5.4"),
    )

    assert saved["prompt_history_by_model"] == {
        PROVIDER_CODEX: {"gpt-5.4": ["Explain"]}
    }


def test_agent_done_summarizes_stopped_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.ADD_CARDS,
        current_note_id=123,
        last_browser_note_id=None,
    )
    stop_event = runtime.Event()
    pane._agent_stop_event = stop_event
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 12.4)

    pane._handle_agent_done(
        FutureWithException(AgentStopped("stopped")),
        generation=0,
        stop_event=stop_event,
        prompt="Explain",
        snapshot=snapshot(),
        notetype={},
        activity=types.SimpleNamespace(detail_lines=["[tool] rg canonical"]),
        started_at=5.0,
    )

    assert any("stopped after 7.4s" in eval for eval in pane.surface.evals)
    assert pane.history == [("old prompt", "old answer")]


def test_agent_done_summarizes_failed_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_runtime(
        runtime,
        mode=runtime.EditorMode.ADD_CARDS,
        current_note_id=123,
        last_browser_note_id=None,
    )
    stop_event = runtime.Event()
    pane._agent_stop_event = stop_event
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 12.4)

    pane._handle_agent_done(
        FutureWithException(RuntimeError("boom")),
        generation=0,
        stop_event=stop_event,
        prompt="Explain",
        snapshot=snapshot(),
        notetype={},
        activity=types.SimpleNamespace(detail_lines=["[tool] rg canonical"]),
        started_at=5.0,
    )

    assert any("failed after 7.4s" in eval for eval in pane.surface.evals)
    assert any("boom" in eval for eval in pane.surface.evals)
    assert pane.history == [("old prompt", "old answer")]


def test_agent_turn_duration_formatting(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)

    assert runtime._format_agent_turn_duration(7.44) == "7.4s"
    assert runtime._format_agent_turn_duration(123.4) == "2m 03s"


def test_selection_context_indicator_updates_and_hides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.selection_context_label = FakeLabel()
    selected = SelectedTextSnapshot(
        field_name="Front",
        field_index=0,
        input_kind="rich_text",
        text="  selected\n text  ",
        html="<strong>selected</strong>",
    )

    pane._set_selected_text_snapshot(selected)

    assert pane._selected_text_snapshot == selected
    assert pane.selection_context_label.visible is True
    assert (
        pane.selection_context_label.text
        == 'Selection context: Front - "selected text"'
    )

    pane._set_selected_text_snapshot(None)

    assert pane._selected_text_snapshot is None
    assert pane.selection_context_label.visible is False
    assert pane.selection_context_label.text == ""


def test_selection_context_refresh_hides_stale_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.editor = types.SimpleNamespace(note=FakeNote())
    pane.dock = FakeDock(True)
    pane.selection_context_label = FakeLabel()
    pane._context_generation = 3
    pane._selection_context_refresh_pending = True
    pane._selection_context_request_id = 11
    pane._set_selected_text_snapshot(
        SelectedTextSnapshot(
            field_name="Front",
            field_index=0,
            input_kind="rich_text",
            text="previous",
            html="<strong>previous</strong>",
        )
    )

    pane._handle_selection_context_result(
        11,
        3,
        {
            "field_name": "Front",
            "field_index": 1,
            "input_kind": "rich_text",
            "text": "stale",
            "html": "<strong>stale</strong>",
        },
    )

    assert pane._selection_context_refresh_pending is False
    assert pane._selected_text_snapshot is None
    assert pane.selection_context_label.visible is False
    assert pane.selection_context_label.text == ""


def test_selection_context_refresh_hides_when_dock_is_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.dock = FakeDock(False)
    pane.selection_context_label = FakeLabel()
    pane._selection_context_refresh_pending = False
    pane._selection_context_request_id = 0
    pane._set_selected_text_snapshot(
        SelectedTextSnapshot(
            field_name="Front",
            field_index=0,
            input_kind="rich_text",
            text="previous",
            html="<strong>previous</strong>",
        )
    )

    pane._refresh_selection_context_from_editor()

    assert pane._selection_context_refresh_pending is False
    assert pane._selected_text_snapshot is None
    assert pane.selection_context_label.visible is False


def test_agent_request_transcript_mentions_validated_selection_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_agent_request(runtime, tmp_path)
    taskman = ImmediateTaskman()
    runtime.aqt.mw = types.SimpleNamespace(
        taskman=taskman,
        addonManager=FakeAddonManager(),
    )
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    captured: dict[str, Any] = {}

    class CapturingAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["agent_kwargs"] = kwargs

        def send(self, **kwargs: Any) -> Any:
            captured["send_kwargs"] = kwargs
            return types.SimpleNamespace(text="", html="", proposals=())

    monkeypatch.setattr(runtime, "CodexCliAgent", CapturingAgent)
    selected_text = {
        "field_name": "Front",
        "field_index": 0,
        "input_kind": "rich_text",
        "text": "selected <text>",
        "html": "<strong>selected</strong>",
    }

    pane._start_agent_request(
        "Improve this",
        generation=0,
        selected_text=selected_text,
    )

    snapshot_sent = captured["send_kwargs"]["snapshot"]
    assert snapshot_sent.selected_text == SelectedTextSnapshot(
        field_name="Front",
        field_index=0,
        input_kind="rich_text",
        text="selected <text>",
        html="<strong>selected</strong>",
    )
    assert pane._selected_text_snapshot == snapshot_sent.selected_text
    assert pane.selection_context_label.visible is True
    assert pane.selection_context_label.text == (
        'Selection context: Front - "selected <text>"'
    )
    assert pane.surface.evals[0].startswith("window.agentPane.appendTranscript(")
    assert "Selection sent from Front" in pane.surface.evals[0]
    assert "selected &lt;text&gt;" in pane.surface.evals[0]
    assert "<strong>selected</strong>" not in pane.surface.evals[0]
    assert captured["agent_kwargs"]["reasoning_effort"] == ""
    assert taskman.uses_collection is False


def test_agent_request_passes_selected_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_agent_request(runtime, tmp_path)
    pane._reasoning_effort = lambda: "high"
    runtime.aqt.mw = types.SimpleNamespace(
        taskman=ImmediateTaskman(),
        addonManager=FakeAddonManager(),
    )
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    captured: dict[str, Any] = {}

    class CapturingAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["agent_kwargs"] = kwargs

        def send(self, **_kwargs: Any) -> Any:
            return types.SimpleNamespace(text="", html="", proposals=())

    monkeypatch.setattr(runtime, "CodexCliAgent", CapturingAgent)

    pane._start_agent_request("Improve this", generation=0)

    assert captured["agent_kwargs"]["reasoning_effort"] == "high"


def test_agent_request_uses_ollama_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_agent_request(runtime, tmp_path)
    pane._provider = lambda: PROVIDER_OLLAMA
    pane._model_text = lambda: "qwen3:latest"
    pane.ollama_path_edit = types.SimpleNamespace(text=lambda: "/usr/local/bin/ollama")
    pane.ollama_host_edit = types.SimpleNamespace(text=lambda: "localhost:11434")
    runtime.aqt.mw = types.SimpleNamespace(
        taskman=ImmediateTaskman(),
        addonManager=FakeAddonManager(),
    )
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    captured: dict[str, Any] = {}

    class CapturingOllamaAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["agent_kwargs"] = kwargs

        def send(self, **kwargs: Any) -> Any:
            captured["send_kwargs"] = kwargs
            return types.SimpleNamespace(text="Done", html="<p>Done</p>", proposals=())

    monkeypatch.setattr(
        runtime,
        "CodexCliAgent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Codex")),
    )
    monkeypatch.setattr(runtime, "OllamaCliAgent", CapturingOllamaAgent)

    pane._start_agent_request("Improve this", generation=0)

    assert captured["agent_kwargs"] == {
        "ollama_path": "/usr/local/bin/ollama",
        "ollama_host": "localhost:11434",
        "model": "qwen3:latest",
        "timeout_seconds": 300,
        "custom_instructions": "",
    }
    assert captured["send_kwargs"]["project_root"] == ""
    assert "event_callback" not in captured["send_kwargs"]
    assert any("Ollama activity" in eval for eval in pane.surface.evals)


def test_agent_request_uses_claude_provider(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_agent_request(runtime, tmp_path)
    pane._provider = lambda: PROVIDER_CLAUDE
    pane._model_text = lambda: "opus"
    pane._reasoning_effort = lambda: "high"
    pane._project_folder_text = lambda: str(tmp_path)
    pane.claude_path_edit = types.SimpleNamespace(text=lambda: "/usr/local/bin/claude")
    runtime.aqt.mw = types.SimpleNamespace(
        taskman=ImmediateTaskman(),
        addonManager=FakeAddonManager(),
    )
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    captured: dict[str, Any] = {}

    class CapturingClaudeAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["agent_kwargs"] = kwargs

        def send(self, **kwargs: Any) -> Any:
            captured["send_kwargs"] = kwargs
            return types.SimpleNamespace(text="Done", html="<p>Done</p>", proposals=())

    monkeypatch.setattr(
        runtime,
        "CodexCliAgent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Codex")),
    )
    monkeypatch.setattr(
        runtime,
        "OllamaCliAgent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Ollama")),
    )
    monkeypatch.setattr(runtime, "ClaudeCliAgent", CapturingClaudeAgent)

    pane._start_agent_request("Improve this", generation=0)

    assert captured["agent_kwargs"] == {
        "claude_path": "/usr/local/bin/claude",
        "model": "opus",
        "timeout_seconds": 300,
        "project_folder_access": PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE,
        "custom_instructions": "",
        "reasoning_effort": "high",
    }
    assert captured["send_kwargs"]["project_root"] == str(tmp_path)
    assert "event_callback" not in captured["send_kwargs"]
    assert any("Claude activity" in eval for eval in pane.surface.evals)


def test_effort_for_provider_drops_unsupported_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    # Claude's "max" must not leak into a Codex run.
    assert runtime._effort_for_provider(PROVIDER_CODEX, "max") == ""
    assert runtime._effort_for_provider(PROVIDER_CODEX, "high") == "high"
    assert runtime._effort_for_provider(PROVIDER_CODEX, "none") == "none"
    # Codex's "none" must not leak into a Claude --effort; "max" is valid there.
    assert runtime._effort_for_provider(PROVIDER_CLAUDE, "none") == ""
    assert runtime._effort_for_provider(PROVIDER_CLAUDE, "max") == "max"
    assert runtime._effort_for_provider(PROVIDER_CLAUDE, "high") == "high"
    # The empty default is always preserved.
    assert runtime._effort_for_provider(PROVIDER_CODEX, "") == ""
    assert runtime._effort_for_provider(PROVIDER_CLAUDE, "") == ""


def test_agent_request_passes_claude_effort_but_drops_codex_max(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_agent_request(runtime, tmp_path)
    pane._provider = lambda: PROVIDER_CODEX
    pane._reasoning_effort = lambda: "max"  # a Claude-only effort
    runtime.aqt.mw = types.SimpleNamespace(
        taskman=ImmediateTaskman(),
        addonManager=FakeAddonManager(),
    )
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    captured: dict[str, Any] = {}

    class CapturingAgent:
        def __init__(self, **kwargs: Any) -> None:
            captured["agent_kwargs"] = kwargs

        def send(self, **_kwargs: Any) -> Any:
            return types.SimpleNamespace(text="", html="", proposals=())

    monkeypatch.setattr(runtime, "CodexCliAgent", CapturingAgent)

    pane._start_agent_request("Improve this", generation=0)

    assert captured["agent_kwargs"]["reasoning_effort"] == ""


def test_agent_request_blocks_ollama_without_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_agent_request(runtime, tmp_path)
    pane._provider = lambda: PROVIDER_OLLAMA
    pane._model_text = lambda: ""
    runtime.aqt.mw = types.SimpleNamespace(
        taskman=ImmediateTaskman(),
        addonManager=FakeAddonManager(),
    )
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    warnings: list[str] = []
    monkeypatch.setattr(
        runtime, "showWarning", lambda message, parent: warnings.append(message)
    )

    pane._start_agent_request("Improve this", generation=0)

    assert warnings == ["Select an Ollama model first."]
    assert pane.surface.evals == []


def test_load_settings_restores_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    restored: dict[str, Any] = {}
    pane.provider_combo = types.SimpleNamespace(setCurrentIndex=lambda index: None)
    pane.codex_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.ollama_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.ollama_host_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.claude_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane._set_model_choice = lambda model: None
    pane._set_project_folder_choices = lambda folder, recent: None
    pane._set_project_folder_access = lambda access: None
    pane._set_effort_choice = lambda effort: restored.update(effort=effort)
    pane.fast_mode_checkbox = types.SimpleNamespace(setChecked=lambda checked: None)
    pane.reasoning_checkbox = types.SimpleNamespace(setChecked=lambda checked: None)
    pane.instructions_edit = types.SimpleNamespace(setPlainText=lambda text: None)
    pane._set_instructions_collapsed = lambda collapsed: None
    pane.text_splitter = types.SimpleNamespace(setSizes=lambda sizes: None)
    pane._update_provider_controls = lambda: None
    pane._provider = lambda: PROVIDER_CODEX
    pane._model_text = lambda: ""
    config = dict(runtime.DEFAULT_CONFIG)
    config["reasoning_effort"] = "xhigh"
    monkeypatch.setattr(runtime, "_config", lambda: config)

    pane._load_settings()

    assert restored["effort"] == "xhigh"


def test_load_settings_restores_model_scoped_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.provider_combo = types.SimpleNamespace(setCurrentIndex=lambda index: None)
    pane.codex_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.ollama_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.ollama_host_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.claude_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane._set_model_choice = lambda model: None
    pane._set_project_folder_choices = lambda folder, recent: None
    pane._set_project_folder_access = lambda access: None
    pane._set_effort_choice = lambda effort: None
    pane.fast_mode_checkbox = types.SimpleNamespace(setChecked=lambda checked: None)
    pane.reasoning_checkbox = types.SimpleNamespace(setChecked=lambda checked: None)
    pane.instructions_edit = FakeInstructionsEdit()
    pane._set_instructions_collapsed = lambda collapsed: None
    pane.text_splitter = types.SimpleNamespace(setSizes=lambda sizes: None)
    pane._update_provider_controls = lambda: None
    pane._provider = lambda: PROVIDER_CODEX
    pane._model_text = lambda: "gpt-5.4"
    config = dict(runtime.DEFAULT_CONFIG)
    config["codex_model"] = "gpt-5.4"
    config["custom_instructions"] = "legacy instructions"
    config["custom_instructions_by_model"] = {
        PROVIDER_CODEX: {"gpt-5.4": "codex scoped instructions"},
        PROVIDER_OLLAMA: {"qwen3:latest": "ollama scoped instructions"},
    }
    monkeypatch.setattr(runtime, "_config", lambda: config)

    pane._load_settings()

    assert pane.instructions_edit.text == "codex scoped instructions"
    assert pane._instructions_scope == (PROVIDER_CODEX, "gpt-5.4")


def test_model_scoped_instructions_fall_back_to_legacy_global_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    config = dict(runtime.DEFAULT_CONFIG)
    config["custom_instructions"] = "legacy instructions"
    config["custom_instructions_by_model"] = {
        PROVIDER_OLLAMA: {"qwen3:latest": "ollama scoped instructions"}
    }

    instructions = runtime._custom_instructions_for_model(
        config,
        provider=PROVIDER_CODEX,
        model="gpt-5.5",
    )

    assert instructions == "legacy instructions"


def test_load_settings_restores_collapsed_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.provider_combo = types.SimpleNamespace(setCurrentIndex=lambda index: None)
    pane.codex_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.ollama_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.ollama_host_edit = types.SimpleNamespace(setText=lambda text: None)
    pane.claude_path_edit = types.SimpleNamespace(setText=lambda text: None)
    pane._set_model_choice = lambda model: None
    pane._set_project_folder_choices = lambda folder, recent: None
    pane._set_project_folder_access = lambda access: None
    pane._set_effort_choice = lambda effort: None
    pane.fast_mode_checkbox = types.SimpleNamespace(setChecked=lambda checked: None)
    pane.reasoning_checkbox = types.SimpleNamespace(setChecked=lambda checked: None)
    pane.instructions_edit = FakeInstructionsEdit()
    pane.instructions_reset_button = FakeButton()
    pane.instructions_toggle_button = FakeToolButton()
    pane.text_splitter = types.SimpleNamespace(setSizes=lambda sizes: None)
    pane._update_provider_controls = lambda: None
    pane._provider = lambda: PROVIDER_CODEX
    pane._model_text = lambda: ""
    config = dict(runtime.DEFAULT_CONFIG)
    config["instructions_collapsed"] = True
    monkeypatch.setattr(runtime, "_config", lambda: config)

    pane._load_settings()

    assert pane._instructions_collapsed is True
    assert pane.instructions_edit.visible is False
    assert pane.instructions_reset_button.visible is False
    assert pane.instructions_toggle_button.arrow_type == runtime.Qt.ArrowType.RightArrow


def test_toggle_instructions_collapsed_saves_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane._instructions_collapsed = False
    pane._loading_settings = False
    pane.instructions_edit = FakeInstructionsEdit()
    pane.instructions_reset_button = FakeButton()
    pane.instructions_toggle_button = FakeToolButton()
    saves: list[bool] = []
    pane._save_settings = lambda: saves.append(True)

    pane._toggle_instructions_collapsed()

    assert pane._instructions_collapsed is True
    assert pane.instructions_edit.visible is False
    assert pane.instructions_reset_button.visible is False
    assert pane.instructions_toggle_button.arrow_type == runtime.Qt.ArrowType.RightArrow
    assert saves == [True]


def test_agent_request_drops_stale_selection_from_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_agent_request(runtime, tmp_path)
    runtime.aqt.mw = types.SimpleNamespace(
        taskman=ImmediateTaskman(),
        addonManager=FakeAddonManager(),
    )
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    captured: dict[str, Any] = {}

    class CapturingAgent:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def send(self, **kwargs: Any) -> Any:
            captured["snapshot"] = kwargs["snapshot"]
            return types.SimpleNamespace(text="", html="", proposals=())

    monkeypatch.setattr(runtime, "CodexCliAgent", CapturingAgent)

    pane._start_agent_request(
        "Improve this",
        generation=0,
        selected_text={
            "field_name": "Front",
            "field_index": 1,
            "input_kind": "rich_text",
            "text": "stale",
            "html": "<strong>stale</strong>",
        },
    )

    assert captured["snapshot"].selected_text is None
    assert pane._selected_text_snapshot is None
    assert pane.selection_context_label.visible is False
    assert "Selection sent" not in pane.surface.evals[0]


def test_fast_mode_toggle_saves_settings_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    calls: list[bool] = []
    pane._save_settings = lambda: calls.append(True)

    pane._on_fast_mode_toggled(True)

    assert calls == [True]


def test_save_settings_persists_fast_mode_and_no_project_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.project_edit = FakeProjectCombo(NO_PROJECT_FOLDER_LABEL)
    pane.codex_path_edit = types.SimpleNamespace(text=lambda: "/usr/bin/codex")
    pane.ollama_path_edit = types.SimpleNamespace(text=lambda: "/usr/bin/ollama")
    pane.ollama_host_edit = types.SimpleNamespace(text=lambda: "localhost:11434")
    pane.claude_path_edit = types.SimpleNamespace(text=lambda: "/usr/bin/claude")
    pane._provider = lambda: PROVIDER_CODEX
    pane._model_text = lambda: "gpt-5.5"
    pane._custom_instructions_text = lambda: "be concise"
    pane._project_folder_access = lambda: PROJECT_FOLDER_ACCESS_READ_ONLY
    pane._reasoning_effort = lambda: "high"
    pane._fast_mode = lambda: True
    pane._stream_reasoning_summaries = lambda: False
    pane._instructions_are_collapsed = lambda: True
    config = dict(runtime.DEFAULT_CONFIG)
    config["custom_instructions"] = "legacy fallback"
    config["recent_project_folders"] = ["/one", "/two"]
    monkeypatch.setattr(runtime, "_config", lambda: config)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "_write_config", lambda data: saved.update(data))

    pane._save_settings()

    assert saved["codex_path"] == "/usr/bin/codex"
    assert saved["ollama_path"] == "/usr/bin/ollama"
    assert saved["ollama_host"] == "http://localhost:11434"
    assert saved["claude_path"] == "/usr/bin/claude"
    assert saved["provider"] == PROVIDER_CODEX
    assert saved["codex_model"] == "gpt-5.5"
    assert saved["custom_instructions"] == "legacy fallback"
    assert saved["custom_instructions_by_model"] == {
        PROVIDER_CODEX: {"gpt-5.5": "be concise"}
    }
    assert saved["instructions_collapsed"] is True
    assert saved["project_folder"] == ""
    assert saved["project_folder_access"] == PROJECT_FOLDER_ACCESS_READ_ONLY
    assert saved["reasoning_effort"] == "high"
    assert saved["fast_mode"] is True
    assert saved["stream_reasoning_summaries"] is False
    assert saved["recent_project_folders"] == ["/one", "/two"]


def test_agent_effort_options_include_default_and_known_efforts() -> None:
    assert EFFORT_OPTIONS == (
        ("Codex default", ""),
        ("None", "none"),
        ("Low", "low"),
        ("Medium", "medium"),
        ("High", "high"),
        ("XHigh", "xhigh"),
    )
    assert effort_option_index("") == 0


def test_agent_effort_options_round_trip_known_efforts() -> None:
    for expected_index, (_label, effort) in enumerate(EFFORT_OPTIONS):
        assert effort_options_with_legacy(effort) == EFFORT_OPTIONS
        assert effort_option_index(effort) == expected_index


def test_agent_effort_options_preserve_unknown_legacy_effort() -> None:
    options = effort_options_with_legacy(" experimental ")

    assert options[:-1] == EFFORT_OPTIONS
    assert options[-1] == ("experimental", "experimental")
    assert effort_option_index("experimental") == len(options) - 1


def test_agent_effort_options_reset_unsupported_minimal_effort() -> None:
    assert effort_options_with_legacy("minimal") == EFFORT_OPTIONS
    assert effort_option_index("minimal") == 0


def test_agent_model_options_include_default_and_known_models() -> None:
    assert MODEL_OPTIONS == (
        ("Codex default", ""),
        ("gpt-5.5", "gpt-5.5"),
        ("gpt-5.4", "gpt-5.4"),
        ("gpt-5.4-mini", "gpt-5.4-mini"),
        ("gpt-5.3-codex", "gpt-5.3-codex"),
        ("gpt-5.3-codex-spark", "gpt-5.3-codex-spark"),
        ("gpt-5.2", "gpt-5.2"),
    )
    assert model_option_index("") == 0


def test_agent_model_options_round_trip_known_models() -> None:
    for expected_index, (_label, model) in enumerate(MODEL_OPTIONS):
        assert model_options_with_legacy(model) == MODEL_OPTIONS
        assert model_option_index(model) == expected_index


def test_agent_model_options_preserve_unknown_legacy_model() -> None:
    options = model_options_with_legacy(" gpt-legacy ")

    assert options[:-1] == MODEL_OPTIONS
    assert options[-1] == ("gpt-legacy", "gpt-legacy")
    assert model_option_index("gpt-legacy") == len(options) - 1


def test_agent_provider_options_default_to_codex() -> None:
    assert provider_value("") == PROVIDER_CODEX
    assert provider_value("ollama") == PROVIDER_OLLAMA
    assert provider_value("unknown") == PROVIDER_CODEX
    assert provider_option_index(PROVIDER_OLLAMA) == 1


def test_agent_provider_options_include_claude() -> None:
    assert provider_value("claude") == PROVIDER_CLAUDE
    assert provider_value(" claude ") == PROVIDER_CLAUDE
    assert ("Claude", PROVIDER_CLAUDE) in PROVIDER_OPTIONS
    assert provider_option_index(PROVIDER_CLAUDE) == 2


def test_claude_model_options_include_default_and_aliases() -> None:
    assert CLAUDE_MODEL_OPTIONS[0] == ("Claude default", "")
    values = [value for _label, value in CLAUDE_MODEL_OPTIONS]
    assert values == ["", "opus", "sonnet", "haiku"]
    assert (
        model_options_with_legacy("opus", CLAUDE_MODEL_OPTIONS) == CLAUDE_MODEL_OPTIONS
    )
    assert model_option_index("sonnet", CLAUDE_MODEL_OPTIONS) == 2
    assert model_option_index("", CLAUDE_MODEL_OPTIONS) == 0


def test_claude_model_options_preserve_unknown_legacy_model() -> None:
    options = model_options_with_legacy(" claude-opus-4-8 ", CLAUDE_MODEL_OPTIONS)
    assert options[-1] == ("claude-opus-4-8", "claude-opus-4-8")
    assert (
        model_option_index("claude-opus-4-8", CLAUDE_MODEL_OPTIONS) == len(options) - 1
    )


def test_claude_effort_options_offer_max_and_drop_none() -> None:
    values = [value for _label, value in CLAUDE_EFFORT_OPTIONS]
    assert values == ["", "low", "medium", "high", "xhigh", "max"]
    assert "none" not in values
    for index, (_label, value) in enumerate(CLAUDE_EFFORT_OPTIONS):
        assert effort_options_with_legacy(value, CLAUDE_EFFORT_OPTIONS) == (
            CLAUDE_EFFORT_OPTIONS
        )
        assert effort_option_index(value, CLAUDE_EFFORT_OPTIONS) == index
    # A Codex-only effort is kept as a legacy entry (the client drops it before
    # passing --effort to the claude CLI; see the ClaudeCliAgent effort tests).
    legacy = effort_options_with_legacy("none", CLAUDE_EFFORT_OPTIONS)
    assert legacy[-1] == ("none", "none")


def test_ollama_model_options_use_discovered_models() -> None:
    models = ("qwen3:30b-a3b-instruct-2507-q4_K_M", "llama3.2:latest")

    assert ollama_model_options_with_legacy("", models) == (
        ("qwen3:30b-a3b-instruct-2507-q4_K_M", "qwen3:30b-a3b-instruct-2507-q4_K_M"),
        ("llama3.2:latest", "llama3.2:latest"),
    )
    assert ollama_model_option_index("llama3.2:latest", models) == 1


def test_ollama_model_options_preserve_saved_missing_model() -> None:
    options = ollama_model_options_with_legacy("custom:latest", ("qwen3:latest",))

    assert options == (
        ("qwen3:latest", "qwen3:latest"),
        ("custom:latest", "custom:latest"),
    )
    assert ollama_model_option_index("custom:latest", ("qwen3:latest",)) == 1


def test_ollama_model_options_show_empty_or_unavailable_state() -> None:
    assert ollama_model_options_with_legacy("", ()) == (
        ("No Ollama models installed", ""),
    )
    assert ollama_model_options_with_legacy("", (), unavailable=True) == (
        ("Ollama unavailable", ""),
    )


def test_parse_ollama_list_extracts_model_names() -> None:
    assert parse_ollama_list(
        "NAME                                  ID              SIZE     MODIFIED\n"
        "qwen3:30b-a3b-instruct-2507-q4_K_M    19e422b02313    18 GB    13 minutes ago\n"
        "llama3.2:latest                       abcdef012345    2.0 GB   2 days ago\n"
    ) == (
        OllamaModelInfo(
            name="qwen3:30b-a3b-instruct-2507-q4_K_M",
            digest="19e422b02313",
        ),
        OllamaModelInfo(name="llama3.2:latest", digest="abcdef012345"),
    )


def test_discover_ollama_models_prefers_local_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Response:
        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            pass

        def read(self) -> bytes:
            return json.dumps(
                {
                    "models": [
                        {
                            "name": "qwen3:latest",
                            "size": 123,
                            "digest": "digest",
                            "modified_at": "2026-06-06T20:59:15+01:00",
                            "details": {
                                "family": "qwen3moe",
                                "quantization_level": "Q4_K_M",
                            },
                        }
                    ]
                }
            ).encode()

    captured: dict[str, Any] = {}

    def fake_urlopen(url: str, *, timeout: float) -> Response:
        captured["url"] = url
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(ollama_client.urllib.request, "urlopen", fake_urlopen)

    models = discover_ollama_models(
        ollama_path="/custom/ollama",
        ollama_host="127.0.0.1:11434",
        timeout_seconds=0.5,
    )

    assert captured == {
        "url": "http://127.0.0.1:11434/api/tags",
        "timeout": 0.5,
    }
    assert models == (
        OllamaModelInfo(
            name="qwen3:latest",
            size=123,
            digest="digest",
            modified_at="2026-06-06T20:59:15+01:00",
            details={"family": "qwen3moe", "quantization_level": "Q4_K_M"},
        ),
    )


def test_discover_ollama_models_falls_back_to_ollama_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ollama_client.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("api down")),
    )
    captured: dict[str, Any] = {}

    def fake_run(
        command: list[str],
        *,
        capture_output: bool,
        text: bool,
        timeout: float,
        env: dict[str, str],
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        captured.update(
            command=command,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            env_host=env["OLLAMA_HOST"],
            check=check,
        )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                "NAME            ID              SIZE     MODIFIED\n"
                "qwen3:latest    19e422b02313    18 GB    13 minutes ago\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert discover_ollama_models(
        ollama_path="/custom/ollama",
        ollama_host="localhost:11434",
        timeout_seconds=0.25,
    ) == (OllamaModelInfo(name="qwen3:latest", digest="19e422b02313"),)
    assert captured["command"] == ["/custom/ollama", "list"]
    assert captured["env_host"] == "http://localhost:11434"


def test_ollama_path_and_host_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama_client.shutil, "which", lambda _command: None)
    monkeypatch.setattr(ollama_client.os.path, "isfile", lambda _path: False)
    monkeypatch.setattr(ollama_client.os, "access", lambda _path, _mode: False)

    assert resolve_ollama_path("") == "ollama"
    assert normalize_ollama_host("") == DEFAULT_OLLAMA_HOST
    assert normalize_ollama_host("localhost:11434") == "http://localhost:11434"
    assert normalize_ollama_host("http://localhost:11434/") == "http://localhost:11434"


def test_ollama_path_prefers_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ollama_client.shutil,
        "which",
        lambda command: "/opt/homebrew/bin/ollama" if command == "ollama" else None,
    )

    assert resolve_ollama_path("") == "/opt/homebrew/bin/ollama"


def test_ollama_path_falls_back_to_common_app_locations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ollama_client.shutil, "which", lambda _command: None)
    monkeypatch.setattr(
        ollama_client.os.path,
        "isfile",
        lambda path: path == "/usr/local/bin/ollama",
    )
    monkeypatch.setattr(
        ollama_client.os,
        "access",
        lambda path, mode: path == "/usr/local/bin/ollama" and mode == os.X_OK,
    )

    assert resolve_ollama_path("") == "/usr/local/bin/ollama"


def test_agent_button_tooltip_includes_shortcut() -> None:
    assert AGENT_BUTTON_LABEL == "Agent"
    assert AGENT_BUTTON_TIP == f"Open the editor agent pane ({AGENT_PANE_SHORTCUT})"


def test_agent_config_streams_reasoning_summaries_by_default() -> None:
    config = json.loads((ROOT / "addons/editor_agent_pane/config.json").read_text())

    assert config["stream_reasoning_summaries"] is True


def test_agent_config_disables_fast_mode_by_default() -> None:
    config = json.loads((ROOT / "addons/editor_agent_pane/config.json").read_text())

    assert config["fast_mode"] is False


def test_agent_config_uses_codex_default_effort_by_default() -> None:
    config = json.loads((ROOT / "addons/editor_agent_pane/config.json").read_text())

    assert config["reasoning_effort"] == ""


def test_agent_config_defaults_to_codex_provider() -> None:
    config = json.loads((ROOT / "addons/editor_agent_pane/config.json").read_text())

    assert config["provider"] == PROVIDER_CODEX
    assert config["codex_model"] == ""
    assert config["ollama_model"] == ""
    assert config["ollama_path"] == ""
    assert config["ollama_host"] == DEFAULT_OLLAMA_HOST
    assert config["instructions_collapsed"] is False
    assert config["prompt_history_by_model"] == {}
    assert "model" not in config


def test_agent_config_normalizes_instruction_and_prompt_history_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    runtime.aqt.mw = types.SimpleNamespace(
        addonManager=types.SimpleNamespace(
            getConfig=lambda _addon: {
                "instructions_collapsed": "yes",
                "prompt_history_by_model": {
                    PROVIDER_CODEX: {
                        " gpt-5.4 ": [f" prompt {index} " for index in range(12)]
                    },
                    PROVIDER_OLLAMA: {"qwen3:latest": "not a list"},
                    "unknown": {"model": ["ignored"]},
                },
            }
        )
    )

    config = runtime._config()

    assert config["instructions_collapsed"] is False
    assert config["prompt_history_by_model"] == {
        PROVIDER_CODEX: {"gpt-5.4": [f"prompt {index}" for index in range(10)]}
    }


def test_prompt_history_is_saved_per_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    config = dict(runtime.DEFAULT_CONFIG)

    for index in range(12):
        runtime._record_prompt_history_for_model(
            config,
            provider=PROVIDER_CODEX,
            model="gpt-5.4",
            prompt=f"Prompt {index}",
        )
    runtime._record_prompt_history_for_model(
        config,
        provider=PROVIDER_CODEX,
        model="gpt-5.5",
        prompt="Other Codex prompt",
    )
    runtime._record_prompt_history_for_model(
        config,
        provider=PROVIDER_OLLAMA,
        model="qwen3:latest",
        prompt="Local prompt",
    )

    assert runtime._prompt_history_for_model(
        config, provider=PROVIDER_CODEX, model="gpt-5.4"
    ) == tuple(f"Prompt {index}" for index in range(11, 1, -1))
    assert runtime._prompt_history_for_model(
        config, provider=PROVIDER_CODEX, model="gpt-5.5"
    ) == ("Other Codex prompt",)
    assert runtime._prompt_history_for_model(
        config, provider=PROVIDER_OLLAMA, model="qwen3:latest"
    ) == ("Local prompt",)


def test_agent_config_migrates_legacy_model_to_codex_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    runtime.aqt.mw = types.SimpleNamespace(
        addonManager=types.SimpleNamespace(
            getConfig=lambda _addon: {
                "model": "gpt-legacy",
                "provider": PROVIDER_OLLAMA,
            }
        )
    )

    config = runtime._config()

    assert config["provider"] == PROVIDER_OLLAMA
    assert config["codex_model"] == "gpt-legacy"
    assert config["custom_instructions_by_model"] == {}


def test_agent_pane_model_change_saves_and_restores_scoped_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.provider_combo = FakeCombo()
    pane.provider_combo.addItem("Codex", PROVIDER_CODEX)
    pane.provider_combo.addItem("Ollama", PROVIDER_OLLAMA)
    pane.provider_combo.setCurrentIndex(0)
    pane.model_combo = FakeCombo()
    pane.instructions_edit = FakeInstructionsEdit()
    pane._ollama_models = ()
    pane._ollama_models_unavailable = False
    pane._loading_settings = False
    pane._setting_model_choice = False
    pane._model_provider = PROVIDER_CODEX
    pane.refresh_context_label = lambda: None
    config = dict(runtime.DEFAULT_CONFIG)
    config["codex_model"] = "gpt-5.4"
    config["custom_instructions_by_model"] = {
        PROVIDER_CODEX: {"gpt-5.5": "second model instructions"}
    }
    monkeypatch.setattr(runtime, "_config", lambda: config)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "_write_config", lambda data: saved.update(data))

    pane._set_model_choice("gpt-5.4")
    pane._load_instructions_for_current_choice(config)
    pane.instructions_edit.text = "first model instructions"
    pane.model_combo.setCurrentIndex(model_option_index("gpt-5.5"))

    pane._on_model_changed(pane.model_combo.current_index)

    assert saved["codex_model"] == "gpt-5.5"
    assert saved["custom_instructions_by_model"][PROVIDER_CODEX] == {
        "gpt-5.4": "first model instructions",
        "gpt-5.5": "second model instructions",
    }
    assert pane.instructions_edit.text == "second model instructions"
    assert pane._instructions_scope == (PROVIDER_CODEX, "gpt-5.5")


def test_agent_pane_model_change_does_not_bleed_current_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.provider_combo = FakeCombo()
    pane.provider_combo.addItem("Codex", PROVIDER_CODEX)
    pane.provider_combo.addItem("Ollama", PROVIDER_OLLAMA)
    pane.provider_combo.setCurrentIndex(0)
    pane.model_combo = FakeCombo()
    pane.instructions_edit = FakeInstructionsEdit()
    pane._ollama_models = ()
    pane._ollama_models_unavailable = False
    pane._loading_settings = False
    pane._setting_model_choice = False
    pane._model_provider = PROVIDER_CODEX
    pane.refresh_context_label = lambda: None
    config = dict(runtime.DEFAULT_CONFIG)
    monkeypatch.setattr(runtime, "_config", lambda: config)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "_write_config", lambda data: saved.update(data))

    pane._set_model_choice("gpt-5.4")
    pane._load_instructions_for_current_choice(config)
    pane.instructions_edit.text = "first model instructions"
    pane.model_combo.setCurrentIndex(model_option_index("gpt-5.5"))

    pane._on_model_changed(pane.model_combo.current_index)

    assert saved["custom_instructions"] == ""
    assert saved["custom_instructions_by_model"][PROVIDER_CODEX] == {
        "gpt-5.4": "first model instructions"
    }
    assert pane.instructions_edit.text == ""


def test_agent_pane_provider_change_saves_and_restores_scoped_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.provider_combo = FakeCombo()
    pane.provider_combo.addItem("Codex", PROVIDER_CODEX)
    pane.provider_combo.addItem("Ollama", PROVIDER_OLLAMA)
    pane.model_combo = FakeCombo()
    pane.effort_combo = FakeCombo()
    pane.instructions_edit = FakeInstructionsEdit()
    pane._ollama_models = ("qwen3:latest",)
    pane._ollama_models_unavailable = False
    pane._loading_settings = False
    pane._setting_model_choice = False
    pane._model_provider = PROVIDER_CODEX
    pane._update_provider_controls = lambda: None
    pane.refresh_context_label = lambda: None
    pane._refresh_ollama_models = lambda: None
    config = dict(runtime.DEFAULT_CONFIG)
    config["codex_model"] = "gpt-5.4"
    config["ollama_model"] = "qwen3:latest"
    config["custom_instructions_by_model"] = {
        PROVIDER_OLLAMA: {"qwen3:latest": "ollama model instructions"}
    }
    monkeypatch.setattr(runtime, "_config", lambda: config)
    saved: dict[str, Any] = {}
    monkeypatch.setattr(runtime, "_write_config", lambda data: saved.update(data))

    pane.provider_combo.setCurrentIndex(0)
    pane._set_model_choice("gpt-5.4")
    pane._load_instructions_for_current_choice(config)
    pane.instructions_edit.text = "codex model instructions"
    pane.provider_combo.setCurrentIndex(1)

    pane._on_provider_changed(1)

    assert saved["provider"] == PROVIDER_OLLAMA
    assert saved["codex_model"] == "gpt-5.4"
    assert saved["custom_instructions_by_model"][PROVIDER_CODEX] == {
        "gpt-5.4": "codex model instructions"
    }
    assert pane.model_combo.currentData() == "qwen3:latest"
    assert pane.instructions_edit.text == "ollama model instructions"
    assert pane._instructions_scope == (PROVIDER_OLLAMA, "qwen3:latest")


def _pane_for_provider_control_visibility(runtime: Any, provider: str) -> Any:
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane._provider = lambda: provider
    pane.codex_path_label = FakeLabel()
    pane.codex_path_edit = FakeLineEdit()
    pane.claude_path_label = FakeLabel()
    pane.claude_path_edit = FakeLineEdit()
    pane.ollama_path_label = FakeLabel()
    pane.ollama_path_edit = FakeLineEdit()
    pane.ollama_host_label = FakeLabel()
    pane.ollama_host_edit = FakeLineEdit()
    pane.model_refresh_button = FakeButton()
    pane.effort_label = FakeLabel()
    pane.effort_combo = FakeCombo()
    pane.fast_mode_label = FakeLabel()
    pane.fast_mode_checkbox = FakeButton()
    pane.access_label = FakeLabel()
    pane.access_combo = FakeCombo()
    pane.reasoning_label = FakeLabel()
    pane.reasoning_checkbox = FakeButton()
    pane.project_row_widget = FakeVisibleWidget()
    return pane


def test_agent_pane_hides_ollama_controls_for_codex_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_provider_control_visibility(runtime, PROVIDER_CODEX)

    pane._update_provider_controls()

    assert pane.codex_path_label.visible is True
    assert pane.codex_path_edit.visible is True
    assert pane.codex_path_edit.enabled is True
    assert pane.claude_path_label.visible is False
    assert pane.claude_path_edit.visible is False
    assert pane.claude_path_edit.enabled is False
    assert pane.ollama_path_label.visible is False
    assert pane.ollama_path_edit.visible is False
    assert pane.ollama_path_edit.enabled is False
    assert pane.ollama_host_label.visible is False
    assert pane.ollama_host_edit.visible is False
    assert pane.model_refresh_button.visible is False
    assert pane.model_refresh_button.enabled is False
    assert pane.effort_label.visible is True
    assert pane.effort_combo.visible is True
    assert pane.fast_mode_checkbox.visible is True
    assert pane.access_label.visible is True
    assert pane.access_combo.visible is True
    assert pane.reasoning_checkbox.visible is True
    assert pane.project_row_widget.visible is True
    assert pane.project_row_widget.enabled is True


def test_agent_pane_hides_codex_controls_for_ollama_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_provider_control_visibility(runtime, PROVIDER_OLLAMA)

    pane._update_provider_controls()

    assert pane.codex_path_label.visible is False
    assert pane.codex_path_edit.visible is False
    assert pane.codex_path_edit.enabled is False
    assert pane.claude_path_label.visible is False
    assert pane.claude_path_edit.visible is False
    assert pane.claude_path_edit.enabled is False
    assert pane.ollama_path_label.visible is True
    assert pane.ollama_path_edit.visible is True
    assert pane.ollama_path_edit.enabled is True
    assert pane.ollama_host_label.visible is True
    assert pane.ollama_host_edit.visible is True
    assert pane.model_refresh_button.visible is True
    assert pane.model_refresh_button.enabled is True
    assert pane.effort_label.visible is False
    assert pane.effort_combo.visible is False
    assert pane.effort_combo.enabled is False
    assert pane.fast_mode_checkbox.visible is False
    assert pane.fast_mode_checkbox.enabled is False
    assert pane.access_label.visible is False
    assert pane.access_combo.visible is False
    assert pane.access_combo.enabled is False
    assert pane.reasoning_checkbox.visible is False
    assert pane.reasoning_checkbox.enabled is False
    assert pane.project_row_widget.visible is False
    assert pane.project_row_widget.enabled is False


def test_agent_pane_shows_claude_controls_for_claude_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = _pane_for_provider_control_visibility(runtime, PROVIDER_CLAUDE)

    pane._update_provider_controls()

    # Claude is agentic like Codex: project folder + effort + access are shown.
    assert pane.claude_path_label.visible is True
    assert pane.claude_path_edit.visible is True
    assert pane.claude_path_edit.enabled is True
    assert pane.effort_label.visible is True
    assert pane.effort_combo.visible is True
    assert pane.effort_combo.enabled is True
    assert pane.access_label.visible is True
    assert pane.access_combo.visible is True
    assert pane.project_row_widget.visible is True
    assert pane.project_row_widget.enabled is True
    # Codex- and Ollama-specific controls stay hidden.
    assert pane.codex_path_label.visible is False
    assert pane.codex_path_edit.visible is False
    assert pane.ollama_path_label.visible is False
    assert pane.ollama_host_label.visible is False
    assert pane.model_refresh_button.visible is False
    assert pane.fast_mode_checkbox.visible is False
    assert pane.reasoning_checkbox.visible is False


def test_agent_pane_model_dropdown_uses_current_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.provider_combo = FakeCombo()
    pane.provider_combo.addItem("Codex", PROVIDER_CODEX)
    pane.provider_combo.addItem("Ollama", PROVIDER_OLLAMA)
    pane.model_combo = FakeCombo()
    pane._ollama_models = ("qwen3:latest",)
    pane._ollama_models_unavailable = False

    pane.provider_combo.setCurrentIndex(0)
    pane._set_model_choice("gpt-5.4")

    assert pane.model_combo.currentData() == "gpt-5.4"
    assert pane._model_provider == PROVIDER_CODEX

    pane.provider_combo.setCurrentIndex(1)
    pane._set_model_choice("")

    assert pane.model_combo.items == [("qwen3:latest", "qwen3:latest")]
    assert pane.model_combo.currentData() == "qwen3:latest"
    assert pane._model_provider == PROVIDER_OLLAMA

    pane._ollama_models = ()
    pane._ollama_models_unavailable = True
    pane._set_model_choice("")

    assert pane.model_combo.items == [("Ollama unavailable", "")]


def test_json_line_agent_run_logger_writes_structured_json() -> None:
    logger = CapturingLogger()
    run_logger = JsonLineAgentRunLogger(logger=logger, run_id="run-123")

    run_logger.record("run_start", count=1, path=Path("/tmp/agent/log"))

    assert len(logger.messages) == 1
    entry = json.loads(logger.messages[0])
    assert entry["run_id"] == "run-123"
    assert entry["event"] == "run_start"
    assert isinstance(entry["ts"], float)
    assert entry["count"] == 1
    assert entry["path"] == "/tmp/agent/log"


def test_agent_log_folder_helper_creates_addon_folder(tmp_path: Path) -> None:
    log_folder = tmp_path / "logs" / "addons" / "editor_agent_pane"
    addon_manager = FakeAddonManagerForLogs(log_folder)

    path = ensure_agent_log_folder(addon_manager, "editor_agent_pane")

    assert path == log_folder
    assert path.is_dir()
    assert addon_manager.requested_addons == ["editor_agent_pane"]


def test_read_source_file_rejects_traversal_and_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(SourceAccessError, match="traversal"):
        read_source_file(root, "../outside.md")

    with pytest.raises(SourceAccessError, match="relative"):
        read_source_file(root, outside)


def test_read_source_file_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = root / "linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    with pytest.raises(SourceAccessError, match="escapes"):
        read_source_file(root, "linked.md")


def test_read_source_file_rejects_binary_and_large_files(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    binary = root / "binary.txt"
    binary.write_bytes(b"hello\x00world")
    large = root / "large.md"
    large.write_text("x" * 20, encoding="utf-8")

    with pytest.raises(SourceAccessError, match="binary"):
        read_source_file(root, "binary.txt")

    with pytest.raises(SourceAccessError, match="large"):
        read_source_file(root, "large.md", max_bytes=10)


def test_search_source_files_is_bounded(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    for index in range(4):
        (root / f"file{index}.md").write_text(
            f"needle line {index}\nneedle again {index}",
            encoding="utf-8",
        )

    hits = search_source_files(root, "needle", max_results=3, max_files=10)

    assert len(hits) == 3
    assert hits[0].path == "file0.md"
    assert hits[0].line == 1


def test_project_folder_choices_include_current_and_clean_history() -> None:
    assert project_folder_choices(
        " /current ",
        ["/two", " /one ", "/two", "", 123],
    ) == [NO_PROJECT_FOLDER_LABEL, "/current", "/two", "/one"]
    assert project_folder_choices(" /one ", ["/two", "/one"]) == [
        NO_PROJECT_FOLDER_LABEL,
        "/one",
        "/two",
    ]


def test_project_folder_choices_include_no_folder_option_without_remembering_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.project_edit = FakeProjectCombo()

    pane._set_project_folder_choices("", ["/one", NO_PROJECT_FOLDER_LABEL, "/two"])

    assert pane.project_edit.items == [NO_PROJECT_FOLDER_LABEL, "/one", "/two"]
    assert pane.project_edit.current_index == 0
    assert pane._project_folder_text() == ""
    assert remember_project_folder(NO_PROJECT_FOLDER_LABEL, ["/one"]) == ["/one"]


def test_remember_project_folder_moves_selection_to_front_and_limits() -> None:
    existing = [f"/project/{index}" for index in range(MAX_RECENT_PROJECT_FOLDERS + 2)]

    remembered = remember_project_folder(" /project/5 ", existing)

    assert remembered[:3] == ["/project/5", "/project/0", "/project/1"]
    assert len(remembered) == MAX_RECENT_PROJECT_FOLDERS
    assert remember_project_folder("", ["/project/1"]) == ["/project/1"]


def test_project_root_status_reflects_access_mode(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    assert project_root_status(str(project), PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE) == (
        f"Writable project folder: {project.resolve()}"
    )
    assert project_root_status(str(project), PROJECT_FOLDER_ACCESS_READ_ONLY) == (
        f"Read-only project folder: {project.resolve()}"
    )


def test_browser_multi_card_snapshot_expands_selected_cards_from_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    note_one = FakeMutableNote(
        note_id=101,
        mid=7,
        field_names=("Front", "Back"),
        fields=("front", "back"),
        tags=("keep",),
    )
    note_two = FakeMutableNote(
        note_id=202,
        mid=8,
        field_names=("Text",),
        fields=("text",),
        tags=("cloze",),
    )
    cards = {
        11: FakeCardForSnapshot(card_id=11, note=note_one, ord=0, template_name="A"),
        12: FakeCardForSnapshot(card_id=12, note=note_one, ord=1, template_name="B"),
        21: FakeCardForSnapshot(card_id=21, note=note_two, ord=0, template_name="C"),
    }
    browser = types.SimpleNamespace(
        selected_cards=lambda: [11, 12, 21],
        col=FakeCollectionForCards(cards),
    )

    snapshot = runtime.browser_multi_card_snapshot(browser)

    assert [card.card_id for card in snapshot.cards] == [11, 12, 21]
    assert [note.note_id for note in snapshot.notes] == [101, 202]
    assert snapshot.cards[1].note_id == 101
    assert snapshot.cards[1].template_name == "Card 2"
    assert snapshot.notes[0].fields[0] == FieldSnapshot("Front", "front")
    assert snapshot.as_tool_result()["mode"] == "browse_multi"


def test_prepare_multi_note_updates_applies_checked_notes_only_and_rejects_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    patch = validate_multi_note_patch(
        {
            "summary": "Tighten selected cards",
            "note_updates": [
                {
                    "note_id": 101,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Front", "html": "new front"}],
                    "tags": {"replace": None, "add": ["agent"], "remove": []},
                },
                {
                    "note_id": 202,
                    "notetype_id": 8,
                    "field_updates": [{"name": "Text", "html": "new text"}],
                    "tags": {"replace": None, "add": [], "remove": []},
                },
            ],
        },
        multi_snapshot(),
    )
    note_one = FakeMutableNote(
        note_id=101,
        mid=7,
        field_names=("Front", "Back"),
        fields=("old front", "old back"),
        tags=("keep", "remove-me"),
    )
    note_two = FakeMutableNote(
        note_id=202,
        mid=8,
        field_names=("Text",),
        fields=("old {{c1::text}}",),
        tags=("cloze",),
    )

    updates = runtime.prepare_multi_note_updates(
        FakeCollectionForNotes({101: note_one, 202: note_two}),
        multi_snapshot(),
        patch,
        {101},
    )

    assert updates == [note_one]
    assert note_one.fields == ["new front", "old back"]
    assert note_one.tags == ["keep", "remove-me", "agent"]
    assert note_two.fields == ["old {{c1::text}}"]

    stale_note = FakeMutableNote(
        note_id=101,
        mid=7,
        field_names=("Front", "Back"),
        fields=("changed front", "old back"),
        tags=("keep", "remove-me"),
    )
    with pytest.raises(PatchValidationError, match="changed since the proposal"):
        runtime.prepare_multi_note_updates(
            FakeCollectionForNotes({101: stale_note, 202: note_two}),
            multi_snapshot(),
            patch,
            {101},
        )


def test_browser_multi_card_pane_replaces_blank_editor_area(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    browser = FakeBrowserForPane()
    fields_area = FakeVisibleWidget(False)
    splitter_pane = FakeVisibleWidget(False)
    editor_web = FakeVisibleWidget(True)
    pane = FakeBrowserPane()
    browser.form = types.SimpleNamespace(
        fieldsArea=fields_area,
        splitter=FakeSplitter(splitter_pane),
    )
    browser.editor = types.SimpleNamespace(web=editor_web)
    browser.table = FakeSelectionTable(2)
    runtime._browser_panes[browser] = pane

    runtime._sync_browser_multi_card_pane(browser)

    assert splitter_pane.visible is True
    assert fields_area.visible is True
    assert editor_web.visible is False
    assert pane.visible is True
    assert pane.context_changes == 1

    browser.table.count = 1
    runtime._sync_browser_multi_card_pane(browser)

    assert pane.visible is False
    assert editor_web.visible is True

    browser.table.count = 0
    editor_web.visible = False
    pane.visible = True
    runtime._sync_browser_multi_card_pane(browser)

    assert pane.visible is False
    assert editor_web.visible is True


def test_sanitize_html_allows_formatting_and_mathjax() -> None:
    assert (
        sanitize_html("<p>Use <strong>canonical</strong> divisors \\[K_X\\].</p>")
        == "<p>Use <strong>canonical</strong> divisors \\[K_X\\].</p>"
    )


def test_sanitize_html_strips_scripts_events_and_unsafe_links() -> None:
    assert sanitize_html(
        '<script>alert(1)</script><p onclick="evil()">Hi</p>'
        '<a href="javascript:alert(1)" title="unsafe">bad</a>'
        '<a href="https://example.test/?a=1&b=2">good</a>'
    ) == (
        '<p>Hi</p><a title="unsafe">bad</a>'
        '<a href="https://example.test/?a=1&amp;b=2" rel="noopener noreferrer">good</a>'
    )


def test_sanitize_html_escapes_unknown_tags() -> None:
    assert sanitize_html("<custom data-x='1'>x</custom>") == (
        "&lt;custom data-x=&#x27;1&#x27;&gt;x&lt;/custom&gt;"
    )


def test_surface_rendering_helpers_escape_and_sanitize() -> None:
    assert "&lt;b&gt;hi&lt;/b&gt;<br>again" in render_user_message("<b>hi</b>\nagain")
    assert "<script>" not in render_assistant_message(
        "<p>Math \\(x^2\\)</p><script>bad()</script>",
        "fallback",
    )
    assert "\\(x^2\\)" in render_assistant_message(
        "<p>Math \\(x^2\\)</p><script>bad()</script>",
        "fallback",
    )
    assert "&lt;boom&gt;" in render_error_message("<boom>")


def test_render_user_message_includes_escaped_selected_text_context() -> None:
    selected = SelectedTextSnapshot(
        field_name="Front <field>",
        field_index=0,
        input_kind="rich_text",
        text="  alpha <beta>\n gamma  ",
        html="<strong>raw selected html must not render</strong>",
    )

    rendered = render_user_message("Improve <this>", selected)

    assert "Improve &lt;this&gt;" in rendered
    assert "Selection sent from Front &lt;field&gt;" in rendered
    assert '"alpha &lt;beta&gt; gamma"' in rendered
    assert "raw selected html must not render" not in rendered
    assert "<field>" not in rendered
    assert "<beta>" not in rendered


def test_render_user_message_truncates_selected_text_context() -> None:
    selected = SelectedTextSnapshot(
        field_name="Front",
        field_index=0,
        input_kind="rich_text",
        text="word " * 40,
        html="<strong>word</strong>",
    )

    rendered = render_user_message("Improve", selected)

    assert "Selection sent from Front" in rendered
    assert "..." in rendered
    assert "word " * 35 not in rendered


def test_selection_context_label_text_is_plain_excerpt() -> None:
    selected = SelectedTextSnapshot(
        field_name="Front",
        field_index=0,
        input_kind="plain_text",
        text="  alpha\n beta  ",
        html=None,
    )

    assert selection_context_label_text(selected) == 'Front - "alpha beta"'


def test_render_activity_summary_can_expand_escaped_details() -> None:
    rendered = render_activity_summary(
        "agent-activity-1",
        "[Codex activity: done]",
        ["[tool] rg <unsafe>", "[reasoning] checked & done"],
    )

    assert "<details" in rendered
    assert "<summary>" in rendered
    assert "[Codex activity: done]" in rendered
    assert "[tool] rg &lt;unsafe&gt;" in rendered
    assert "[reasoning] checked &amp; done" in rendered
    assert "<unsafe>" not in rendered


def test_js_apply_agent_proposal_targets_editor_undo_path() -> None:
    js = js_apply_agent_proposal(
        [{"index": 0, "html": '<b data-x="1">new</b>'}],
        ["keep", "agent"],
    )

    assert js.startswith("applyAgentProposal(")
    assert '"fields": [{"index": 0, "html": "<b data-x=\\"1\\">new</b>"}]' in js
    assert '"tags": ["keep", "agent"]' in js


def test_js_clear_transcript_targets_agent_surface() -> None:
    assert js_clear_transcript() == "window.agentPane.clearTranscript();"


def test_collect_note_images_filters_and_deduplicates_local_media(
    tmp_path: Path,
) -> None:
    media_dir = tmp_path / "collection.media"
    media_dir.mkdir()
    (media_dir / "one.jpg").write_bytes(b"one")
    (media_dir / "space name.png").write_bytes(b"space")
    (media_dir / "nested").mkdir()
    (media_dir / "nested" / "inside.gif").write_bytes(b"inside")
    (media_dir / "unicode-\u4eca\u65e5.webp").write_bytes(b"unicode")
    (media_dir / "audio.mp3").write_bytes(b"audio")
    (tmp_path / "outside.jpg").write_bytes(b"outside")
    fields = (
        FieldSnapshot(name="Front", html="front-html"),
        FieldSnapshot(name="Back", html="back-html"),
    )
    media = FakeMediaManager(
        media_dir,
        {
            "front-html": [
                "one.jpg",
                "space%20name.png",
                "nested/inside.gif",
                "audio.mp3",
                "missing.jpg",
                "../outside.jpg",
                "data:image/png;base64,AAAA",
                "https://example.test/remote.jpg",
            ],
            "back-html": [
                "one.jpg",
                "unicode-%E4%BB%8A%E6%97%A5.webp",
                "nested/inside.gif",
            ],
        },
    )

    images = collect_note_images(media, 7, fields)

    assert [
        (image.attachment_index, image.filename, image.fields) for image in images
    ] == [
        (1, "one.jpg", ("Front", "Back")),
        (2, "space name.png", ("Front",)),
        (3, "nested/inside.gif", ("Front", "Back")),
        (4, "unicode-\u4eca\u65e5.webp", ("Back",)),
    ]
    assert [Path(image.path).resolve() for image in images] == [
        (media_dir / "one.jpg").resolve(),
        (media_dir / "space name.png").resolve(),
        (media_dir / "nested" / "inside.gif").resolve(),
        (media_dir / "unicode-\u4eca\u65e5.webp").resolve(),
    ]


def test_editor_snapshot_serializes_image_metadata_without_absolute_paths(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "collection.media" / "one.jpg"
    image = NoteImageSnapshot(
        attachment_index=1,
        filename="one.jpg",
        fields=("Front",),
        path=str(image_path),
    )
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(FieldSnapshot(name="Front", html='<img src="one.jpg">'),),
        tags=(),
        images=(image,),
    )

    serialized = current.as_tool_result()

    assert serialized["images"] == [
        {"attachment_index": 1, "filename": "one.jpg", "fields": ["Front"]}
    ]
    assert str(tmp_path) not in json.dumps(serialized)


def test_editor_snapshot_serializes_selected_text_context() -> None:
    serialized = snapshot().as_tool_result()

    assert serialized["selected_text"] == {
        "field_name": "Front",
        "field_index": 0,
        "input_kind": "rich_text",
        "text": "old",
        "html": "<b>old</b>",
    }


def test_validate_selected_text_snapshot_drops_stale_or_malformed_context() -> None:
    fields = snapshot().fields

    assert validate_selected_text_snapshot(
        {
            "field_name": "Back",
            "field_index": 1,
            "input_kind": "plain_text",
            "text": "<b>source</b>",
            "html": None,
        },
        fields,
    ) == SelectedTextSnapshot(
        field_name="Back",
        field_index=1,
        input_kind="plain_text",
        text="<b>source</b>",
        html=None,
    )
    assert validate_selected_text_snapshot(
        SelectedTextSnapshot(
            field_name="Front",
            field_index=0,
            input_kind="rich_text",
            text="selected",
            html="<strong>selected</strong>",
        ),
        fields,
    ) == SelectedTextSnapshot(
        field_name="Front",
        field_index=0,
        input_kind="rich_text",
        text="selected",
        html="<strong>selected</strong>",
    )
    assert (
        validate_selected_text_snapshot(
            {
                "field_name": "Front",
                "field_index": 1,
                "input_kind": "rich_text",
                "text": "stale",
                "html": "<b>stale</b>",
            },
            fields,
        )
        is None
    )
    assert (
        validate_selected_text_snapshot(
            {
                "field_name": "Front",
                "field_index": 0,
                "input_kind": "tag_editor",
                "text": "no",
                "html": None,
            },
            fields,
        )
        is None
    )
    assert (
        validate_selected_text_snapshot(
            {
                "field_name": "Front",
                "field_index": 0,
                "input_kind": "rich_text",
                "text": " ",
                "html": None,
            },
            fields,
        )
        is None
    )


def test_render_proposal_diff_includes_sanitized_field_preview_and_diff() -> None:
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(
                name="Front",
                html='<p onclick="evil()">old \\(x\\)</p><script>bad()</script>',
            ),
        ),
        tags=(),
    )
    patch = validate_note_patch(
        {
            "summary": "Improve field",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [
                {
                    "name": "Front",
                    "html": '<p>new \\[K_X\\]</p><a href="https://example.test">ok</a>',
                }
            ],
            "tags": {"replace": None, "add": [], "remove": []},
        },
        current,
    )

    rendered = render_proposal_diff(current, patch)
    preview = rendered.split('<div class="agent-diff-heading">', maxsplit=1)[0]

    assert "Improve field" in rendered
    assert '<div class="agent-preview-heading">Current</div>' in rendered
    assert '<div class="agent-preview-heading">Proposed</div>' in rendered
    assert "<p>old \\(x\\)</p>" in preview
    assert "<p>new \\[K_X\\]</p>" in preview
    assert "<script>" not in preview
    assert "onclick" not in preview
    assert 'href="https://example.test"' in preview
    assert "agent-diff-file" in rendered
    assert "agent-diff-hunk" in rendered
    assert "agent-diff-del" in rendered
    assert "agent-diff-add" in rendered
    assert '<span class="agent-diff-marker">+</span>' in rendered
    assert '<div class="agent-diff-content"><p>new \\[K_X\\]</p>' in rendered
    assert "+&lt;p&gt;new \\[K_X\\]&lt;/p&gt;" not in rendered


def test_render_proposal_diff_renders_html_diff_rows_for_mathjax() -> None:
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(
                name="Front",
                html="<div>Degree alone gives \\(4 &lt; 12\\).</div>",
            ),
        ),
        tags=(),
    )
    patch = validate_note_patch(
        {
            "summary": "Improve MathJax explanation",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [
                {
                    "name": "Front",
                    "html": (
                        '<div onclick="evil()"><b>Why the fibre check is needed.</b> '
                        "\\(4 &lt; 12\\)</div>"
                        "<div>\\[y^4-y_0^4=-5x^2+x^4.\\]</div>"
                        "<script>bad()</script>"
                    ),
                }
            ],
            "tags": {"replace": None, "add": [], "remove": []},
        },
        current,
    )

    rendered = render_proposal_diff(current, patch)
    diff = rendered.split('<div class="agent-diff-heading">', maxsplit=1)[1]

    assert (
        '<div class="agent-unified-diff agent-unified-html-diff tex2jax_ignore">'
        in diff
    )
    assert '<span class="agent-diff-marker">+</span>' in diff
    assert "<b>Why the fibre check is needed.</b>" in diff
    assert "\\[y^4-y_0^4=-5x^2+x^4.\\]" in diff
    assert "4 &lt; 12" in diff
    assert "4 &amp;lt; 12" not in diff
    assert "onclick" not in diff
    assert "<script>" not in diff


def test_render_proposal_diff_keeps_multiline_mathjax_literal_in_diff() -> None:
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(FieldSnapshot(name="Front", html="<div>old</div>"),),
        tags=(),
    )
    patch = validate_note_patch(
        {
            "summary": "Explain asymptotic bound",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [
                {
                    "name": "Front",
                    "html": (
                        "<div><b>Appendix: why</b></div>\n"
                        "<div>\\[\n"
                        "\\sqrt{x}\\log x + x^{1/3}(\\log x)^2 = o(x)\n"
                        "\\]</div>\n"
                        '<div onclick="evil()">safe text</div><script>bad()</script>'
                    ),
                }
            ],
            "tags": {"replace": None, "add": [], "remove": []},
        },
        current,
    )

    rendered = render_proposal_diff(current, patch)
    preview, diff = rendered.split('<div class="agent-diff-heading">', maxsplit=1)

    assert "tex2jax_ignore" not in preview
    assert (
        '<div class="agent-unified-diff agent-unified-html-diff tex2jax_ignore">'
        in diff
    )
    assert "\\[" in diff
    assert "\\]" in diff
    assert "\\sqrt{x}\\log x + x^{1/3}(\\log x)^2 = o(x)" in diff
    assert "onclick" not in diff
    assert "<script>" not in diff


def test_render_proposal_diff_renders_legacy_latex_preview_as_data_images() -> None:
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(
                name="Front",
                html="<p>old [$]x^2[/$]</p><script>bad()</script>",
            ),
        ),
        tags=(),
    )
    patch = validate_note_patch(
        {
            "summary": "Render latex",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [
                {
                    "name": "Front",
                    "html": '<p onclick="evil()">new [$$]y[/$$]</p>',
                }
            ],
            "tags": {"replace": None, "add": [], "remove": []},
        },
        current,
    )
    rendered_images: list[str] = []

    def extract_latex(text: str, svg: bool) -> PreviewExtractedLatexOutput:
        assert not svg
        extracted: list[PreviewExtractedLatex] = []
        rendered = text
        if "[$]x^2[/$]" in rendered:
            rendered = rendered.replace(
                "[$]x^2[/$]",
                '<img class=latex alt="$x^2$" src="latex-inline.png">',
            )
            extracted.append(PreviewExtractedLatex("latex-inline.png", "$x^2$"))
        if "[$$]y[/$$]" in rendered:
            rendered = rendered.replace(
                "[$$]y[/$$]",
                '<img class=latex alt="display y" src="latex-display.png">',
            )
            extracted.append(PreviewExtractedLatex("latex-display.png", "display y"))
        return PreviewExtractedLatexOutput(rendered, tuple(extracted))

    def render_latex_image(
        extracted: PreviewExtractedLatex,
        header: str,
        footer: str,
        svg: bool,
    ) -> bytes:
        assert header == "header"
        assert footer == "footer"
        assert not svg
        rendered_images.append(extracted.filename)
        return f"image:{extracted.filename}".encode()

    renderer = LegacyLatexPreviewRenderer(
        col=_CollectionThatMustNotReceiveMediaWrites(),
        notetype={"latexPre": "header", "latexPost": "footer", "latexsvg": False},
        extract_latex=extract_latex,
        render_latex_image=render_latex_image,
        latex_enabled=True,
    )

    rendered = render_proposal_diff(current, patch, renderer.render)
    preview = rendered.split('<div class="agent-diff-heading">', maxsplit=1)[0]

    assert rendered_images == ["latex-inline.png", "latex-display.png"]
    assert '<img class="latex" alt="$x^2$" src="data:image/png;base64,' in preview
    assert '<img class="latex" alt="display y" src="data:image/png;base64,' in preview
    assert "<script>" not in preview
    assert "onclick" not in preview
    assert '<img class="latex" alt="$x^2$" src="data:image/png;base64,' in rendered
    assert '<img class="latex" alt="display y" src="data:image/png;base64,' in rendered
    assert (
        "+&lt;p onclick=&quot;evil()&quot;&gt;new [$$]y[/$$]&lt;/p&gt;" not in rendered
    )


def test_legacy_latex_preview_deduplicates_rendered_images() -> None:
    rendered_images: list[str] = []

    def extract_latex(text: str, svg: bool) -> PreviewExtractedLatexOutput:
        return PreviewExtractedLatexOutput(
            html=text.replace(
                "[$]x[/$]",
                '<img class=latex alt="$x$" src="latex-shared.svg">',
            ),
            latex=(PreviewExtractedLatex("latex-shared.svg", "$x$"),),
        )

    def render_latex_image(
        extracted: PreviewExtractedLatex,
        _header: str,
        _footer: str,
        svg: bool,
    ) -> bytes:
        assert svg
        rendered_images.append(extracted.filename)
        return b"<svg></svg>"

    renderer = LegacyLatexPreviewRenderer(
        col=_CollectionThatMustNotReceiveMediaWrites(),
        notetype={"latexPre": "", "latexPost": "", "latexsvg": True},
        extract_latex=extract_latex,
        render_latex_image=render_latex_image,
        latex_enabled=True,
    )

    first = renderer.render("one [$]x[/$]")
    second = renderer.render("two [$]x[/$]")

    assert rendered_images == ["latex-shared.svg"]
    assert 'src="data:image/svg+xml;base64,' in first
    assert 'src="data:image/svg+xml;base64,' in second


def test_legacy_latex_preview_falls_back_when_generation_fails() -> None:
    def extract_latex(text: str, svg: bool) -> PreviewExtractedLatexOutput:
        return PreviewExtractedLatexOutput(
            html=text.replace(
                "[$]x[/$]",
                '<img class=latex alt="$x$" src="latex-failed.png">',
            ),
            latex=(PreviewExtractedLatex("latex-failed.png", "$x$"),),
        )

    def render_latex_image(
        _extracted: PreviewExtractedLatex,
        _header: str,
        _footer: str,
        _svg: bool,
    ) -> bytes:
        raise LatexPreviewError("<b>latex failed</b><script>bad()</script>")

    renderer = LegacyLatexPreviewRenderer(
        col=_CollectionThatMustNotReceiveMediaWrites(),
        notetype={"latexPre": "", "latexPost": "", "latexsvg": False},
        extract_latex=extract_latex,
        render_latex_image=render_latex_image,
        latex_enabled=True,
    )

    rendered = renderer.render('<p onclick="evil()">[$]x[/$]</p>')

    assert "[$]x[/$]" in rendered
    assert "<img" not in rendered
    assert "onclick" not in rendered
    assert "<b>latex failed</b>" in rendered
    assert "<script>" not in rendered


def test_render_proposal_diff_includes_tag_preview_and_diff() -> None:
    patch = validate_note_patch(
        {
            "summary": "Retag",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [],
            "tags": {"replace": None, "add": ["agent"], "remove": ["remove-me"]},
        },
        snapshot(),
    )

    rendered = render_proposal_diff(snapshot(), patch)

    assert "Tags" in rendered
    assert '<span class="agent-tag">keep</span>' in rendered
    assert '<span class="agent-tag">agent</span>' in rendered
    assert (
        '<div class="agent-diff-row agent-diff-del">-keep remove-me</div>' in rendered
    )
    assert '<div class="agent-diff-row agent-diff-add">+keep agent</div>' in rendered


def test_validate_note_patch_accepts_current_note_fields_and_tags() -> None:
    patch = validate_note_patch(
        {
            "summary": "Tighten wording",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [{"name": "Front", "html": "new front"}],
            "tags": {"replace": None, "add": ["agent"], "remove": ["remove-me"]},
        },
        snapshot(),
    )

    assert patch.field_updates == {"Front": "new front"}
    assert patch.tag_patch.apply(snapshot().tags) == ("keep", "agent")


def test_validate_note_patch_strips_clipboard_fragment_comments() -> None:
    patch = validate_note_patch(
        {
            "summary": "Tighten wording",
            "note_id": 123,
            "notetype_id": 7,
            "field_updates": [
                {
                    "name": "Front",
                    "html": (
                        "before<!--StartFragment--><b>new front</b>"
                        "<!--EndFragment-->after"
                    ),
                }
            ],
            "tags": {"replace": None, "add": [], "remove": []},
        },
        snapshot(),
    )

    assert patch.field_updates == {"Front": "before<b>new front</b>after"}


def test_validate_note_patch_rejects_malformed_clipboard_fragment_html() -> None:
    malformed_html = (
        "definition (hilbert spaces)<div><br></div><div>let</div>"
        "<div><!--StartFragment--><ul><li><anki-mathjax>H</anki-mathjax> "
        "a Hilbert space</li></ul><!--EndFragment></div>"
        "<div><anki-mathjax>T</anki-mathjax>&nbsp;is a&nbsp;"
        '<!--StartFragment--><span class="bigger">Hilbert-Schmidt operator</span>'
        "<!--EndFragment>&nbsp;(HS)&nbsp;if</div>"
        "</body></html>--></div>"
    )

    with pytest.raises(PatchValidationError, match="malformed clipboard fragment"):
        validate_note_patch(
            {
                "summary": "Convert LaTeX",
                "note_id": 123,
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": malformed_html}],
                "tags": {"replace": None, "add": [], "remove": []},
            },
            snapshot(),
        )


def test_validate_note_patch_rejects_document_wrapper_html() -> None:
    with pytest.raises(PatchValidationError, match="document wrapper"):
        validate_note_patch(
            {
                "summary": "Bad wrapper",
                "note_id": 123,
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "<body>new</body>"}],
                "tags": {"replace": None, "add": [], "remove": []},
            },
            snapshot(),
        )


def test_validate_note_patch_rejects_broken_html_comments() -> None:
    for bad_html, message in (
        ("new --> front", "stray HTML comment closer"),
        ("new <!-- front", "unterminated HTML comment"),
    ):
        with pytest.raises(PatchValidationError, match=message):
            validate_note_patch(
                {
                    "summary": "Bad comment",
                    "note_id": 123,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Front", "html": bad_html}],
                    "tags": {"replace": None, "add": [], "remove": []},
                },
                snapshot(),
            )


def test_validate_multi_note_patch_accepts_selected_note_updates() -> None:
    patch = validate_multi_note_patch(
        {
            "summary": "Tighten selected cards",
            "note_updates": [
                {
                    "note_id": 101,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Front", "html": "new front"}],
                    "tags": {"replace": None, "add": ["agent"], "remove": []},
                },
                {
                    "note_id": 202,
                    "notetype_id": 8,
                    "field_updates": [{"name": "Text", "html": "new {{c1::text}}"}],
                    "tags": {"replace": None, "add": [], "remove": ["cloze"]},
                },
            ],
        },
        multi_snapshot(),
    )

    assert isinstance(patch, MultiNotePatch)
    assert patch.summary == "Tighten selected cards"
    assert patch.note_updates[0].note_id == 101
    assert patch.note_updates[0].field_updates == {"Front": "new front"}
    assert patch.note_updates[0].tag_patch.apply(("keep",)) == ("keep", "agent")
    assert patch.note_updates[1].tag_patch.apply(("cloze",)) == ()


def test_validate_multi_note_patch_rejects_malformed_field_html() -> None:
    with pytest.raises(PatchValidationError, match="malformed clipboard fragment"):
        validate_multi_note_patch(
            {
                "summary": "Tighten selected cards",
                "note_updates": [
                    {
                        "note_id": 101,
                        "notetype_id": 7,
                        "field_updates": [
                            {"name": "Front", "html": "<!--EndFragment>new"}
                        ],
                        "tags": {"replace": None, "add": [], "remove": []},
                    }
                ],
            },
            multi_snapshot(),
        )


def test_validate_multi_note_patch_rejects_unselected_note_or_unknown_field() -> None:
    with pytest.raises(PatchValidationError, match="not selected"):
        validate_multi_note_patch(
            {
                "summary": "Bad note",
                "note_updates": [
                    {
                        "note_id": 999,
                        "notetype_id": 7,
                        "field_updates": [{"name": "Front", "html": "new"}],
                        "tags": {"replace": None, "add": [], "remove": []},
                    }
                ],
            },
            multi_snapshot(),
        )

    with pytest.raises(PatchValidationError, match="Unknown field"):
        validate_multi_note_patch(
            {
                "summary": "Bad field",
                "note_updates": [
                    {
                        "note_id": 101,
                        "notetype_id": 7,
                        "field_updates": [{"name": "Extra", "html": "new"}],
                        "tags": {"replace": None, "add": [], "remove": []},
                    }
                ],
            },
            multi_snapshot(),
        )


def test_render_multi_note_card_proposal_diff_shows_one_cards_note_group() -> None:
    patch = validate_multi_note_patch(
        {
            "summary": "Tighten selected cards",
            "note_updates": [
                {
                    "note_id": 101,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Front", "html": "<b>new</b>"}],
                    "tags": {"replace": None, "add": ["agent"], "remove": []},
                },
                {
                    "note_id": 202,
                    "notetype_id": 8,
                    "field_updates": [{"name": "Text", "html": "new text"}],
                    "tags": {"replace": None, "add": [], "remove": []},
                },
            ],
        },
        multi_snapshot(),
    )

    rendered = render_multi_note_card_proposal_diff(multi_snapshot(), patch, card_id=12)

    assert "Tighten selected cards" in rendered
    assert "Card 2" in rendered
    assert "note 101" in rendered
    assert "Field: Front" in rendered
    assert '<div class="agent-diff-content"><b>new</b></div>' in rendered
    assert (
        '<div class="agent-unified-diff agent-unified-html-diff tex2jax_ignore">'
        in rendered
    )
    assert "new text" not in rendered
    assert multi_note_patch_card_ids(multi_snapshot(), patch) == (11, 12, 21)


def test_set_multi_proposal_previews_affected_card_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _import_runtime_with_aqt_stubs(monkeypatch)
    pane = runtime.EditorAgentPane.__new__(runtime.EditorAgentPane)
    pane.surface = FakeSurface()
    pane.apply_button = FakeButton(True)
    pane._show_multi_proposal_dialog = lambda _snapshot, _patch: None
    patch = validate_multi_note_patch(
        {
            "summary": "Tighten selected cards",
            "note_updates": [
                {
                    "note_id": 101,
                    "notetype_id": 7,
                    "field_updates": [{"name": "Front", "html": "new front"}],
                    "tags": {"replace": None, "add": [], "remove": []},
                }
            ],
        },
        multi_snapshot(),
    )

    pane._set_multi_proposal(multi_snapshot(), patch)

    assert "Card 11" in pane.surface.evals[0]
    assert "note 101" in pane.surface.evals[0]


def test_validate_note_patch_rejects_unknown_field() -> None:
    with pytest.raises(PatchValidationError, match="Unknown field"):
        validate_note_patch(
            {
                "summary": "Bad field",
                "notetype_id": 7,
                "field_updates": [{"name": "Extra", "html": "no"}],
            },
            snapshot(),
        )


def test_validate_note_patch_rejects_stale_note_and_notetype() -> None:
    with pytest.raises(PatchValidationError, match="different note"):
        validate_note_patch(
            {
                "summary": "Wrong note",
                "note_id": 456,
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "new"}],
            },
            snapshot(),
        )

    with pytest.raises(PatchValidationError, match="different note type"):
        validate_note_patch(
            {
                "summary": "Wrong type",
                "note_id": 123,
                "notetype_id": 8,
                "field_updates": [{"name": "Front", "html": "new"}],
            },
            snapshot(),
        )


def test_validate_note_patch_rejects_whitespace_tags() -> None:
    with pytest.raises(PatchValidationError, match="whitespace"):
        validate_note_patch(
            {
                "summary": "Bad tag",
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "new"}],
                "tags": {"add": ["two words"]},
            },
            snapshot(),
        )


def test_validate_note_patch_can_replace_all_tags() -> None:
    patch = validate_note_patch(
        {
            "summary": "Retag",
            "notetype_id": 7,
            "field_updates": [{"name": "Front", "html": "new front"}],
            "tags": {"replace": ["fresh"], "add": [], "remove": []},
        },
        snapshot(),
    )

    assert patch.tag_patch.apply(snapshot().tags) == ("fresh",)


def test_ollama_agent_runs_model_and_parses_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        captured["command"] = command
        captured["stdin"] = stdin
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["text"] = text
        captured["bufsize"] = bufsize
        captured["env"] = env
        process = FakePopen(
            stdout=(
                "{\n"
                '  "message": "Looks better with a shorter front.",\n'
                '  "message_html": "<p>Looks <strong>better</strong>.</p>",\n'
                '  "patch": {\n'
                '    "summary": "Shorten front",\n'
                '    "note_id": 123,\n'
                '    "notetype_id": 7,\n'
                '    "field_updates": [{"name": "Front", "html": "new front"}],\n'
                '    "tags": {"replace": null, "add": ["agent"], "remove": []}\n'
                "  }\n"
                "}\n"
            )
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = OllamaCliAgent(
        ollama_path="/usr/local/bin/ollama",
        ollama_host="localhost:11434",
        model="qwen3:latest",
        timeout_seconds=123,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root="/ignored",
        history=[],
    )

    command = captured["command"]
    assert command == [
        "/usr/local/bin/ollama",
        "run",
        "qwen3:latest",
        "--format",
        "json",
        "--hidethinking",
        "--nowordwrap",
    ]
    assert captured["env"]["OLLAMA_HOST"] == "http://localhost:11434"
    assert captured["stdin"] == subprocess.PIPE
    assert captured["stdout"] == subprocess.PIPE
    assert captured["stderr"] == subprocess.PIPE
    assert "Current editor context is JSON" in captured["process"].stdin.text
    assert '"selected_text": {"field_name": "Front"' in captured["process"].stdin.text
    assert "You do not have Codex tools" in captured["process"].stdin.text
    assert "Use only the current editor context JSON" in captured["process"].stdin.text
    assert "Improve this" in captured["process"].stdin.text
    assert "may inspect and edit files" not in captured["process"].stdin.text
    assert "Do not include hidden chain-of-thought" in captured["process"].stdin.text
    assert result.text == "Looks better with a shorter front."
    assert result.html == "<p>Looks <strong>better</strong>.</p>"
    assert result.proposals[0].field_updates == {"Front": "new front"}
    assert result.event_count == 0


def test_ollama_agent_treats_empty_patch_as_no_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        return FakePopen(
            stdout=json.dumps(
                {
                    "message": "Use dollar delimiters for inline MathJax.",
                    "message_html": "<p>Use dollar delimiters for inline MathJax.</p>",
                    "patch": {
                        "summary": "No changes",
                        "note_id": 123,
                        "notetype_id": 7,
                        "field_updates": [],
                        "tags": {"replace": None, "add": [], "remove": []},
                    },
                }
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    result = OllamaCliAgent(
        ollama_path="ollama",
        ollama_host="",
        model="qwen3:latest",
        timeout_seconds=300,
    ).send(
        prompt="use $ delimiters for the mathjax",
        snapshot=snapshot(),
        project_root="",
        history=[],
        run_logger=run_logger,
    )

    assert result.text == "Use dollar delimiters for inline MathJax."
    assert result.proposals == ()
    assert "run_failure" not in [record["event"] for record in run_logger.records]


def test_ollama_agent_accepts_field_updates_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        return FakePopen(
            stdout=json.dumps(
                {
                    "message": "Converted LaTeX to MathJax delimiters.",
                    "message_html": "<p>Converted LaTeX to MathJax delimiters.</p>",
                    "patch": {
                        "summary": "Convert delimiters",
                        "field_updates": {"Front": "\\(x^2\\)"},
                    },
                }
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    result = OllamaCliAgent(
        ollama_path="ollama",
        ollama_host="",
        model="qwen3:latest",
        timeout_seconds=300,
    ).send(
        prompt="convert latex to mathjax",
        snapshot=snapshot(),
        project_root="",
        history=[],
        run_logger=run_logger,
    )

    assert result.proposals[0].field_updates == {"Front": "\\(x^2\\)"}
    assert result.proposals[0].note_id == 123
    assert result.proposals[0].notetype_id == 7
    assert "run_failure" not in [record["event"] for record in run_logger.records]


def test_ollama_agent_accepts_patch_as_field_update_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        return FakePopen(
            stdout=json.dumps(
                {
                    "message": "Converted LaTeX to MathJax delimiters.",
                    "message_html": "<p>Converted LaTeX to MathJax delimiters.</p>",
                    "patch": [
                        {"name": "Front", "html": "\\(x^2\\)"},
                    ],
                }
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = OllamaCliAgent(
        ollama_path="ollama",
        ollama_host="",
        model="qwen3:latest",
        timeout_seconds=300,
    ).send(
        prompt="convert latex to mathjax",
        snapshot=snapshot(),
        project_root="",
        history=[],
    )

    assert result.proposals[0].field_updates == {"Front": "\\(x^2\\)"}


def test_ollama_agent_repairs_empty_patch_with_field_updates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = [
        {
            "message": "Converted LaTeX to MathJax delimiters.",
            "message_html": "<p>Converted LaTeX to MathJax delimiters.</p>",
            "patch": {
                "summary": "Convert delimiters",
                "note_id": 123,
                "notetype_id": 7,
                "field_updates": [],
                "tags": {"replace": None, "add": [], "remove": []},
            },
        },
        {
            "message": "Converted LaTeX to MathJax delimiters.",
            "message_html": "<p>Converted LaTeX to MathJax delimiters.</p>",
            "patch": {
                "summary": "Convert delimiters",
                "note_id": 123,
                "notetype_id": 7,
                "field_updates": {"Front": "\\(x^2\\)"},
            },
        },
    ]
    processes: list[FakePopen] = []

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        process = FakePopen(stdout=json.dumps(responses.pop(0)))
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    result = OllamaCliAgent(
        ollama_path="ollama",
        ollama_host="",
        model="qwen3:latest",
        timeout_seconds=300,
    ).send(
        prompt="convert latex to mathjax",
        snapshot=snapshot(),
        project_root="",
        history=[],
        run_logger=run_logger,
    )

    assert len(processes) == 2
    assert "previous JSON response" in processes[1].stdin.text
    assert "non-null patch object" in processes[1].stdin.text
    assert result.proposals[0].field_updates == {"Front": "\\(x^2\\)"}
    assert run_logger.first("repair_start")["reason"] == "empty_patch"
    assert run_logger.first("run_finish")["repair_attempted"] is True


def test_ollama_agent_times_out_and_kills_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    run_logger = FakeRunLogger()

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        process = FakePopen(returncode=None)
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="timed out"):
        OllamaCliAgent(
            ollama_path="ollama",
            ollama_host="",
            model="qwen3:latest",
            timeout_seconds=0,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    assert captured["process"].killed
    assert run_logger.first("timeout_kill")["event"] == "timeout_kill"
    assert run_logger.first("run_failure")["stage"] == "run"


def test_ollama_agent_stops_running_process_without_failure_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    run_logger = FakeRunLogger()

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        process = FakePopen(returncode=None)
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(AgentStopped, match="stopped"):
        OllamaCliAgent(
            ollama_path="ollama",
            ollama_host="",
            model="qwen3:latest",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
            stop_requested=lambda: True,
        )

    assert captured["process"].terminated
    assert run_logger.first("run_stopped")["event"] == "run_stopped"
    assert "run_failure" not in [record["event"] for record in run_logger.records]


def test_ollama_agent_reports_missing_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(*_args: Any, **_kwargs: Any) -> FakePopen:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="Could not find Ollama CLI"):
        OllamaCliAgent(
            ollama_path="/missing/ollama",
            ollama_host="",
            model="qwen3:latest",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
        )


def test_ollama_agent_logs_nonzero_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        return FakePopen(stderr="Error: connection refused", returncode=1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    with pytest.raises(RuntimeError, match="connection refused"):
        OllamaCliAgent(
            ollama_path="ollama",
            ollama_host="",
            model="qwen3:latest",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    failure = run_logger.first("run_failure")
    assert failure["stage"] == "cli_exit"
    assert failure["returncode"] == 1
    assert failure["stderr_preview"] == "Error: connection refused"


def test_ollama_agent_logs_invalid_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        return FakePopen(stdout="not json\n")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    with pytest.raises(RuntimeError, match="non-JSON"):
        OllamaCliAgent(
            ollama_path="ollama",
            ollama_host="",
            model="qwen3:latest",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    failure = run_logger.first("run_failure")
    assert failure["stage"] == "final_json_parse"


def test_ollama_agent_validates_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        env: dict[str, str],
    ) -> FakePopen:
        return FakePopen(
            stdout=json.dumps(
                {
                    "message": "Bad patch",
                    "message_html": "<p>Bad patch</p>",
                    "patch": {
                        "summary": "Bad",
                        "note_id": 999,
                        "notetype_id": 7,
                        "field_updates": [],
                        "tags": {"replace": None, "add": [], "remove": []},
                    },
                }
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    with pytest.raises(PatchValidationError):
        OllamaCliAgent(
            ollama_path="ollama",
            ollama_host="",
            model="qwen3:latest",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    assert run_logger.first("run_failure")["stage"] == "patch_validation"


def test_codex_agent_uses_writable_cli_by_default_and_parses_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        captured["stdin"] = stdin
        captured["stdout"] = stdout
        captured["stderr"] = stderr
        captured["text"] = text
        captured["bufsize"] = bufsize
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            """
            {
              "message": "Looks better with a shorter front.",
              "message_html": "<p>Looks <strong>better</strong> with a shorter front.</p>",
              "patch": {
                "summary": "Shorten front",
                "note_id": 123,
                "notetype_id": 7,
                "field_updates": [{"name": "Front", "html": "new front"}],
                "tags": {"replace": null, "add": ["agent"], "remove": []}
              }
            }
            """,
            encoding="utf-8",
        )
        process = FakePopen(
            stdout=(
                '{"type":"turn.started"}\n'
                '{"type":"exec_command_begin","cmd":"rg canonical"}\n'
            )
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    events: list[dict[str, Any]] = []

    result = CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
        event_callback=events.append,
    )

    command = captured["command"]
    assert command[:2] == ["/usr/local/bin/codex", "exec"]
    assert (
        command[command.index("--sandbox") + 1] == PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE
    )
    assert "--json" in command
    assert "--ask-for-approval" not in command
    assert 'service_tier="fast"' not in command
    assert "features.fast_mode=true" not in command
    assert 'model_reasoning_effort="high"' not in command
    assert command[command.index("--cd") + 1] == str(project.resolve())
    assert "-c" in command
    assert 'model_reasoning_summary="concise"' in command
    assert "--model" not in command
    assert command[-1] == "-"
    assert captured["stdin"] == subprocess.PIPE
    assert captured["stdout"] == subprocess.PIPE
    assert captured["stderr"] == subprocess.PIPE
    assert "Current editor context is JSON" in captured["process"].stdin.text
    assert '"selected_text": {"field_name": "Front"' in captured["process"].stdin.text
    assert '"text": "old", "html": "<b>old</b>"' in captured["process"].stdin.text
    assert "most recent\nnon-empty text selection" in captured["process"].stdin.text
    assert "Improve this" in captured["process"].stdin.text
    assert "may inspect and edit files" in captured["process"].stdin.text
    assert (
        "Keep file changes inside that project folder" in captured["process"].stdin.text
    )
    assert "Do not modify files" not in captured["process"].stdin.text
    assert "Do not include hidden chain-of-thought" in captured["process"].stdin.text
    assert "briefly in message/message_html why" in captured["process"].stdin.text
    assert result.text == "Looks better with a shorter front."
    assert result.html == "<p>Looks <strong>better</strong> with a shorter front.</p>"
    assert result.proposals[0].field_updates == {"Front": "new front"}
    assert result.event_count == 2
    assert [event["type"] for event in events] == [
        "turn.started",
        "exec_command_begin",
    ]


def test_codex_agent_sends_multi_card_context_and_validates_note_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "message": "Updated selected cards.",
                    "message_html": "<p>Updated selected cards.</p>",
                    "patch": {
                        "summary": "Tighten selected cards",
                        "note_updates": [
                            {
                                "note_id": 101,
                                "notetype_id": 7,
                                "field_updates": [
                                    {"name": "Front", "html": "new front"},
                                ],
                                "tags": {
                                    "replace": None,
                                    "add": ["agent"],
                                    "remove": [],
                                },
                            }
                        ],
                    },
                }
            ),
            encoding="utf-8",
        )
        process = FakePopen(stdout='{"type":"turn.started"}\n')
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
    ).send(
        prompt="Improve selected cards",
        snapshot=multi_snapshot(),
        project_root="",
        history=[],
    )

    stdin = captured["process"].stdin.text
    assert '"mode": "browse_multi"' in stdin
    assert '"card_id": 11' in stdin
    assert '"note_id": 101' in stdin
    assert "return note_updates" in stdin
    assert "attached image number" not in stdin
    assert isinstance(result.proposals[0], MultiNotePatch)
    assert result.proposals[0].note_updates[0].field_updates == {"Front": "new front"}


def test_codex_agent_logs_redacted_success_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    image_path = tmp_path / "one.jpg"
    image_path.write_bytes(b"image")
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(name="Front", html="<p>secret field html</p>"),
            FieldSnapshot(name="Back", html="ordinary back"),
        ),
        tags=("keep", "agent"),
        images=(
            NoteImageSnapshot(
                attachment_index=1,
                filename="one.jpg",
                fields=("Front",),
                path=str(image_path),
            ),
        ),
        selected_text=SelectedTextSnapshot(
            field_name="Front",
            field_index=0,
            input_kind="rich_text",
            text="secret selected text",
            html="<strong>secret selected html</strong>",
        ),
    )
    long_output = "line one\n" + ("x" * 700)

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        write_codex_response(
            command,
            {
                "message": "No changes.",
                "message_html": "<p>No changes.</p>",
                "patch": None,
            },
        )
        return FakePopen(
            stdout=(
                '{"type":"turn.started"}\n'
                '{"type":"exec_command_begin","cmd":["rg","canonical"]}\n'
                + json.dumps({"type": "exec_command_output", "output": long_output})
                + "\n"
                + json.dumps(
                    {
                        "type": "web_search_begin",
                        "action": {
                            "type": "search",
                            "query": "anki latest release",
                        },
                        "content": "private web search content",
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "reasoning",
                            "summary": [{"text": "Checked the source."}],
                            "content": "raw private reasoning",
                        },
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "assistant_message",
                        "content": "private assistant message",
                    }
                )
                + "\n"
                + json.dumps({"type": "error", "message": "stream error happened"})
                + "\n"
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    result = CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="gpt-5.4",
        timeout_seconds=123,
    ).send(
        prompt="secret prompt text",
        snapshot=current,
        project_root=str(project),
        history=[("secret previous user", "secret previous assistant")],
        run_logger=run_logger,
    )

    assert result.text == "No changes."
    start = run_logger.first("run_start")
    assert start["model"] == "gpt-5.4"
    assert start["sandbox"] == PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE
    assert start["timeout_seconds"] == 123
    assert start["note_id"] == 123
    assert start["notetype_id"] == 7
    assert start["field_count"] == 2
    assert start["tag_count"] == 2
    assert start["image_count"] == 1
    assert start["prompt_chars"] == len("secret prompt text")
    assert start["history_count"] == 1
    assert start["history_user_chars"] == len("secret previous user")
    assert start["history_assistant_chars"] == len("secret previous assistant")

    launch = run_logger.first("cli_launch")
    assert launch["cwd"] == str(project.resolve())
    assert launch["command"]["program"] == "codex"
    assert launch["command"]["subcommand"] == "exec"
    assert launch["command"]["sandbox"] == PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE
    assert launch["command"]["image_count"] == 1
    assert launch["command"]["image_basenames"] == ["one.jpg"]
    assert launch["command"]["output_last_message_basename"] == "last-message.json"

    stream_records = run_logger.all("stream_event")
    tool = next(record for record in stream_records if "tool_preview" in record)
    output = next(record for record in stream_records if "output_preview" in record)
    web = next(record for record in stream_records if "web_preview" in record)
    reasoning = next(
        record for record in stream_records if record["type"] == "reasoning"
    )
    message = next(record for record in stream_records if "message_chars" in record)
    error = next(record for record in stream_records if "error_preview" in record)
    assert tool["tool_preview"] == "rg canonical"
    assert "\n" not in output["output_preview"]
    assert len(output["output_preview"]) == MAX_PREVIEW_CHARS
    assert output["output_preview"].endswith("...")
    assert web["web_preview"] == "search: anki latest release"
    assert reasoning["has_reasoning_summary"] is True
    assert reasoning["reasoning_summary_preview"] == "Checked the source."
    assert message["message_chars"] == len("private assistant message")
    assert error["error_preview"] == "stream error happened"

    finish = run_logger.first("run_finish")
    assert finish["returncode"] == 0
    assert finish["event_count"] == 7
    assert finish["stdout_lines"] == 7
    assert finish["stderr_lines"] == 0
    assert finish["final_response_present"] is True
    assert finish["message_chars"] == len("No changes.")
    assert finish["message_html_chars"] == len("<p>No changes.</p>")
    assert finish["proposal_count"] == 0

    serialized = json.dumps(run_logger.records)
    assert "secret prompt text" not in serialized
    assert "secret field html" not in serialized
    assert "secret selected text" not in serialized
    assert "secret selected html" not in serialized
    assert "secret previous user" not in serialized
    assert "secret previous assistant" not in serialized
    assert "private web search content" not in serialized
    assert "raw private reasoning" not in serialized
    assert "private assistant message" not in serialized


def test_codex_agent_logs_timeout_before_killing_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    records_at_kill: list[list[str]] = []
    run_logger = FakeRunLogger()

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        process = FakePopen(
            returncode=None,
            on_kill=lambda: records_at_kill.append(
                [record["event"] for record in run_logger.records]
            ),
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="timed out"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=0,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    assert captured["process"].killed
    assert records_at_kill == [["run_start", "cli_launch", "timeout_kill"]]
    failure = run_logger.first("run_failure")
    assert failure["stage"] == "run"
    assert failure["error_type"] == "RuntimeError"
    assert "timed out" in failure["error_preview"]


def test_codex_agent_stops_running_process_without_failure_log(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    records_at_terminate: list[list[str]] = []
    run_logger = FakeRunLogger()

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        process = FakePopen(
            stdout='{"type":"turn.started"}\n',
            returncode=None,
            on_terminate=lambda: records_at_terminate.append(
                [record["event"] for record in run_logger.records]
            ),
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    def stop_requested() -> bool:
        return any(record["event"] == "stream_event" for record in run_logger.records)

    with pytest.raises(AgentStopped, match="stopped"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
            stop_requested=stop_requested,
        )

    assert captured["process"].terminated
    assert not captured["process"].killed
    assert records_at_terminate == [
        ["run_start", "cli_launch", "stream_event", "stop_terminate"]
    ]
    assert run_logger.first("run_stopped")["event"] == "run_stopped"
    assert "run_failure" not in [record["event"] for record in run_logger.records]


def test_codex_agent_logs_nonzero_cli_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        return FakePopen(
            stdout='{"type":"turn.started"}\n',
            stderr="first stderr line\nsecond stderr line",
            returncode=2,
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    with pytest.raises(RuntimeError, match="exit code 2"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    failure = run_logger.first("run_failure")
    assert failure["stage"] == "cli_exit"
    assert failure["returncode"] == 2
    assert failure["event_count"] == 1
    assert failure["stdout_lines"] == 1
    assert failure["stderr_lines"] == 2
    assert failure["stderr_preview"] == "first stderr line second stderr line"


def test_codex_agent_logs_missing_final_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    with pytest.raises(RuntimeError, match="did not write"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    failure = run_logger.first("run_failure")
    assert failure["stage"] == "missing_final_response"
    assert failure["returncode"] == 0
    assert failure["event_count"] == 1
    assert failure["stdout_lines"] == 1
    assert failure["stderr_lines"] == 0


def test_codex_agent_logs_final_json_parse_failure_without_raw_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_response = "not json with secret final response"

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        write_codex_response(command, raw_response)
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    with pytest.raises(RuntimeError, match="non-JSON"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    failure = run_logger.first("run_failure")
    assert failure["stage"] == "final_json_parse"
    assert failure["output_chars"] == len(raw_response)
    assert failure["error_type"] == "RuntimeError"
    assert raw_response not in json.dumps(run_logger.records)


def test_codex_agent_logs_patch_validation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        write_codex_response(
            command,
            {
                "message": "Proposed a patch.",
                "message_html": "<p>Proposed a patch.</p>",
                "patch": {
                    "summary": "Wrong note type",
                    "note_id": 123,
                    "notetype_id": 999,
                    "field_updates": [],
                    "tags": {"replace": None, "add": [], "remove": []},
                },
            },
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    with pytest.raises(PatchValidationError, match="different note type"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            run_logger=run_logger,
        )

    failure = run_logger.first("run_failure")
    assert failure["stage"] == "patch_validation"
    assert failure["returncode"] == 0
    assert failure["event_count"] == 1
    assert failure["error_type"] == "PatchValidationError"


def test_codex_agent_treats_empty_patch_as_no_proposal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        write_codex_response(
            command,
            {
                "message": "No note changes needed.",
                "message_html": "<p>No note changes needed.</p>",
                "patch": {
                    "summary": "No changes",
                    "note_id": 123,
                    "notetype_id": 7,
                    "field_updates": [],
                    "tags": {"replace": None, "add": [], "remove": []},
                },
            },
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    run_logger = FakeRunLogger()

    result = CodexCliAgent(
        codex_path="codex",
        model="",
        timeout_seconds=300,
    ).send(
        prompt="Explain",
        snapshot=snapshot(),
        project_root="",
        history=[],
        run_logger=run_logger,
    )

    assert result.text == "No note changes needed."
    assert result.proposals == ()
    assert "run_failure" not in [record["event"] for record in run_logger.records]


def test_codex_agent_can_disable_reasoning_summaries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
        stream_reasoning_summaries=False,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(tmp_path),
        history=[],
    )

    command = captured["command"]
    assert command[command.index("-c") + 1] == 'model_reasoning_summary="none"'


def test_codex_agent_can_enable_fast_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        write_codex_response(
            command,
            {
                "message": "No changes.",
                "message_html": "<p>No changes.</p>",
                "patch": None,
            },
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="gpt-5.5",
        timeout_seconds=123,
        fast_mode=True,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(tmp_path),
        history=[],
    )

    command = captured["command"]
    assert "features.fast_mode=true" in command
    assert 'service_tier="fast"' in command


def test_codex_agent_can_set_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        write_codex_response(
            command,
            {
                "message": "No changes.",
                "message_html": "<p>No changes.</p>",
                "patch": None,
            },
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="gpt-5.5",
        timeout_seconds=123,
        reasoning_effort="high",
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(tmp_path),
        history=[],
    )

    command = captured["command"]
    assert 'model_reasoning_effort="high"' in command


def test_codex_agent_omits_unsupported_minimal_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        write_codex_response(
            command,
            {
                "message": "No changes.",
                "message_html": "<p>No changes.</p>",
                "patch": None,
            },
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="gpt-5.5",
        timeout_seconds=123,
        reasoning_effort="minimal",
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(tmp_path),
        history=[],
    )

    command = captured["command"]
    assert 'model_reasoning_effort="minimal"' not in command


def test_codex_agent_disables_reasoning_summaries_for_spark_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        write_codex_response(
            command,
            {
                "message": "No changes.",
                "message_html": "<p>No changes.</p>",
                "patch": None,
            },
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="gpt-5.3-codex-spark",
        timeout_seconds=123,
        stream_reasoning_summaries=True,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(tmp_path),
        history=[],
    )

    command = captured["command"]
    assert command[command.index("-c") + 1] == 'model_reasoning_summary="none"'


def test_codex_agent_includes_custom_instructions_and_keeps_fixed_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        process = FakePopen(stdout='{"type":"turn.completed"}\n')
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
        custom_instructions="Prefer concise wording for cloze cards.",
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
    )

    stdin_text = captured["process"].stdin.text
    assert "User-customized instructions:" in stdin_text
    assert "Prefer concise wording for cloze cards." in stdin_text
    assert stdin_text.index("Prefer concise wording") < stdin_text.index(
        "Return a JSON object matching the supplied schema"
    )
    assert "Do not include hidden chain-of-thought" in stdin_text
    assert "When proposing a patch:" in stdin_text


def test_codex_agent_attaches_note_images_and_explains_image_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    image_one = tmp_path / "one.jpg"
    image_two = tmp_path / "two.png"
    image_one.write_bytes(b"one")
    image_two.write_bytes(b"two")
    current = EditorSnapshot(
        mode="browse",
        note_id=123,
        notetype_id=7,
        notetype_name="Basic",
        fields=(
            FieldSnapshot(name="Front", html='<img src="one.jpg">'),
            FieldSnapshot(name="Back", html='<img src="two.png">'),
        ),
        tags=(),
        images=(
            NoteImageSnapshot(
                attachment_index=1,
                filename="one.jpg",
                fields=("Front",),
                path=str(image_one),
            ),
            NoteImageSnapshot(
                attachment_index=2,
                filename="two.png",
                fields=("Back",),
                path=str(image_two),
            ),
        ),
    )
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        process = FakePopen(stdout='{"type":"turn.completed"}\n')
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
    ).send(
        prompt="Explain the pictures",
        snapshot=current,
        project_root=str(project),
        history=[],
    )

    command = captured["command"]
    assert command.count("--image") == 2
    first_image_flag = command.index("--image")
    second_image_flag = command.index("--image", first_image_flag + 1)
    assert command[first_image_flag + 1] == str(image_one)
    assert command[second_image_flag + 1] == str(image_two)
    stdin_text = captured["process"].stdin.text
    assert "context_json.images[n]" in stdin_text
    assert "attached image number n + 1" in stdin_text
    assert '"filename": "one.jpg"' in stdin_text
    assert str(tmp_path) not in stdin_text


def test_codex_agent_can_use_read_only_project_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        process = FakePopen(stdout='{"type":"turn.completed"}\n')
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = CodexCliAgent(
        codex_path="/usr/local/bin/codex",
        model="",
        timeout_seconds=123,
        project_folder_access=PROJECT_FOLDER_ACCESS_READ_ONLY,
    ).send(
        prompt="Explain this",
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
    )

    command = captured["command"]
    assert command[command.index("--sandbox") + 1] == PROJECT_FOLDER_ACCESS_READ_ONLY
    assert "read-only shell commands" in captured["process"].stdin.text
    assert "Do not modify files" in captured["process"].stdin.text
    assert "may inspect and edit files" not in captured["process"].stdin.text
    assert result.text == "No changes."


def test_codex_agent_passes_optional_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        captured["command"] = command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        return FakePopen(stdout='{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = CodexCliAgent(
        codex_path="codex",
        model="gpt-5-codex",
        timeout_seconds=300,
    ).send(
        prompt="Explain",
        snapshot=snapshot(),
        project_root="",
        history=[("Earlier", "Earlier answer")],
    )

    command = captured["command"]
    assert command[command.index("--model") + 1] == "gpt-5-codex"
    assert result.text == "No changes."
    assert result.html == "<p>No changes.</p>"
    assert result.proposals == ()
    assert result.event_count == 1


def test_codex_agent_surfaces_login_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        return FakePopen(stderr="not logged in", returncode=1)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="codex login"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
        )


def test_codex_agent_surfaces_schema_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        return FakePopen(
            stderr=(
                'ERROR: {"type":"error","error":{"code":"invalid_json_schema",'
                '"message":"required is required"}}'
            ),
            returncode=1,
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="rejected the response schema"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=300,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
        )


def test_codex_agent_streams_malformed_json_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message": "No changes.", "message_html": "<p>No changes.</p>", "patch": null}',
            encoding="utf-8",
        )
        return FakePopen(stdout='not json\n{"type":"turn.completed"}\n')

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    events: list[dict[str, Any]] = []

    result = CodexCliAgent(
        codex_path="codex",
        model="",
        timeout_seconds=300,
    ).send(
        prompt="Explain",
        snapshot=snapshot(),
        project_root="",
        history=[],
        event_callback=events.append,
    )

    assert result.event_count == 2
    assert events[0] == {"type": "malformed_json", "line": "not json"}
    assert events[1] == {"type": "turn.completed"}


def test_codex_agent_kills_timed_out_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
    ) -> FakePopen:
        process = FakePopen(returncode=None)
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="timed out"):
        CodexCliAgent(
            codex_path="codex",
            model="",
            timeout_seconds=0,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
        )

    assert captured["process"].killed


def test_codex_activity_renderer_compacts_verbose_activity() -> None:
    renderer = CodexActivityRenderer()

    live_lines = [
        renderer.record({"type": "turn.started"}),
        renderer.record({"type": "exec_command_begin", "cmd": "rg canonical"}),
        renderer.record({"type": "exec_command_output", "output": "line 1\nline 2"}),
        renderer.record({"type": "reasoning", "summary": "Checking the source."}),
        renderer.record({"type": "unexpected.future.event"}),
    ]

    assert live_lines == [
        "[status] turn started",
        "[tool] rg canonical",
        "[output] line 1 line 2",
        "[reasoning] Checking the source.",
        "[event] unexpected.future.event",
    ]
    assert renderer.compact_summary() == (
        "[Codex activity: 5 stream events, tools: rg canonical, 1 output chunk, "
        "reasoning: Checking the source., 1 other event type]\n"
    )


def test_codex_activity_renderer_streams_web_search_details() -> None:
    renderer = CodexActivityRenderer()

    live_lines = [
        renderer.record({"type": "web_search"}),
        renderer.record({"type": "web_search", "query": "anki 24.11 release"}),
        renderer.record(
            {
                "type": "web_search_begin",
                "action": {
                    "type": "search",
                    "queries": ["fsrs optimizer", "anki fsrs"],
                },
            }
        ),
        renderer.record(
            {
                "type": "response_item",
                "payload": {
                    "type": "web_search_call",
                    "action": {
                        "type": "open_page",
                        "url": "https://docs.ankiweb.net/searching.html",
                    },
                    "status": "in_progress",
                },
            }
        ),
        renderer.record(
            {
                "type": "item.started",
                "item": {
                    "type": "web_search_call",
                    "action": {
                        "type": "find_in_page",
                        "pattern": "filtered deck",
                        "url": "https://docs.ankiweb.net/filtered-decks.html",
                    },
                },
            }
        ),
        renderer.record({"type": "web_search_end", "duration_ms": 1234}),
    ]

    assert live_lines == [
        "[web] search",
        "[web] search: anki 24.11 release",
        "[web] search: fsrs optimizer; anki fsrs",
        "[web] open page: https://docs.ankiweb.net/searching.html",
        (
            "[web] find in page: filtered deck "
            "(https://docs.ankiweb.net/filtered-decks.html)"
        ),
        "[web] completed in 1.2s",
    ]
    assert renderer.compact_summary() == (
        "[Codex activity: 6 stream events, web: search; search: anki 24.11 "
        "release; search: fsrs optimizer; anki fsrs; +3 more]\n"
    )


def test_codex_activity_renderer_adds_safe_status_event_metadata() -> None:
    renderer = CodexActivityRenderer()

    status_line = renderer.record(
        {
            "type": "item.started",
            "status": "in_progress",
            "phase": "run",
            "name": "inspect",
            "query": "visible query",
            "result_count": 2,
            "content": "private content",
            "raw_content": "raw private reasoning",
            "encrypted_content": "encrypted-private-reasoning",
        }
    )
    event_line = renderer.record(
        {
            "type": "unexpected.future.event",
            "action": {"type": "search", "query": "safe query"},
            "url": "https://example.com",
            "content": "private event content",
        }
    )

    assert status_line == (
        "[status] item started (status=in_progress, phase=run, name=inspect, "
        "query=visible query, result_count=2)"
    )
    assert event_line == (
        "[event] unexpected.future.event (action=search, query=safe query, "
        "url=https://example.com)"
    )
    rendered = "\n".join(renderer.detail_lines)
    assert "private content" not in rendered
    assert "raw private reasoning" not in rendered
    assert "encrypted-private-reasoning" not in rendered
    assert "private event content" not in rendered


def test_codex_activity_renderer_streams_nested_reasoning_summary_only() -> None:
    renderer = CodexActivityRenderer()

    line = renderer.record(
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "Checking"},
                    {"type": "summary_text", "text": "the source."},
                ],
                "content": "raw private reasoning",
                "encrypted_content": "encrypted-private-reasoning",
            },
        }
    )

    assert line == "[reasoning] Checking the source."
    assert "raw private reasoning" not in line
    assert "encrypted-private-reasoning" not in line
    assert renderer.compact_summary() == (
        "[Codex activity: 1 stream event, reasoning: Checking the source.]\n"
    )
    assert renderer.detail_lines == ["[reasoning] Checking the source."]


def test_codex_activity_renderer_streams_item_completed_reasoning() -> None:
    renderer = CodexActivityRenderer()

    line = renderer.record(
        {
            "type": "item.completed",
            "item": {
                "type": "reasoning",
                "summary": "Checking the source.",
                "content": "raw private reasoning",
                "encrypted_content": "encrypted-private-reasoning",
            },
        }
    )

    assert line == "[reasoning] Checking the source."
    assert "raw private reasoning" not in line
    assert "encrypted-private-reasoning" not in line
    assert renderer.detail_lines == ["[reasoning] Checking the source."]


def test_codex_activity_renderer_streams_wrapped_empty_reasoning_summary() -> None:
    renderer = CodexActivityRenderer()

    line = renderer.record(
        {
            "type": "event_msg",
            "payload": {
                "type": "item.completed",
                "item": {
                    "type": "reasoning",
                    "summary": [],
                    "content": "raw private reasoning",
                    "encrypted_content": "encrypted-private-reasoning",
                },
            },
        }
    )

    assert line == "[reasoning] activity"
    assert "raw private reasoning" not in line
    assert "encrypted-private-reasoning" not in line
    assert renderer.compact_summary() == (
        "[Codex activity: 1 stream event, 1 reasoning activity update]\n"
    )
    assert renderer.detail_lines == ["[reasoning] activity"]


def test_codex_activity_renderer_ignores_reasoning_content_without_summary() -> None:
    renderer = CodexActivityRenderer()

    line = renderer.record(
        {
            "type": "reasoning",
            "content": "raw private reasoning",
            "encrypted_content": "encrypted-private-reasoning",
        }
    )

    assert line == "[reasoning] activity"
    assert "raw private reasoning" not in line
    assert "encrypted-private-reasoning" not in line
    assert renderer.reasoning_summaries == []
    assert renderer.detail_lines == ["[reasoning] activity"]


def test_codex_activity_renderer_can_hide_reasoning_summaries() -> None:
    renderer = CodexActivityRenderer(stream_reasoning_summaries=False)

    line = renderer.record(
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": "Checking the source.",
                "content": "raw private reasoning",
                "encrypted_content": "encrypted-private-reasoning",
            },
        }
    )

    assert line is None
    assert renderer.event_count == 1
    assert renderer.reasoning_count == 1
    assert renderer.reasoning_summaries == []
    assert renderer.compact_summary() == (
        "[Codex activity: 1 stream event, 1 reasoning activity update]\n"
    )


def test_compact_activity_transcript_replaces_live_tail() -> None:
    transcript = "You: Explain\nAssistant: [Live Codex activity]\n[tool] rg canonical\n"
    start = transcript.index("[Live Codex activity]")

    assert compact_activity_transcript(
        transcript, start, "[Codex activity: done]\n"
    ) == ("You: Explain\nAssistant: [Codex activity: done]\n")


def test_codex_cli_json_stream_smoke(tmp_path: Path) -> None:
    if os.environ.get("ANKI_CODEX_CLI_INTEGRATION") != "1":
        pytest.skip("set ANKI_CODEX_CLI_INTEGRATION=1 to run real Codex CLI smoke")

    repo_status_before = _git_status()
    project = tmp_path / "project"
    project.mkdir()
    source = project / "source.md"
    source.write_text(
        "A canonical divisor is attached to top forms.\n", encoding="utf-8"
    )
    events: list[dict[str, Any]] = []

    result = CodexCliAgent(
        codex_path=os.environ.get("ANKI_CODEX_CLI_PATH", ""),
        model="",
        timeout_seconds=120,
    ).send(
        prompt=(
            "Reply with one brief sentence about the source file. Do not propose "
            "note changes; use patch null. Include simple paragraph HTML in "
            "message_html."
        ),
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
        event_callback=events.append,
    )

    assert result.text
    assert result.html
    assert result.proposals == ()
    assert events
    assert source.read_text(encoding="utf-8") == (
        "A canonical divisor is attached to top forms.\n"
    )
    assert _git_status() == repo_status_before


def _git_status() -> str:
    if not (ROOT / ".git").exists():
        return ""
    return subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout


def test_resolve_codex_path_prefers_configured_value() -> None:
    assert resolve_codex_path("/custom/codex") == "/custom/codex"


def _claude_envelope(
    inner: dict[str, Any] | str,
    *,
    is_error: bool = False,
    subtype: str = "success",
    result_text: str = "Done.",
    **extra: Any,
) -> str:
    # Mirrors the real `claude -p --output-format json --json-schema` envelope:
    # the validated object is returned (already parsed) in `structured_output`,
    # while `result` only carries a short human-readable note. A string `inner`
    # represents the error case, where the message lands in `result`.
    envelope: dict[str, Any] = {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
    }
    if isinstance(inner, str):
        envelope["result"] = inner
    else:
        envelope["structured_output"] = inner
        envelope["result"] = result_text
    envelope.update(extra)
    return json.dumps(envelope)


def _claude_inner(
    *,
    message: str = "All set.",
    message_html: str = "<p>All set.</p>",
    patch: Any = None,
) -> dict[str, Any]:
    return {"message": message, "message_html": message_html, "patch": patch}


def test_resolve_claude_path_prefers_configured_value() -> None:
    assert resolve_claude_path("/custom/claude") == "/custom/claude"
    assert resolve_claude_path("  /spaced/claude  ") == "/spaced/claude"


def test_claude_effort_value_filters_to_valid_levels() -> None:
    for level in ("low", "medium", "high", "xhigh", "max"):
        assert claude_effort_value(level) == level
    for invalid in ("", "none", "minimal", "ultra", "Low"):
        assert claude_effort_value(invalid) == ""


def test_claude_agent_context_only_when_no_project_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        captured["command"] = command
        captured["cwd"] = cwd
        process = FakePopen(
            stdout=_claude_envelope(
                _claude_inner(message="Considered.", message_html="<p>Considered.</p>")
            )
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ClaudeCliAgent(
        claude_path="/usr/local/bin/claude",
        model="",
        timeout_seconds=123,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root="",
        history=[],
    )

    command = captured["command"]
    assert command[:2] == ["/usr/local/bin/claude", "-p"]
    assert command[command.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in command
    assert "--json-schema" in command
    json.loads(command[command.index("--json-schema") + 1])
    assert command[command.index("--tools") + 1] == ""
    assert "--add-dir" not in command
    assert "--permission-mode" not in command
    assert "--model" not in command
    assert "--effort" not in command
    assert "tools are disabled" in captured["process"].stdin.text
    assert "Improve this" in captured["process"].stdin.text
    assert "Current editor context is JSON" in captured["process"].stdin.text
    assert result.text == "Considered."
    assert result.html == "<p>Considered.</p>"
    assert result.proposals == ()
    assert result.event_count == 0


def test_claude_agent_uses_writable_project_folder_and_parses_patch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        captured["command"] = command
        captured["cwd"] = cwd
        process = FakePopen(
            stdout=_claude_envelope(
                _claude_inner(
                    message="Shorter front.",
                    message_html="<p>Shorter front.</p>",
                    patch={
                        "summary": "Shorten front",
                        "note_id": 123,
                        "notetype_id": 7,
                        "field_updates": [{"name": "Front", "html": "new front"}],
                        "tags": {"replace": None, "add": ["agent"], "remove": []},
                    },
                )
            )
        )
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ClaudeCliAgent(
        claude_path="claude",
        model="",
        timeout_seconds=123,
        project_folder_access=PROJECT_FOLDER_ACCESS_WORKSPACE_WRITE,
    ).send(
        prompt="Improve this",
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
    )

    command = captured["command"]
    assert command[command.index("--add-dir") + 1] == str(project.resolve())
    assert command[command.index("--permission-mode") + 1] == "acceptEdits"
    assert "--allowedTools" not in command
    assert "--tools" not in command
    assert captured["cwd"] == str(project.resolve())
    assert "inspect and edit files" in captured["process"].stdin.text
    assert "Do not modify files" not in captured["process"].stdin.text
    assert result.proposals[0].field_updates == {"Front": "new front"}


def test_claude_agent_read_only_project_folder_uses_allow_list(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        captured["command"] = command
        process = FakePopen(stdout=_claude_envelope(_claude_inner()))
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ClaudeCliAgent(
        claude_path="claude",
        model="",
        timeout_seconds=123,
        project_folder_access=PROJECT_FOLDER_ACCESS_READ_ONLY,
    ).send(
        prompt="Explain this",
        snapshot=snapshot(),
        project_root=str(project),
        history=[],
    )

    command = captured["command"]
    # Read-only is an allow-list of read-only tools: no edits, shell, or network,
    # and no acceptEdits permission mode.
    assert command[command.index("--allowedTools") + 1] == "Read Grep Glob"
    assert "--permission-mode" not in command
    assert "--disallowedTools" not in command
    assert "read-only tools" in captured["process"].stdin.text
    assert "Do not modify files" in captured["process"].stdin.text
    assert "inspect and edit files" not in captured["process"].stdin.text


def test_claude_agent_passes_model_and_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        captured["command"] = command
        return FakePopen(stdout=_claude_envelope(_claude_inner()))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ClaudeCliAgent(
        claude_path="claude",
        model="opus",
        timeout_seconds=123,
        reasoning_effort="high",
    ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])

    command = captured["command"]
    assert command[command.index("--model") + 1] == "opus"
    assert command[command.index("--effort") + 1] == "high"


def test_claude_agent_drops_invalid_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        captured["command"] = command
        return FakePopen(stdout=_claude_envelope(_claude_inner()))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    ClaudeCliAgent(
        claude_path="claude",
        model="",
        timeout_seconds=123,
        reasoning_effort="none",
    ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])

    assert "--effort" not in captured["command"]


def test_claude_agent_surfaces_login_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        return FakePopen(
            stdout=_claude_envelope(
                "Failed to authenticate. API Error: 401 authentication_error",
                is_error=True,
                api_error_status=401,
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="not logged in"):
        ClaudeCliAgent(
            claude_path="claude",
            model="",
            timeout_seconds=123,
        ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])


def test_claude_agent_surfaces_generic_api_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        return FakePopen(
            stdout=_claude_envelope(
                "529 overloaded_error",
                is_error=True,
                subtype="error_during_execution",
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="overloaded_error"):
        ClaudeCliAgent(
            claude_path="claude",
            model="",
            timeout_seconds=123,
        ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])


def test_claude_agent_reports_non_json_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        return FakePopen(stdout="not json at all\n")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="non-JSON"):
        ClaudeCliAgent(
            claude_path="claude",
            model="",
            timeout_seconds=123,
        ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])


def test_claude_agent_reads_structured_output_not_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With --json-schema the answer is in `structured_output`; `result` only has
    # a short human note. We must read the former, not the latter.
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        return FakePopen(
            stdout=_claude_envelope(
                _claude_inner(message="real answer", message_html="<p>real</p>"),
                result_text="Done.",
            )
        )

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ClaudeCliAgent(
        claude_path="claude",
        model="",
        timeout_seconds=123,
    ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])

    assert result.text == "real answer"
    assert result.html == "<p>real</p>"


def test_claude_agent_falls_back_to_result_json_without_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Older CLI / no structured payload: the JSON object is in `result` text.
    inner = json.dumps({"message": "fb", "message_html": "<p>fb</p>", "patch": None})

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        # Passing a string puts it in `result` with no `structured_output`.
        return FakePopen(stdout=_claude_envelope(inner))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ClaudeCliAgent(
        claude_path="claude",
        model="",
        timeout_seconds=123,
    ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])

    assert result.text == "fb"
    assert result.html == "<p>fb</p>"


def test_claude_agent_reports_missing_structured_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Success envelope but neither structured_output nor a result body.
    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        return FakePopen(stdout=_claude_envelope(""))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="did not return a structured response"):
        ClaudeCliAgent(
            claude_path="claude",
            model="",
            timeout_seconds=123,
        ).send(prompt="Explain", snapshot=snapshot(), project_root="", history=[])


def test_claude_agent_stops_running_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_popen(
        command: list[str],
        *,
        stdin: int,
        stdout: int,
        stderr: int,
        text: bool,
        bufsize: int,
        cwd: str,
    ) -> FakePopen:
        process = FakePopen(returncode=None)
        captured["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    with pytest.raises(AgentStopped):
        ClaudeCliAgent(
            claude_path="claude",
            model="",
            timeout_seconds=123,
        ).send(
            prompt="Explain",
            snapshot=snapshot(),
            project_root="",
            history=[],
            stop_requested=lambda: True,
        )

    assert captured["process"].terminated
