# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Print the old Anki's profile list and sync settings as JSON.

usage: python import_prefs.py <Anki2 base folder>

prefs21.db holds pickled dicts that contain Qt objects, so this reuses
aqt.profiles' unpickler (it needs an Anki install's Python, with PyQt). The
database is opened read-only, so the old Anki can keep running.
"""

from __future__ import annotations

import ast
import json
import sqlite3
import sys
from pathlib import Path

SYNC_KEYS = (
    "syncKey",
    "syncUser",
    "hostNum",
    "currentSyncUrl",
    "customSyncUrl",
    "syncMedia",
    "autoSync",
)


def latex_commands(base: Path) -> dict[str, list[list[str]]]:
    """The `pngCommands`/`svgCommands` that enabled add-ons set on
    `anki.latex` at module level (as "Edit LaTeX toolchain" does), keyed
    "png"/"svg". The files are parsed, not run."""
    out: dict[str, list[list[str]]] = {}
    addons = base / "addons21"
    folders = sorted(addons.iterdir()) if addons.is_dir() else []
    for folder in folders:
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if not folder.is_dir() or (isinstance(meta, dict) and meta.get("disabled")):
            continue
        for path in sorted(folder.glob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            # an odd file mustn't stop the profile import
            except Exception:
                continue
            for node in tree.body:
                if not isinstance(node, ast.Assign):
                    continue
                for target in node.targets:
                    kind = _latex_target(target)
                    if kind is None:
                        continue
                    try:
                        value = ast.literal_eval(node.value)
                    except Exception:
                        continue
                    if _is_command_list(value):
                        out[kind] = value
    return out


def _latex_target(target: ast.expr) -> str | None:
    """"png"/"svg" for `latex.pngCommands`/`anki.latex.svgCommands`."""
    if not isinstance(target, ast.Attribute):
        return None
    if target.attr not in ("pngCommands", "svgCommands"):
        return None
    owner = target.value
    name = owner.attr if isinstance(owner, ast.Attribute) else getattr(owner, "id", None)
    if name != "latex":
        return None
    return target.attr[:3]


def _is_command_list(value: object) -> bool:
    return (
        isinstance(value, list)
        and len(value) > 0
        and all(
            isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd)
            for cmd in value
        )
    )


def main(base: Path) -> None:
    from aqt.profiles import ProfileManager

    db = sqlite3.connect(f"{(base / 'prefs21.db').as_uri()}?mode=ro", uri=True)
    rows = db.execute("select name, cast(data as blob) from profiles").fetchall()
    db.close()
    # _unpickle doesn't use the instance
    unpickle = ProfileManager._unpickle
    meta: dict = {}
    profiles = []
    for name, data in rows:
        values = unpickle(None, data)  # type: ignore[arg-type]
        if name == "_global":
            meta = values
            continue
        profile = {"name": name}
        for key in SYNC_KEYS:
            value = values.get(key)
            if isinstance(value, (str, int, bool)) or value is None:
                profile[key] = value
        profiles.append(profile)
    profiles.sort(key=lambda p: p["name"].lower())
    out = {
        "profiles": profiles,
        "lastLoaded": meta.get("last_loaded_profile_name"),
        # ProfileManager.get_answer_key; the dict's keys are ints
        "answerKeys": {str(k): v for k, v in (meta.get("answer_keys") or {}).items()},
        "spacebarRatesCard": meta.get("spacebar_rates_card"),
        "theme": meta.get("theme") if isinstance(meta.get("theme"), int) else None,
        "latexCommands": latex_commands(base) or None,
    }
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
