# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Print the old Anki's profile list and sync settings as JSON.

usage: python import_prefs.py <Anki2 base folder>

prefs21.db holds pickled dicts that contain Qt objects, so this reuses
aqt.profiles' unpickler (it needs an Anki install's Python, with PyQt). The
database is opened read-only, so the old Anki can keep running.
"""

import json
import sqlite3
import sys
from pathlib import Path

from aqt.profiles import ProfileManager

SYNC_KEYS = (
    "syncKey",
    "syncUser",
    "hostNum",
    "currentSyncUrl",
    "customSyncUrl",
    "syncMedia",
    "autoSync",
)


def main(base: Path) -> None:
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
    }
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
