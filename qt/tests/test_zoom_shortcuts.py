# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).parents[2]


def _action_shortcut(ui_file: str, action_name: str) -> str | None:
    tree = ElementTree.parse(ROOT / "qt" / "aqt" / "forms" / ui_file)
    for action in tree.findall(".//action"):
        if action.get("name") != action_name:
            continue
        shortcut = action.find("./property[@name='shortcut']/string")
        return shortcut.text if shortcut is not None else None

    raise AssertionError(f"{action_name} not found in {ui_file}")


def test_zoom_shortcuts_are_defined_in_main_window() -> None:
    assert _action_shortcut("main.ui", "actionZoomIn") == "Ctrl+Shift++"
    assert _action_shortcut("main.ui", "actionZoomOut") == "Ctrl+Shift+_"


def test_zoom_shortcuts_are_defined_in_browser() -> None:
    assert _action_shortcut("browser.ui", "actionZoomIn") == "Ctrl+Shift++"
    assert _action_shortcut("browser.ui", "actionZoomOut") == "Ctrl+Shift+_"
