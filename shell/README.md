# Anki shell

A native macOS shell for Anki (Tauri 2 on the system WebKit). It runs the
existing `ts/` pages against an in-process Rust backend, without Qt, Python
or a bundled Chromium. See `native-shell-port-plan.md` for the plan and
findings. Phases 0 to 3 are done: profile, collection, page protocol,
bridge, settings import, AnkiConnect, reviewing (deck list, overview,
reviewer, sync, native menus), editing (Add Cards, Edit Current), and a
plugin host running Chinese/French Frontier, the editor agent pane and the
Alfred bridge. The browser, other add-ons and most dialogs still need the
old Anki.

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
- Dark mode follows the system's appearance, also when it changes while
  the shell runs, as in Anki. `"theme": "light"` or `"dark"` in the
  settings file fixes it instead; the default is the old Anki's theme
  (imported), else the system. Pages and cards (`.nightMode`) switch at
  once, and windows show Anki's background colour while a page loads.
  Add-ons get it too (Frontier's panels take it when they open).
- Not yet: custom study, filtered deck creation, deck descriptions editing,
  "Set due date", "Forget", "Create copy", recording your own voice, and
  add-ons other than those below.

## Preferences

**Anki > Preferences…** (Cmd+,) has what the old Anki's preferences offer
that the shell uses: the theme, the review and scheduling options, the
editing and search defaults, backups, and the LaTeX commands. Changes are
saved as you make them.

- **Theme:** system, light or dark (see "Reviewing").
- **LaTeX commands:** the commands that turn `tmp.tex` into `tmp.png` or
  `tmp.svg`, one per line. They start as the ones the "Edit LaTeX
  toolchain" add-on sets in the old Anki (the profile import reads its
  file without running it), or latex.py's defaults. "Use Default Commands"
  goes back to those.

## Add-ons

Add-ons run in a Python plugin host, from the old Anki's `addons21` folder
(sharing its config and files). Supported: Chinese/French Frontier
(`chinese_frontier`), the editor agent pane (`editor_agent_pane`) and the
Alfred bridge (`alfred_anki_html_bridge`). To turn them on, quit the shell,
list them in the settings file, e.g. `"plugins": ["chinese_frontier",
"editor_agent_pane", "alfred_anki_html_bridge"]`, and start it again.
Their menu items are in Tools, with Add-on Log (the host's output,
`plugin-host.log` next to the settings) and Restart Add-ons. The host
needs an Anki install's Python, found as for the profile import
(`import_python`).

- **Agent pane:** the toolbar's Agent button (or Cmd+Option+Shift+E) in
  Add or Edit Current shows the pane beside the fields, as in Anki: the
  same settings, requests, transcript and Apply. Its × hides it. The agent
  queue window (Tools > Agent Queue) and the pane in the browser aren't in
  the shell yet.
- **Alfred bridge:** Alfred's "insert into Anki" pastes into the field you
  last left in Add or Edit Current, as in Anki, but at the field's end:
  the add-on's caret script needs `ShadowRoot.getSelection`, which WebKit
  hasn't got.

## Editing

- **Add** (toolbar, or `a` in the main window) opens Add Cards: the stock
  editor with Type and Deck choosers above it (type to filter, Enter to
  pick) and History, Close and Add below. Cmd+Enter adds, Escape closes
  (asking first if there's unsaved input), Cmd+Shift+H shows the history;
  a history entry opens the note in Edit Current. Sticky fields (the pin
  on each field) carry over to the next note, as do the
  tags.
- **Edit** (`e` while reviewing) opens Edit Current on the card's note;
  edits are saved as you type, and the reviewer shows them when the window
  closes. Cmd+Enter closes it.
- Everything on the editor's toolbar works: formatting, cloze, colours,
  MathJax and LaTeX (with previews), attach (a file picker), record (asks
  for the microphone the first time), image occlusion, the HTML editor and
  the options menu. Paste filters HTML from other apps as Anki does,
  downloads remote images into the media folder, and keeps HTML copied
  from a field as is; Shift while pasting flips "strip formatting". Files
  dropped from Finder go into the media folder.
- The Mini Format Pack buttons are on the toolbar's right, with the
  add-on's hotkeys (Qt's Ctrl is Cmd): strikethrough (Option+Shift+5),
  `<abbr>` (Cmd+Shift+Option+A), code block (Cmd+.), inline code (Cmd+,),
  horizontal line (Cmd+Shift+Option+_), lists (Cmd+[ and Cmd+]),
  indent/outdent (Cmd+Shift+] and Cmd+Shift+[), and alignment
  (Cmd+Shift+Option+S/L/R/B).
- Not yet: Cards (card layout) and Fields open nothing but a note; "Show
  Duplicates" lists the duplicates and opens the one you pick in Edit
  Current, since there's no browser yet.

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
  are saved. `harness/editor-features.js` (same page) checks paste, undo,
  input method composition, MathJax/LaTeX, format buttons and options;
  pastes come from a scratch pasteboard, never the clipboard.
  `--test harness/editor-add.js --test-page add` drives Add Cards: the
  choosers, sticky fields, adding, history, duplicates and format buttons.
  `--test harness/preferences.js --test-page preferences` drives the
  Preferences window (`--test-page` also takes the other screens' names;
  see `src/testchan.rs`): the theme, a collection preference and the LaTeX
  commands; the copy's `addons21` needs the "Edit LaTeX toolchain" add-on.
  `--test harness/dark-mode.js --test-page main` switches the theme and a
  simulated system appearance through the deck list, overview and
  reviewer (it changes no system setting).
  `harness/agent-pane.js` (`--test-page add`, and `--test-page editor
  --test-note 0`) drives the agent pane with a real request; it needs the
  add-on in the copy's `addons21` with its provider set to Ollama and a
  local model. `harness/alfred-bridge.js` (`--test-page add`) calls the
  Alfred bridge's server as the workflow does.
  `--test harness/main-flow.js --test-page main` adds a test deck
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
