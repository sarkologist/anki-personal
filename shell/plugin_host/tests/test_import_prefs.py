# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""The profile import's reading of add-on LaTeX toolchains
(shell/scripts/import_prefs.py), which parses add-ons without running them."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
_spec = importlib.util.spec_from_file_location(
    "import_prefs", REPO / "shell/scripts/import_prefs.py"
)
assert _spec and _spec.loader
import_prefs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(import_prefs)

# the "Edit LaTeX toolchain" add-on's file, shortened
TOOLCHAIN = """
from anki import latex

#latex.pngCommands = [["xelatex", "tmp.tex"]]
latex.pngCommands = [
    ["pdflatex", "--interaction=nonstopmode", "tmp.tex"],
    ["sips", "-s", "format", "png", "tmp.pdf", "--out", "tmp.png"]
]

latex.svgCommands = [
    ["pdflatex", "--interaction=nonstopmode", "tmp.tex"],
    ["/usr/local/bin/pdf2svg", "tmp.pdf", "tmp.svg"]
]
"""


def addon(base: Path, name: str, code: str, disabled: bool = False) -> None:
    folder = base / "addons21" / name
    folder.mkdir(parents=True)
    (folder / "toolchain.py").write_text(code, encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"disabled": disabled}))


def test_reads_the_commands_an_enabled_add_on_sets(tmp_path: Path) -> None:
    addon(tmp_path, "933614828", TOOLCHAIN)
    out = import_prefs.latex_commands(tmp_path)
    assert out["png"][1] == [
        "sips",
        "-s",
        "format",
        "png",
        "tmp.pdf",
        "--out",
        "tmp.png",
    ]
    assert out["svg"][1][0] == "/usr/local/bin/pdf2svg"


def test_ignores_disabled_add_ons_and_other_assignments(tmp_path: Path) -> None:
    addon(tmp_path, "off", TOOLCHAIN, disabled=True)
    addon(
        tmp_path,
        "other",
        "\n".join(
            [
                # not literals, not commands, or not anki.latex's
                "latex.pngCommands = make_commands()",
                "latex.svgCommands = ['latex', 'tmp.tex']",
                "other.pngCommands = [['x']]",
                "def f():\n    latex.pngCommands = [['inner']]",
            ]
        ),
    )
    addon(tmp_path, "broken", "latex.pngCommands = [[")
    assert import_prefs.latex_commands(tmp_path) == {}


def test_no_add_ons_folder(tmp_path: Path) -> None:
    assert import_prefs.latex_commands(tmp_path) == {}
