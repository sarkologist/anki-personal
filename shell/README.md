# Anki shell

A native macOS shell for Anki (Tauri 2 on the system WebKit). It runs the
existing `ts/` pages against an in-process Rust backend, without Qt, Python
or a bundled Chromium. See `native-shell-port-plan.md` for the plan and
findings. Phases 0 and 1 are done: profile, collection, page protocol,
bridge, settings import, AnkiConnect, the editor in WebKit (Edit only), and
reviewing: deck list, overview, reviewer, sync and native menus. Adding
cards, the browser, add-ons and most dialogs still need the old Anki.

## Reviewing

- The main window shows the deck list, a deck's overview, and the reviewer,
  with the toolbar (Decks, Stats, Sync) at the top. Studying works as in
  Anki: Space/Enter shows the answer, 1-4 answer, plus the reviewer's usual
  keys (`e` edit, `*` mark, `-`/`=` bury, `@`/`!` suspend, `u` undo, `r`
  replay, `i` card info, `o` options, Shift+A auto advance; `d` decks, `s`
  study, `t` stats, `y` sync). Flags (Cmd+1-7), undo/redo (Cmd+Z,
  Cmd+Shift+Z) and the rest are in the menus; "More" opens the card menu.
- Answer keys: h/j/k/l answer too by default (Again/Hard/Good/Easy). The
  profile import takes the old Anki's answer keys and the "hjkl answer keys"
  add-on's settings instead, if set. To change any shortcut, add
  `"shortcuts": {"answer1": ["1", ";"], "decks": ["d"]}` (action -> keys,
  see `src/shortcuts.rs`) to the settings file.
- Audio plays in the page (`<audio>`; WebKit plays mp3, m4a, wav, ogg,
  opus, flac, webm); files it refuses go to mpv (Anki's `anki_audio`, or
  Homebrew). TTS tags use the system voices (`speechSynthesis`).
- LaTeX images are made with latex and dvipng/dvisvgm, as in Anki.
- Sync: the toolbar's Sync logs in first if needed, then syncs the
  collection and media, asking before a full sync. It also syncs when the
  shell opens and closes if "sync on open/close" was on in the old Anki
  (`autoSync`). Sync redirects are kept (`currentSyncUrl`). A profile copy
  (any `--anki-base` other than the default) can only sync with a server on
  this machine (`customSyncUrl` pointing at 127.0.0.1), never AnkiWeb.
- Not yet: custom study, filtered deck creation, deck descriptions editing,
  "Set due date", "Forget", "Create copy", recording your own voice, dark
  mode, and add-ons.

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
- `--auto-sync`: sync on open and close in background runs too (sync tests).
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
  are saved. `--test harness/main-flow.js --test-page main` adds a test deck
  and reviews it through the deck list, overview and reviewer: keys, undo,
  typed answers, audio, TTS and LaTeX. `harness/sync-flow.js` syncs through
  the UI against a local server (`anki-sync-server` with
  `SYNC_USER1=test:pass`; point the copy's `customSyncUrl` at it and clear
  `syncKey`): log in and full upload, then `--auto-sync` for the sync on
  open and close, or a fresh profile for the full download. With the screen
  locked, raise `ANKI_SHELL_TEST_TIMEOUT` (seconds). `harness/editor-typing.js` measures keystroke-to-paint latency on
  a given note; it needs an unlocked screen, because macOS hides all windows
  while the screen is locked.
- `python3 shell/harness/bench.py`: the memory check. It drives the shell's
  reviewer (main window, toolbar and bottom bar included) through 240 cards
  on a synthetic profile and fails above `--max-total-mb` (400) or
  `--max-growth-mb` (150). It also reports render latency (in the app, and
  in the page until shown). `--collection` runs on a copy of a real
  collection instead, and `--compare-stock` runs the same workload in an
  isolated stock Anki. Use a release build (`cargo build --release -p
  anki-shell`, `--shell target/release/anki-shell`) for numbers.

Always test on copies of a profile, never the live one.
