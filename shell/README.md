# Anki shell

A native macOS shell for Anki (Tauri 2 on the system WebKit). It runs the
existing `ts/` pages against an in-process Rust backend, without Qt, Python
or a bundled Chromium. See `native-shell-port-plan.md` for the plan and
findings. This is phase 0: profile, collection, page protocol, bridge,
settings import, AnkiConnect, and the editor working in WebKit. There is no
reviewer, deck list or browser yet, so use the old Anki for those.

## Build and run

```
./ninja shell
open out/shell/AnkiShell.app
```

`./ninja shell` builds the web assets and the binary, then bundles
`out/shell/AnkiShell.app` with the assets in `Contents/Resources/web`. During
development, `cargo run -p anki-shell` finds `out/qt/_aqt/data/web` by itself.

The shell uses the old Anki's profile folders (`~/Library/Application
Support/Anki2`). Only one app can have a collection open at a time; the
shell shows rslib's lock error if the old Anki has it, so close the profile
there first. On first start it imports the profile list and sync settings
from `prefs21.db`, using an Anki install's Python (`ANKI_SHELL_PYTHON`, the
launcher's venv, or `~/.anki-stable`). **Profiles > Re-import from Anki**
repeats that import.

Options:

- `--settings PATH`: the settings file (default
  `~/Library/Application Support/AnkiShell/settings.json`)
- `--anki-base PATH`: another Anki base folder, e.g. a copy for testing
- `--profile NAME`
- `--ankiconnect-port N`: the native AnkiConnect port (0 disables it). The
  default, the API key and the CORS origins come from the AnkiConnect
  add-on's settings in the old Anki, or the add-on's defaults (8765). If the old Anki's AnkiConnect has the port, the shell logs it
  and carries on without one.
- `--import`: re-import profiles at startup
- `--background`: never take focus, and keep windows behind other windows
  (implied by `--test` and `--bench`).
- `--geometry x,y,width,height`

## Checks

- `cargo test -p anki-shell`: unit tests and the AnkiConnect smoke test
  (`tests/ankiconnect_smoke.rs`, which runs over HTTP on an ephemeral port).
- WebKit checks, through the shell's test channel (`src/testchan.rs`).
  They run in a real WKWebView and check results against the collection:

  ```
  anki-shell --anki-base <copy> --test shell/harness/editor-selection.js \
      --test-page editor --test-note 0
  ```

  `--test-note 0` creates a fresh cloze note. The exit status is 0 on
  success. `--test` and `--bench` refuse to run on the live Anki folder.
  `harness/editor-save.js` checks that edits made just before a note switch
  are saved. `harness/editor-typing.js` measures keystroke-to-paint latency on
  a given note; it needs an unlocked screen, because macOS hides all windows
  while the screen is locked.
- `python3 shell/harness/bench.py`: the memory check. It drives the stock
  reviewer page through 240 cards on a synthetic profile and fails above
  `--max-total-mb` (400) or `--max-growth-mb` (150). `--collection` runs on a
  copy of a real collection instead, and `--compare-stock` runs the same
  workload in an isolated stock Anki. Run it with the screen unlocked.

Always test on copies of a profile, never the live one.
