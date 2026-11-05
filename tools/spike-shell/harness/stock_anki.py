"""Launch a second, isolated stock Anki instance for benchmarking.

usage: python stock_anki.py <base folder>
The base must contain "User 1/collection.anki2" (+ collection.media).
"""
import os
import sys

base = sys.argv[1]
os.environ.setdefault("QTWEBENGINE_REMOTE_DEBUGGING", "8081")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--remote-allow-origins=*")
os.environ["ANKI_API_PORT"] = "40001"

import aqt
from aqt.profiles import ProfileManager
from pathlib import Path

# the single-instance guard is keyed by username; give this instance its own
aqt.AnkiApp.KEY = aqt.AnkiApp.KEY + "-bench"

pm = ProfileManager(Path(base))
pm.setupMeta()
pm.setLang("en_US")
pm.meta["firstRun"] = False
profiles = pm.profiles()  # creates "User 1" if missing
pm.load(profiles[0])
pm.save()
pm.db.close()

sys.argv = ["anki", "-b", base]
aqt.run()
