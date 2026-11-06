# Native shell port plan

Replace the Qt + QtWebEngine + Python desktop shell with a native Rust shell
on the system WebView, keeping the Rust core (`rslib`) and the Svelte UI (`ts/`).
Evidence: the spike in draft PR
[sarkologist/anki-personal#143](https://github.com/sarkologist/anki-personal/pull/143)
(`tools/spike-shell/PLAN.md` on `claude/spike-native-shell`): about 5x less memory (135-252 MB vs 893-1506 MB on
the same workload), steadier typing on large notes, identical rendering of the
existing reviewer and editor pages.

Status: proposal. Update the checkboxes and "Findings" as phases land.

## What stays, what goes

```
               today                               after
┌──────────────────────────────┐    ┌──────────────────────────────┐
│ ts/  Svelte pages            │    │ ts/  Svelte pages (+ new)     │  keep, extend
├──────────────────────────────┤    ├──────────────────────────────┤
│ QtWebEngine (bundled Chromium)│    │ WKWebView (system WebKit)     │  replace
│ qt/aqt  Python + Qt widgets   │    │ shell/  Rust (Tauri 2)        │  replace
│ pylib   Python wrapper        │    │ plugin host: Python sidecar   │  repurpose
├──────────────────────────────┤    ├──────────────────────────────┤
│ rslib  Rust core              │    │ rslib  Rust core              │  keep
└──────────────────────────────┘    └──────────────────────────────┘
```

- **Keep, unchanged:** `rslib`, `proto/`, `ftl/`, and every existing `ts/` page.
  Upstream merges keep flowing into these.
- **Replace:** `qt/aqt` (about 34k lines). This is the Qt windows plus the Python
  glue that drives the pages.
- **Repurpose:** `pylib`. Its collection API goes through
  `backend.command(service, method, bytes)` and `db_command`, so pointing those
  at a socket makes existing Python collection code work out of process (see
  "Plugin host" for what else that needs). This is what makes the plugin host
  cheap.
- **Take over from Python:** some logic that looks like core lives in Python
  today and has to move into the shell (see "Python-side logic to port").

## Decisions

These are defaults chosen for this plan. Change them before starting phase 0.

1. **Platform: macOS only.** That means WebKit only. Linux (WebKitGTK) and
   Windows (WebView2, which is Chromium) are out of scope. They would double the
   browser-compat surface for a single user.
2. **Framework: Tauri 2.** It is built on the same wry/tao as the spike, and it
   adds native menus, multiple windows, dialogs, global shortcuts, bundling,
   signing and an updater. Hand-rolling those on bare wry is pure overhead.
3. **The bridge stays protocol-compatible.** Pages keep calling
   `pycmd`/`bridgeCommand(...)` and `POST /_anki/<method>`, and the shell
   implements the same commands. Upstream `ts/` changes then keep working
   without forking the pages.
4. **The old Anki stays installed as the fallback.** Both use the same profile
   folder and collection format (`rslib` writes both). Only one can open a
   collection at a time, and the shell must detect the lock and refuse.
5. **Add-ons run in a Python sidecar plugin host.** It handles collection logic
   through pylib-over-IPC plus a small `aqt` compatibility shim, and UI comes
   from a web contribution API. Python add-ons that patch Qt internals
   (`aqt.*` monkeypatching) are not supported, and are replaced case by case.

## Add-ons

| Add-on (enabled)             | Plan                                                                                                                                                                                                                          |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AnkiConnect                  | Native: a Rust HTTP server on :8765 implementing the subset actually used (Claude workflows, Alfred). Phase 0, so automations keep working from day one.                                                                      |
| chinese_frontier (11.5k py)  | Plugin host. Its data logic runs over pylib-IPC plus the `aqt` shim (`mw.col`, `CollectionOp`/`QueryOp`, `taskman`, add-on config, the hooks listed below); its Qt dialogs (about 80 widget uses) become web panels. Phase 2. |
| editor_agent_pane (13.6k py) | Plugin host. The agent processes stay in Python; the pane becomes a web side panel in the editor window. Phase 3.                                                                                                             |
| suspended_card_priority      | Needs a "custom browser column" plugin API (`browser_did_fetch_row`-style). Phase 4.                                                                                                                                          |
| alfred_anki_html_bridge      | Editor hooks (`editor_did_unfocus_field`, typing timer) are forwarded as plugin events. Phase 3.                                                                                                                              |
| hjkl answer keys             | Native configurable reviewer shortcuts. Phase 1.                                                                                                                                                                              |
| Edit LaTeX toolchain         | Native setting for the LaTeX command. Phase 4.                                                                                                                                                                                |
| Mini Format Pack             | Pick the buttons actually used and add them to the editor toolbar. Phase 3.                                                                                                                                                   |
| Image Occlusion Enhanced     | Retire: built-in IO (`ts/routes/image-occlusion`) replaces it.                                                                                                                                                                |

### Plugin host

- **Process:** a `python` process started by the shell. It runs with this
  repo's `pylib` and a `RemoteBackend`, which implements `_run_command` and
  `db_command` over a Unix socket using length-prefixed (service, method, bytes)
  frames. The shell owns the only `Backend`; plugin calls are serialized through
  it like page calls.
- **pylib changes the remote mode needs:**
  - `_backend.py` imports `_rsbridge` and checks its buildhash at import time,
    so the remote mode must skip both. `lang.py` also creates its own
    `RustBackend` for the module-level `tr`, which needs a local i18n path or a
    remote one.
  - `Collection.__init__` calls `open_collection` and `close()` calls
    `close_collection`. A remote collection must attach to the shell's
    already-open one and never open or close it.
  - Cache coherence: pylib caches notetypes (`models._cache`), which aqt clears
    after each operation. The shell forwards `OpChanges` from its own
    operations to the sidecar, and decodes `OpChanges` from plugin operations
    to refresh its pages.
  - A full sync closes the collection, so the sidecar is told to pause.
- **`aqt` compatibility shim:** `mw` (`col`, `taskman`, `progress`,
  `addonManager.getConfig/writeConfig`), `aqt.operations`
  (`CollectionOp`/`QueryOp` run in the sidecar), and the `gui_hooks` the
  enabled add-ons use. Frontier alone uses `deck_browser_will_render_content`,
  `reviewer_will_show_context_menu`, `webview_did_receive_js_message`,
  `sync_did_finish`, `reviewer_did_answer_card` and `state_did_change`. Hooks
  that return or modify HTML need a round trip to the sidecar, so keep that
  list small.
- **API (`anki_plugin`):**
  - events: profile open/close, `operation_did_execute`, `state_did_change`,
    sync finished, reviewer show question/answer and answered card, editor
    load/unfocus/typing, browser row fetch, plus page messages
    (`webview_did_receive_js_message`);
  - contributions: menu items, toolbar/editor buttons, side panels and dialogs.
    A contribution is a web page served by the plugin, or a small JSON form
    spec;
  - `taskman`-style background work.
- **Lifecycle:** restarted on crash, and its stdout/stderr go to a log the shell
  shows. A plugin crash never takes the reviewer down.

## Python-side logic to port

These look like core features but run in Python today:

- **LaTeX images.** `pylib/anki/latex.py` renders `[latex]`/`[$]` to PNG/SVG
  with latex + dvipng/dvisvgm, from a `card_did_render` hook. This is used by
  card rendering, media check's "Render LaTeX" and the editor's legacy LaTeX
  preview. Port it to Rust in the shell (subprocesses as today) in phase 1,
  or cards with LaTeX show nothing.
- **Card text preparation.** `prepare_card_text_for_display` handles media
  filename escaping and turns `[anki:play:...]` tags into play buttons. Also
  the reviewer's `play:`, `ease`, `edit`, `more` and `statesMutated`
  commands.
- **Typed answers.** The `[[type:]]` substitution and its HTML are Python
  (`reviewer.py`); only the comparison is in Rust.
- **Backups.** `rslib` creates backups, but aqt decides when: on close (gated
  by a quick check) and on a 5-minute timer.
- **Sync redirects.** aqt persists the sync endpoint redirect
  (`set_current_sync_url`).
- **Paste filtering.** The editor's paste path uses BeautifulSoup and fetches
  remote images (`editor.py`).
- **Media server security.** `mediasrv.py` uses an API key, an editor CSP
  nonce and per-page endpoint whitelists (the reviewer gets only
  `getSchedulingStatesWithContext`, `setSchedulingStates`, `congratsInfo`,
  `i18nResources`). A custom protocol isn't reachable from other origins,
  but keep the per-page whitelist. Plugin assets need an `/_addons/`
  equivalent.
- **Audio.** Today it's mpv plus `say` for TTS. Check the collection's media
  extensions against what WebKit `<audio>` plays; keep mpv (from
  `anki_audio`) as a subprocess fallback if needed.

## Bridge and screen inventory

Bridge commands the existing pages send. About 40 in total, mostly editor:
`key:/blur:/focus:` field saves, `paste`, `cutOrCopy`, `attach`, `record`,
`saveTags`, `setTagsCollapsed`, `toggleSticky*`, `toggleMathjax`,
`toggleShrinkImages`, `toggleCloseHTMLTags`, `lastTextColor`,
`lastHighlightColor`, `dupes`, `cards`, `fields`, `preview`, `editorState`,
`addImageForOcclusion*`, `ioImageLoaded`, `saveFields`, `renderLatexPreview`,
`browserSearch`, `ans`, `updateToolbar`, `domDone`, `close`, deck browser and
overview link clicks. `qt/aqt/editor.py`'s `onBridgeCmd` and paste/media handling
(about 2.1k lines) are the reference.

| Screen                                                                                                  | Today                                                            | Work                                                                                                                           |
| ------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Reviewer                                                                                                | `reviewer.js` + Python driver                                    | Driver in Rust (proven in the spike), answer bar, shortcuts, undo, flags/marks, bury/suspend, audio, timers, auto-advance      |
| Deck list, overview, top toolbar                                                                        | Python-generated HTML                                            | New Svelte pages over existing backend calls (`deck_tree`, `congrats_info`, ...)                                               |
| Editor (Add / Edit Current)                                                                             | Svelte + `editor.py` bridge                                      | Bridge in Rust, plus the WebKit selection shim (below)                                                                         |
| Card browser                                                                                            | Qt model/view table, sidebar tree, `browser.py` (about 6k lines) | New Svelte virtualized table over `browser_row_for_id`, sidebar over `sidebar` tree calls, find/replace, duplicates, previewer |
| Card info, deck options, graphs, change notetype, import, image occlusion                               | Svelte routes                                                    | Host them, plus their bridge handlers (`deck_options_ready`, `import_done`, ...)                                               |
| Card layout, notetypes, fields                                                                          | Qt (`clayout.py`, `models.py`, `fields.py`, about 1.6k lines)    | New Svelte pages                                                                                                               |
| Filtered deck, custom study, preferences, profiles, sync login, export, media check, empty cards, about | Qt dialogs                                                       | New Svelte pages (mostly forms)                                                                                                |
| Debug console, add-on manager                                                                           | Qt                                                               | Drop; replaced by the plugin host log/settings                                                                                 |

## Known engine gaps (WebKit)

- **Shadow-DOM selection.** `ts/lib/tslib/cross-browser.ts#getSelection` uses
  `ShadowRoot.getSelection()`, which only Chromium has. WebKit falls back to
  `document.getSelection()`, whose selection is retargeted to the shadow host,
  so the caret location inside a field is lost and `restoreSelection` throws
  (`location.anchor`). Fix with a shim based on
  `Selection.getComposedRanges({shadowRoots})` behind the same helpers (used
  in about 23 files), under red/green vitest tests. Then exercise formatting, cloze
  wrap, MathJax/LaTeX overlays, field undo and paste in WebKit. **This is the
  biggest single technical risk, so do it first in phase 0.**
- **Not exercised by the spike:** deck options, graphs, import,
  image-occlusion canvas, audio/video playback, drag-drop media, clipboard
  images, IME composition. Each phase's exit check covers its own screens.
- **Testing:** there's no WebDriver for WKWebView on macOS. Reuse the spike
  harness pattern: an in-app test channel that evals JS in a page and checks
  results through backend data, not screenshots.

## Phases

The goal is to make the shell the daily driver for reviewing early, then grow it
one screen at a time. The old Anki covers everything not ported yet.

### Phase 0: foundation and risk burn-down

- [x] `shell/` Tauri 2 crate in the workspace; `./ninja shell` builds it
      against `out/` web assets; app bundle.
- [x] Backend in-process: open profile/collection; the lock is free
      (`rslib` opens SQLite in exclusive locking mode), so just surface its
      error. Backups on close (with a quick check) and on a timer.
- [x] Protocol handler (`/_anki/*`, media, pages) and a generic bridge
      dispatcher.
- [x] Settings: own JSON, plus an import of the profile list and sync
      credentials (`syncKey`, `syncUser`, `hostNum`, `currentSyncUrl`,
      `customSyncUrl`) from `prefs21.db`. Its pickles contain Qt objects
      (`sip._unpickle_type`), so the import runs in Python using
      `aqt.profiles`' unpickler, not `serde-pickle`. Offer a "re-import"
      action, since the old Anki can change them while it's the fallback (a
      re-login, a sync redirect).
- [x] Native AnkiConnect subset; check it against the actions existing
      automations call (Claude workflows, Alfred).
- [x] WebKit selection shim with tests, so the editor works with selection in
      WebKit.
- [x] Leak/perf harness: the spike's `bench.py` as a CI-runnable check.

Exit: the shell opens your profile, the AnkiConnect smoke test passes, and the
editor selection tests pass in WebKit.

### Phase 1: review-only daily driver

- [x] Deck list, overview, congrats, top toolbar (Svelte).
- [x] Reviewer: question/answer, answer bar, configurable shortcuts (replaces
      hjkl), undo/redo, flags, marks, bury/suspend, auto-advance, timers,
      typed answers, card text preparation, the scheduling-state endpoints.
- [x] LaTeX image generation ported to Rust.
- [x] Audio via HTML5 `<audio>` in the page (mpv fallback if formats need
      it); TTS via `AVSpeechSynthesizer`.
- [x] Sync: login, normal and media sync, full-sync prompts, sync on open and
      close.
- [x] Native menus.

Exit: a week of real reviews only in the shell; memory checked with the
`memtrack` sampler. **Decision gate:** if memory or latency isn't still clearly
better under real use, stop here.

### Phase 2: plugin host and Frontier

- [x] Sidecar process, `RemoteBackend` (attach, no open/close, no
      buildhash), `OpChanges` forwarding both ways, `aqt` shim, events,
      contributions, `/_addons/` assets, logging, restart.
- [x] chinese_frontier running unchanged except its UI, which moves to web
      panels.

### Phase 3: editing

- [x] Add Cards and Edit Current windows; every editor bridge command; paste,
      media import and recording; LaTeX preview; MathJax; image occlusion
      entry points.
- [x] Notetype/deck/tag choosers.
- [x] Formatting buttons (Mini Format Pack).
- [x] editor_agent_pane as a side panel; alfred bridge events (both
      unchanged, in the plugin host).

Exit: a week of adding and editing only in the shell.

### Phase 4: browser and the rest

- [ ] Card browser: table, sidebar, search, column config including the plugin
      column API (suspended_card_priority), find/replace, duplicates,
      previewer, card info.
- [x] Preferences, dark mode, and the LaTeX toolchain setting.
- [x] Filtered decks and custom study.
- [x] Media check and empty cards.
- [x] Card layout.
- Deferred, as unused (see "Phase 4: the other screens"): Manage Note
  Types, Fields, import/export, deck description editing, Set due date,
  Forget, Create copy, and recording your own voice.

Exit: the old Anki is no longer opened for a month, then it is retired.

## Sizing

Relative sizes, from the line counts above. Calendar time depends on how much
runs in parallel.

| Phase | Size | Main cost                                                    |
| ----- | ---- | ------------------------------------------------------------ |
| 0     | M    | Selection shim + test coverage; settings import              |
| 1     | L    | Deck list/overview pages; sync UX; LaTeX port; typed answers |
| 2     | L    | Remote pylib + `aqt` shim; Frontier's Qt UI to web           |
| 3     | L    | Editor bridge + paste/media; agent pane UI                   |
| 4     | L    | Browser table + sidebar; many small form screens             |

## Risks

- **WebKit editing behaviour** beyond selection (contenteditable quirks, IME).
  Mitigation: phase 0 shim plus per-feature WebKit checks. The old Anki stays
  available for editing until phase 3 exits.
- **Divergence from upstream on page/bridge contracts.** Upstream may change a
  page's bridge commands. Mitigation: the bridge dispatcher logs unknown
  commands, and the harness loads every page after each upstream merge.
- **Scope creep from rebuilding Qt screens one-to-one.** Mitigation: port what
  you actually use (check against your usage) and drop the rest.
- **Python 3.13 + pylib in the sidecar is still Python.** That is acceptable: it
  only runs plugins, off the UI path, and only when a plugin is enabled.

## Findings

(append per phase)

### Phase 0 (2026-11-05)

The exit criteria are met. The shell opens a copy of the real profile (29,857
cards). The AnkiConnect smoke test passes, and so do the editor selection
checks in WKWebView. How to build, run and check it is in `shell/README.md`.

- **Shell:** `shell/` (crate `anki-shell`) uses Tauri 2 without
  `tauri-cli`. The fork is macOS only (`AGENTS.md`), so `.deny.toml`
  audits only macOS targets; Tauri's Linux gtk stack isn't checked.
  `./ninja shell` builds the binary and `shell/bundle.sh`
  assembles an ad-hoc-signed `out/shell/AnkiShell.app` with the web assets in
  `Resources/web`. The backend method table (snake_case name -> service and
  method index) is generated in `build.rs` with `anki_proto_gen`, the same
  way `_backend_generated.py` is, so it can't drift.
- **Collection:** Opening the profile the old Anki holds fails with rslib's
  `DbErrorKind::Locked` ("Anki already open, or media currently syncing."),
  and the home page shows it with a retry. Closing runs `pragma quick_check`,
  then a throttled backup (skipped if the check fails), then
  `await_backup_completion`. A 5-minute timer asks for a backup, and the
  backend decides whether one is due.
- **Protocol:** `anki://localhost/` mirrors `mediasrv.py`:
  - per-webview kinds with the same API-access split as `webview.py`;
  - the four-endpoint whitelist for the reviewer and previewer;
  - `exposed_backend_list` plus `post_handler_list` for pages with access;
  - the `application/binary` content-type guard;
  - the editor CSP nonce;
  - SvelteKit routes;
  - legacy path remaps;
  - path-traversal guards (tested, with the guards mutated out to check the
    tests fail).

  Endpoints that need UI state (scheduling states, `deckOptionsReady`, ...)
  go to handlers registered by the app; the rest return an error until their
  phase. `/_addons/` is a 404 until phase 2.
- **Bridge:** `pycmd`/`bridgeCommand` are a `fetch` POST to
  `/_shell/bridge`, chained so commands arrive in order as on Qt's channel.
  Requests must carry an `X-Anki-Bridge` header and, if they have an origin,
  come from `anki://localhost`. A no-cors request from other content (an
  iframe in a field) can't send the header.
- **Navigation:** webviews navigate only to Anki's pages, plus the web for
  frames in cards and fields (YouTube embeds). Link clicks in the main frame
  go to the browser through an `openUrl` bridge command
  (`acceptNavigationRequest`). Tauri's navigation hook runs for every frame
  and can't tell them apart, so a script-driven main-frame navigation to the
  web isn't blocked. Such a page can't reach the bridge or the API. `domDone` is sent at `DOMContentLoaded`, and
  evals are queued until then (`_queueAction`). Unknown commands are logged
  and counted. The editor sends `editorState` on load, which nothing handles
  yet (phase 3).
- **Editing the current note:**
  - `key:`, `blur:`, `saveFields:` and `saveTags:` save through
    `update_notes`. They start from the note as stored, so changes made
    meanwhile (AnkiConnect) aren't reverted.
  - Before the editor closes, loads another note, or the app quits, the shell
    runs `saveNow()` and waits for it over the ordered bridge
    (`call_after_note_saved`), with a 2 s timeout. Quit is a custom menu
    item, because the predefined one sends `terminate:`, which skips
    Tauri's `ExitRequested`. Without this, edits made
    within the 600 ms save debounce were lost. `shell/harness/editor-save.js`
    checks it.
  - `escape_media_filenames` isn't applied on load yet.
- **Settings:** `~/Library/Application Support/AnkiShell/settings.json`. The
  import runs `shell/scripts/import_prefs.py` (embedded in the binary) with
  an Anki install's Python and `-I`, and reuses
  `ProfileManager._unpickle`. It opens `prefs21.db` read-only, so the old
  Anki can stay open. It imports the profile names, `syncKey`, `syncUser`,
  `hostNum`, `currentSyncUrl`, `customSyncUrl`, `syncMedia` and `autoSync`.
  Re-importing replaces the imported fields and keeps the shell's own
  (`last_profile`).
- **AnkiConnect:** it uses the add-on's own settings from the old Anki
  (`config.json` with the user's `meta.json` edits): `apiKey`, CORS origins
  and port. The action list comes from the actions this user's
  automations have called (Claude workflow transcripts, `~/anki-eq2mathjax`).
  The Alfred workflows use the separate `alfred_anki_html_bridge` add-on,
  not AnkiConnect, so they wait for phase 3.
  - 56 actions are native.
  - The add-on's other 65 return "<action> is not supported by the native
    shell yet": all `gui*`, `sync`, export/import, and notetype edits.
  - Response shapes follow the add-on. Two exceptions: keys come out in
    alphabetical order (`serde_json` without `preserve_order`), and
    `getProfiles` lists the profile folders.
  - 200 `cardsInfo` on the real collection take about 0.3 s.
- **WebKit selection** (WKWebView on macOS 27):
  - What WebKit has: there's no `ShadowRoot.getSelection`.
    `getComposedRanges({shadowRoots})` (the dictionary form) and
    `Selection.direction` both work. The anchor and focus are retargeted to
    the shadow host.
  - Two traps beyond the plan's:
    - `addRange()` silently ignores ranges inside a shadow root, as the spec
      says. `collapse`, `extend`, `setBaseAndExtent`, `collapseToEnd` and
      `modify` do accept them.
    - The document selection is already empty when `blur` fires during a
      focus change, which is when the editor saves the caret.
  - The shim (`ts/lib/tslib/cross-browser.ts`) returns a
    `ShadowRootSelection` adapter from the same `getSelection` helper. It
    reads through `getComposedRanges` and turns `addRange` into
    `setBaseAndExtent`. It also remembers the last range inside each shadow
    root (from `selectionchange` and from its own reads) while the selection
    is empty.
  - Tests: 15 vitest tests against a fake that reproduces the WebKit
    behaviour above, written red first. `shell/harness/editor-selection.js`
    runs in the real editor page: caret placement, caret restored after a
    refocus, bold (surround), cloze on a backward selection (wrap), and the
    result saved to the collection.
  - Before the shim, caret restore, bold and cloze all failed with
    `location.anchor` errors. With it, all checks pass.
- **WebKit editing, for phase 3:**
  - WebKit leaves a trailing placeholder `<br>` in the live DOM after
    formatting the end of a field; it isn't saved.
  - Native undo after Anki's wrap and surround, which edit the DOM directly,
    depends on timing. WebKit coalesces the typing on either side of them,
    so one undo can revert several edits. It hasn't been compared with
    Chromium yet. Anki has no field undo of its own.
  - Not exercised yet: MathJax/LaTeX overlays, paste, IME.
  - Firefox 142+ also has `getComposedRanges` without
    `ShadowRoot.getSelection`, so Gecko now gets the adapter too. Its reads
    are equivalent, because Gecko doesn't retarget. Anki ships no Gecko
    webview, so this was left as is.
- **Test channel and background runs:** there's no WebDriver for WKWebView,
  so `--test <script>` evaluates an async JS body in a real page and checks
  its result. `--test` and `--bench` run in the background:
  - Tauri's `Accessory` policy still gets activated at launch by tao, so the
    app starts as `Prohibited` and switches to `Accessory` on `Ready`.
  - Windows open unfocused and behind other windows (`orderBack:`), and
    WebKit's occlusion detection is turned off (SPI) so covered pages keep
    rendering.

  While the screen is locked, every page is `hidden`: there are no animation
  frames and timers are throttled to about 300 ms. The harness waits race
  `requestAnimationFrame` with a timer for that reason; the typing and memory
  benchmarks need an unlocked screen.
- **Memory check:** `shell/harness/bench.py` runs on a synthetic profile (300
  notes, so it needs no user data). It measured 108 MB idle, 161 MB after 40
  cards and 190 MB after 240, against budgets of 400 MB total and 150 MB
  growth. `--compare-stock` reruns the spike's stock-Anki side.

### Phase 1 (2026-11-06)

Everything in phase 1 works on a copy of the real profile (29,857 cards).
What remains is the exit criterion: a week of real reviews in the shell.

- **Main window:** one webview with three states, as `moveToState` has:
  deck browser, overview and reviewer (`shell/src/main_window.rs`).
  - The deck browser and overview are new SvelteKit routes
    (`ts/routes/deck-browser`, `ts/routes/deck-overview`) over existing
    backend calls, with the shared `ts/routes/toolbar`.
  - The overview shows the stock congrats page when the deck is done.
  - The toolbar has Decks, Stats and Sync; Add and Browse come with
    phases 3 and 4.
- **Page access:** the webview's kind is switched before it navigates.
  - `Main` pages get a 20-method list (`MAIN_PAGE_BACKEND`): the deck
    tree, rename/delete/reparent/add deck, queue counts, filtered-deck
    rebuild and empty, unbury, and a few more.
  - The reviewer, in the same webview, gets only the four-endpoint
    whitelist.
  - The kind changes before the webview navigates, so the reviewer's
    document (with card JS) still runs as `Main` for a moment. So each
    main page gets a fresh capability in its URL (`?cap=`), and the bridge
    script adds it to that document's `/_anki/` calls; the leaving document
    never sees it. Those two routes also refuse framing
    (`frame-ancestors 'none'`). `Referer` was not enough: a page can set any
    same-origin referrer itself (`fetch`'s `referrer`,
    `history.replaceState`).
  - Card JS can call `pycmd`, as in Qt, but Qt never let it undo, bury or
    suspend. So shortcuts count only from real key presses (`isTrusted`),
    sent with a per-process token kept in the key script's closure through
    a `fetch` captured before page scripts run. The home page's phase 0
    commands (re-import, open editor) work only on the home page.
  - Deck descriptions are user HTML, so they render in a sandboxed iframe
    without scripts.
- **Reviewer page:** the stock `reviewer.js` page, with the stock bottom bar
  (`reviewer-bottom.js`) and the toolbar framed into it.
  - The frames keep card CSS away from the bars.
  - Same-origin frames share the page's WebContent process, so there are
    no extra webviews.
- **Reviewer driver:** `shell/src/reviewer.rs` is `reviewer.py` in Rust,
  v3 only.
  - The queue, the answer buttons (with intervals and remaining counts) and
    the answer timer (`show_timer`, capped).
  - The timebox prompt.
  - Auto advance: it waits for audio if set, and stops when the window
    loses focus.
  - Flags, marks, bury/suspend for card or note, delete note, undo/redo, and
    the leech notice.
  - Edit, which opens the phase 0 editor and redraws after; card info,
    previous card info and deck options.
  - Custom scheduling (`mutateNextCardStates` with the per-session key). A
    syntax error in the user's code now still shows the buttons.
  - It returns effects instead of driving windows, so
    `tests/reviewer_flow.rs` checks it against a collection without a
    webview.
- **Card text** (`card_text.rs`):
  - rendering, AV tag extraction and LaTeX;
  - `prepare_card_text_for_display`: IRI encoding and play buttons;
  - the `[[type:]]` filters, with rslib's comparison.
- **Phase 0 bug:** legacy pages had no `<base href>`, so relative media
  (every image on a card) resolved under `/_anki/` and failed to load. Qt's
  `stdHtml` sets `<base href>` to the server root; `std_html` now does too.
- **Shortcuts:** single keys stay in the page, because a menu key equivalent
  would also fire while typing in the editor.
  - A capture-phase listener in every `anki:` frame sends `shortcut:<combo>`
    for the keys the shell lists for the current state, except in text
    fields.
  - Cmd shortcuts are menu items.
  - The defaults are Qt's, plus h/j/k/l for Again/Hard/Good/Easy.
  - The profile import reads Anki's own `answer_keys` and the hjkl add-on's
    settings (this user's are `; l k j`), which replace the h/j/k/l
    defaults. `shortcuts` in the settings file overrides everything.
- **Menus:**
  - Anki;
  - File (Sync, Log out, Re-import);
  - Edit (Undo/Redo named after the operation);
  - Go;
  - Card (checked flags, bury/suspend/mark/delete, edit, info, options,
    replay, auto advance);
  - Window.

  "More" pops up the Card menu. Menu updates are queued on the main thread:
  Tauri's menu setters wait for it, and calling one from a bridge thread
  hung the bridge.
- **Audio:**
  - This collection has no audio files (its media is 826 svg, 728 jpg, 40
    png and 6 gif) and no TTS or type-in templates, so these features were
    checked on synthetic notes.
  - WebKit's `<audio>` plays mp3, m4a/aac, wav, ogg (Vorbis and Opus), flac
    and webm (`canPlayType`). mpv (Anki's `anki_audio`, else Homebrew) is
    only a fallback for a file `<audio>` refuses, with pause and seek over
    its IPC socket.
  - TTS uses the page's `speechSynthesis`, WebKit's binding of the system
    synthesizer, so it needs no Objective-C. Voices are picked by name,
    then by language.
- **LaTeX:** `latex.rs` runs latex plus dvipng or dvisvgm in a temp folder
  per render, as `latex.py` did, and adds the images through the media API.
- **Sync:** the core is `sync.rs`; the UI is in `main_window.rs`.
  - Login dialog, then normal sync and media sync; the toolbar spins until
    the media sync is done.
  - Full-sync prompts: a choice, or a confirmation when one side is empty.
    A backup is made before a download.
  - Sync on open and on close (`autoSync`), and a sync-needed indicator.
  - Redirects (`currentSyncUrl`) and `hostNum` are saved.
  - Guard: a profile copy (any base other than the live `Anki2`) may only
    sync with a loopback server. It is checked with rslib's own URL
    parsing before every network call. One gap is left: rslib follows a
    308 redirect within a sync, so a loopback server that redirects
    elsewhere would be followed for that one sync. rslib's own server
    doesn't redirect.
  - A full sync refuses to start while the editor or deck options are
    open, since they would save into the replaced collection; when quitting
    they're closed (the editor was saved first). Quitting during a sync
    waits for it to finish. Sync on close runs even while a media sync is
    still going.
  - Checked end to end against `anki-sync-server` on 127.0.0.1:
    - login and full upload of the copy (29,861 cards, 1,604 media files);
    - sync on open and on close;
    - full download into an empty profile.
- **Dialogs:** one small window per dialog (`main_window::ask`): login,
  full-sync choices, the timebox, errors. The Svelte pages use in-page
  modals instead of `alert`/`confirm`/`prompt`, and their backend calls
  report errors in the page rather than with `alert`.
- **WebKit checks:**
  - `harness/main-flow.js` reviews a test deck: deck browser, overview,
    reviewer; keys, undo, typed answer, audio, TTS, LaTeX, and the windows
    the reviewer opens.
  - `harness/sync-flow.js` drives the sync UI.
  - With the screen locked every page is hidden, and WebKit throttles
    hidden pages' timers more the longer they stay hidden; a 50 ms poll
    took close to a minute. Test and bench windows now turn that off with
    WKPreferences SPI (`_setHiddenPageDOMTimerThrottlingEnabled:` and two
    related settings).
- **Benchmark** (`harness/bench.py --compare-stock`, release build): both
  apps drive their real reviewer through 240 cards, question then answer
  every 250 ms. The shell's run includes the toolbar and bottom bar. The
  screen was locked, so both apps' pages were hidden.

  | MB (footprint, all processes) | idle | 40 cards | 240 cards | startup |
  | ----------------------------- | ---- | -------- | --------- | ------- |
  | shell, synthetic (300 notes)  | 130  | 187      | 219       | 1.3 s   |
  | stock, synthetic              | 478  | 585      | 755       | 1.8 s   |
  | shell, copy of this profile   | 132  | 219      | 275       | 0.5 s   |
  | stock, copy of this profile   | 449  | 715      | 1134      | 2.1 s   |

  - Memory is 3.4x less on the synthetic profile and 4.1x less on the
    real one. Most of stock's growth is its renderer (625 MB after 240
    cards); the shell's WebContent process reaches 186 MB.
  - Phase 0's bare reviewer page measured 108/161/190 MB, so the bottom
    bar and toolbar frames add about 25 MB.

  | latency, real profile (ms) | host p50/p95 | page p50/p95 |
  | -------------------------- | ------------ | ------------ |
  | shell question             | 0.6 / 1.6    | 5 / 14       |
  | stock question             | 1.7 / 3.0    | 3.7 / 15.6   |
  | shell answer               | 0.1 / 0.3    | 6 / 13       |
  | stock answer               | 0.4 / 0.8    | 4.2 / 13.5   |

  - "host" is rendering and preparing the card in Rust or Python.
  - "page" is from the eval until the card is shown, with images loaded
    and MathJax typeset. It is measured the same way in both apps
    (`harness/page-timing.js`).
  - Rendering is about 3x faster in Rust. WebKit shows a card about 1 ms
    slower than Chromium at the median, with the same tail. Both are well
    under a frame.
- **Not ported:** custom study, creating filtered decks, editing deck
  descriptions, set due date, forget, create copy, recording your own
  voice, dark mode, and the deck browser's import and shared-deck links.
  Add-on hooks on these screens wait for phase 2.

### Phase 2 (2026-11-06)

Chinese/French Frontier runs in the shell's plugin host, and in the old
Anki as before, from the same `addons21` folder. How to turn it on is in
`shell/README.md` ("Add-ons").

- **Process:** `shell/plugin_host/host.py`, started after the profile
  opens for the add-ons in the settings' `plugins` (none by default).
  - It runs on an Anki install's Python (`import_python`, the launcher's
    venv or `~/.anki-stable`) with `-I`. In the app bundle,
    `Resources/plugin_host` holds pylib, the generated `_aqt/hooks.py` and
    the shim, but no `_rsbridge`. A meta-path finder makes `anki`, `aqt`
    and `_aqt` come only from there: an editable install's finder beats
    `sys.path`, and a dev venv's `.pth` pointed at another checkout.
  - The host is a child in its own process group. Stopping it sends
    `profile_will_close` (3 s), then `shutdown`, then kills the group,
    which includes Frontier's model CLIs. The host also ends its group
    when the shell's socket closes, in case the shell died first.
  - A crash restarts it after 1, 4 and 9 s, and more than 3 crashes in
    10 minutes leave it off until Tools > Restart Add-ons. Its output goes
    to `plugin-host.log` next to the settings, shown by Tools > Add-on
    Log.
- **Wire:** one Unix socket, with frames `u32 length | u8 kind | u32 id |
  payload` (`shell/src/plugins/wire.rs`, `pylib/anki/remote.py`).
  - Kinds: CALL (service, method, protobuf), DB (db_command JSON), HOST
    (a JSON request to the shell), EVENT (JSON from the shell), OK, ERR.
    Id 0 wants no reply.
  - The socket sits in a 0700 per-process folder and is unlinked once the
    host connects.
  - Calls run on their own threads, so a slow query doesn't hold up
    events. Host requests run in order on one thread, since a menu's
    items come whole each time.
  - Writes to the host time out after 2 s. A host that stops reading
    (stopped, wedged) is killed and restarted rather than blocking the
    shell (tested with SIGSTOP).
- **pylib:**
  - `RemoteBackend` overrides `_run_command`/`_db_command`, and attaches:
    `open_collection` only checks the path, and `close_collection` does
    nothing.
  - `_backend.py` tolerates a missing `_rsbridge`, keeping the import
    error for a local `RustBackend`, and skips the build hash then.
  - `lang.py`'s globals use the remote backend
    (`use_for_translations`). No other pylib change was needed.
  - The shell refuses add-on calls that would close or replace the
    collection: open/close, full and normal sync, and colpkg
    import/export.
- **OpChanges:**
  - The method table now records where each output's `OpChanges` are
    (from the descriptors in `build.rs`). `Api::raw` passes non-empty ones
    from the shell's own calls (answering, undo, AnkiConnect) to the host.
    There they clear `models._cache` and fire `operation_did_execute`.
  - A plugin `CollectionOp` reports its changes once it's done. The
    reviewer then moves on if its card was suspended, buried or deleted
    (as `Reviewer.op_executed`), and the deck list and overview reload.
  - A full sync holds a gate that plugin calls wait on. Afterwards the
    host gets `sync_did_finish` with `full`, which reloads the scheduler
    and clears caches.
- **`aqt` shim (`shell/plugin_host/aqt`):**
  - `mw` with `col`, `state`, `taskman` (collection tasks one at a time,
    as aqt), `progress` timers, `addonManager` (config in the same
    `meta.json`, web exports), `form.menuTools`, `deckBrowser.refresh`,
    `reviewer.card` and `pm`.
  - `operations.CollectionOp`/`QueryOp`, `utils.tooltip`/`showInfo`,
    `dialogs.open("Browser")` (no browser yet: the search goes on the
    clipboard), and `theme`.
  - `aqt.qt` has only `QAction`, `QMenu`, `QTimer`, signals and
    `qconnect`. Widgets raise a clear error.
  - `gui_hooks` is the generated `_aqt/hooks.py` itself, so every hook
    exists, with aqt's filter semantics (a hook that raises is dropped).
- **Events the shell sends:** `state_did_change`, `operation_did_execute`,
  `reviewer_did_answer_card`, `reviewer_will_show_context_menu` (its items
  join the "More" menu), `deck_browser_will_render_content` (`stats` only,
  under the deck list), `webview_did_receive_js_message` (unknown commands
  from the main window's pages), `sync_did_finish` and
  `profile_will_close`, plus menu clicks and panel messages.
  - Events the shell waits on time out: 0.4 s while drawing a menu or
    page, 1 s for an unknown page command (the page's later commands wait
    behind it), and 5 s for a panel message.
- **UI:**
  - Add-on menu items go at the end of a new Tools menu.
  - A panel is a window showing `/_addons/<module>/<path>`, served only
    where the add-on's `setWebExports` pattern matches. All its bridge
    commands go to the add-on.
- **Frontier:**
  - Its dialogs (settings, fix, feedback, attention, diagnostics, in both
    languages) are now one generic page, `web/form.html`, rendering a
    JSON form spec that Python builds (`panels.py`). In the old Anki it's
    a QDialog with an AnkiWebView; in the shell, a panel. They aren't
    modal any more.
  - The old Chinese feedback dialog retried the wrong key (a loop
    variable shadowed it); now fixed. The rest of the add-on is
    unchanged.
  - Its state is keyed by the collection path. The shell passes the same
    path the old Anki does, so both apps share
    `user_files/<hash>/state.json`.
- **Checks:**
  - `pylib/tests/test_remote.py` (8 tests) and
    `shell/plugin_host/tests/test_host.py` (11 tests: the real host as a
    subprocess against a fake shell, run by `check:pytest:plugin_host`).
  - `shell/tests/plugin_host.rs` (4 tests: the real host on a scratch
    collection, covering the sync gate, denied methods, crash restart and
    a stopped host).
  - Reviewer and bridge tests, and Frontier's 394 tests.
  - The subagent that wrote the panels opened each one in the real Qt
    Anki offscreen on a profile copy, with a round trip and one feedback
    submission.
- **End to end:** `shell/harness/frontier-flow.js` on a copy of the real
  profile (29,857 cards), with the bundled app and its host on
  `~/.anki-stable`'s Python. It checks:
  - the Tools items for both languages, the deck list notice, the
    settings panel and its live status, and the "More" menu's "Fix this
    sentence...";
  - the background job: model calls (writer and checker through the
    claude CLI), and its admission op, which updated a generated note in
    the copy (他买{{c1::八}}个玩具).

  Two runs (with the copy capped at 2 and 3 batches) admitted no new card:
  Frontier's checks rejected every candidate and deferred it. The live
  state has 174 such deferrals too, and the three pending fixes are hard
  ones (飘 against 漂, say). So the admission path was checked another
  way: with `shell/harness/remote_everywhere.py`, all 379 of Frontier's
  collection tests pass with every collection attached over the socket
  protocol (18,880 backend calls), admission and schedule takeover
  included.
- **Review:** an independent Opus pass found no deadlock, data loss or
  security hole. Its findings are fixed: host requests applied out of
  order (a stale Tools list), unbounded writes to a stuck host, orphaned
  model CLIs if the shell dies, colpkg import/export, `_rsbridge` import
  errors losing their cause, a 5 s wait on unknown page commands, a panel
  left blank by a busy host, replies missing from a panel's diffing, the
  host's initial state, and unnormalised collection paths.
- **After merging phase 3:**
  - Media HTML is now sandboxed. Add-on files are served without that
    sandbox, or a panel's page loses its scripts.
  - All the WebKit harnesses pass on a profile copy with the plugin host
    running: main flow, Add Cards, the editor's selection, save, features
    and occlusion checks, and Frontier's UI.
  - Wired since: phase 3's editor hooks, for the agent pane and the
    Alfred bridge (below).

### Phase 3: agent pane and Alfred bridge (2026-11-06)

Both add-ons run unchanged in the plugin host, so the old Anki keeps
running them as before.

- **Which versions:** the user's `editor_agent_pane` (addons21) is the
  agent-queue stack (PR #108 and the ones below it), not main's older
  copy, so that's the one ported; this PR carries the stack until it's
  merged. `alfred_anki_html_bridge` is only in addons21: a localhost HTTP
  server (`/insert`, `/media`, `/health`, token in
  `<base>/alfred-anki-bridge.json`) that pastes into the last field left.
- **Qt widgets, drawn by the shell.** Rewriting the pane's ~2k lines of
  Qt UI as a web page would have meant two UIs to keep in step. Instead
  the shim has the widgets it uses (`aqt/widgets.py`): box and form
  layouts, labels, buttons, tool buttons, combo boxes (editable too),
  line and plain text edits, check boxes, splitters, docks, `AnkiWebView`
  and the folder picker. Python keeps their state and Qt's signal rules
  (a first `addItem` selects it, `clear` signals -1, `blockSignals`). A
  dock added to an editor window is its side panel: a generic page
  (`src/plugins/widgets.js`) asks for the tree, applies ordered batches of
  changes, and sends input back (`addonWidget:`). Typing goes with the next
  message, so a click handler sees it. A `keyPressEvent` override (the
  prompt's Enter, and Up/Down for history) gets those keys first, and the
  page does Qt's default if the base class is called.
  - Other Qt names import but can't be created (the queue window's table
    and date picker); top-level windows and dialogs can't be shown yet.
  - Web views are sandboxed frames (an opaque origin): the transcript is
    model output, and in the editor's webview it could otherwise reach the
    editor's bridge and API, which Qt's own webview couldn't. They talk to
    the panel by messages. Their evals since the last `stdHtml` are
    replayed when the panel is drawn again (a page redraw).
  - The panel loads hidden with the window, so a hidden pane's evals land,
    as in Qt.
- **`aqt.editor`:** an `Editor` per window (`add`, `editor`) with the hooks
  `editor_did_init(_buttons)`, `editor_did_load_note`,
  `editor_did_focus/unfocus_field`, `editor_did_fire_typing_timer`,
  `editor_did_update_tags`, `editor_state_did_change` and
  `add_cards_did_add_note`. The shell sends the note with each event;
  `editor.note` stays one object while the window shows that note. What an
  add-on changes in it goes back after each task on the host's loop (only
  the changed fields), so Add and the next save have it, as with Qt's
  in-memory note. Also `loadNote`, `call_after_note_saved`, `doPaste`
  (filtered as `_pastePreFilter`), `checkValid`, `addButton` with keys
  (Qt's Ctrl is Cmd), and `QApplication.focusWidget`.
  - `web.evalWithCallback` uses WKWebView's `evaluateJavaScript` with a
    completion handler (`src/webkit.rs`): the editor's CSP has no `eval`.
  - The format buttons and the add-ons' buttons are one toolbar group:
    each `AddonButtons` group handles the clicks of every add-on button in
    the page, so with two each click ran twice. The add-ons' are put in the
    format group with their own handlers (a group's `destroy` leaves its
    element, so it can't be redrawn).
- **Review** (an independent Opus pass), fixed:
  - rich-text labels drew any HTML in the panel, which has the editor's
    bridge and API: now only formatting tags and http links;
  - `loadNote` sent the host's whole, older copy of the note, undoing
    typing since: now only what the add-on changed; the reload after
    `editor_did_unfocus_field` waits 100 ms, as Qt;
  - eval callbacks could be dropped (a reset page, a structure change, a
    redrawn panel): now always answered, with null if lost;
  - the host's text replaced typing it hadn't had yet;
  - add-on shortcuts got a second listener after a host restart;
  - web view pages were kept after their window closed;
  - add-on web views are also served with `CSP: sandbox`, and API calls
    from another origin (`null`) are refused;
  - a closed window's `Editor` could act on a reopened one.
  - Host requests can now get replies (the folder picker waits for one).
  - The add-on logger and logs folder are Anki's (`logs/addons/<add-on>`).
- **Checks** (on a copy of the real profile, with both add-ons):
  - `harness/agent-pane.js` in Add and in Edit Current: the shortcut and
    button open and close the pane; the form; Enter sends a request to
    Ollama (`qwen3:30b-a3b`); the transcript shows it with a proposal;
    Apply fills the field; the note is added (Add) or saved (Edit
    Current) with it.
  - `harness/alfred-bridge.js`: the field left is remembered
    (`editor_did_unfocus_field`); `/insert` pastes there and adds the
    source link; a wrong token is refused; `/media` stores the file; the
    added note has it all.
  - `plugin_host/tests/test_widgets.py` (the widgets and editors in
    process) and `tests/plugin_host.rs` (replies).
- **Not ported:**
  - the agent queue window, the pane in the browser (phase 4), the
    toolbar badge;
  - the reviewer's selection in a request's context (the reviewer's page
    isn't the host's);
  - Alfred's paste at the caret: the add-on's caret script uses
    `ShadowRoot.getSelection`, which WebKit lacks, so it pastes at the
    field's end (the add-on itself would need `getComposedRanges`);
  - after a plugin host restart, open editor windows get the add-ons back
    when they're reopened.

### Phase 3 (2026-11-06)

Adding and editing work in the shell on a copy of the real profile, checked
against the collection. What remains: the add-on parts (below), and the
exit criterion, a week of adding and editing only in the shell.

- **Windows:** Add Cards (`add`) and Edit Current (`editor`) host the stock
  `editor.js` page (`shell/src/editor_window.rs`). The note logic is in
  `editor.rs`, whose unit tests run on a real collection without a
  webview.
  - The page mounts the editor at the end of `body`. A CSS grid puts the
    windows' own bars (Qt widgets there) around it: Type and Deck above,
    History, Close and Add below. The editor CSP blocks inline scripts, so
    the bars get their behaviour by eval.
  - Add mode keeps the note in memory until it's added. `key:`/`blur:`
    saves change only the in-memory note, as `Editor.addMode` does.
  - Adding: save the fields (`saveNow`, then the ordered bridge), then
    `note_fields_check`. Problems are a warning dialog; a cloze notetype
    without a cloze asks first. Then `add_note` into the chosen deck. The
    next note keeps the sticky fields and the tags. The history holds the
    last 15; an entry opens Edit Current, since there's no browser yet.
  - Notetype change copies fields as `on_notetype_change` does: same-named
    non-empty fields first, then the rest in order. The deck follows the
    notetype's last-used deck. The choosers are the main window's deck
    picker, made generic (`main_window::pick`).
  - Closing or Escape saves first, and asks before discarding input in Add
    (sticky fields don't count). Quitting waits for every editor to save;
    unsaved Add input asks too, except in test runs.
  - Edit Current comes from the reviewer's `e`, with its card. Cmd+Enter
    closes it, as in Qt.
- **Bridge commands:** everything `onBridgeCmd` and `_links` handle in add
  and edit mode:
  - `toggleSticky*` (saved in the notetype);
  - `toggleMathjax` (redraws the page), `toggleShrinkImages`,
    `toggleCloseHTMLTags`;
  - `lastTextColor`, `lastHighlightColor`, `setTagsCollapsed`, kept in the
    shell's settings (`editor`);
  - `editorState`, `ioImageLoaded`;
  - `dupes`: there's no browser, so a picker lists the duplicates and opens
    one in Edit Current;
  - `cards`/`fields`: a "use Anki" note until phase 4;
  - `preview`: the browser's editor only.

  They are accepted only from the two editor windows: card JS in the main
  window reaches the same bridge.
- **Safety** (from the independent review):
  - Add mode's notes are all id 0, so each note shown gets a generation,
    and the page gets its negative as the note id. A late save, a double
    Add (double click, key repeat), or an Add confirmed after a cloze
    prompt can't land on the next note. A confirmed Add saves and checks
    again first.
  - The 2 s save timeout waits while the editor's bridge is busy. A paste
    downloading images, or a big drop, holds the ordered bridge, and the
    save and the close/add/load behind it would otherwise run early.
  - The tokens `after_saved` waits for name their editor, so card JS in
    the main window can't trigger an editor's close, add or quit.
  - Fields can't frame media documents, which would share the editor's
    origin and bridge. The editor CSP allows frames only from the shell's
    own pages and the web (`frame-src`), and no `object-src`. HTML and SVG
    media are served with `CSP: sandbox`, an opaque origin. A webview gets
    only its own `legacyPageData` pages (`label`, `label-*`). WebKit honours
    all three with the `anki:` scheme (`editor-features.js` checks them).
    This user's notes frame only one web page.
  - Drops: only pictures and audio go into the media folder, as Qt's
    `urlToLink`; other files are named in a message. Files over 100 MB
    aren't read.
  - The card menu's key equivalents (Cmd+1-7 flags, delete) are ignored
    while an editor window has the focus, as Qt's are main-window only.
  - Escape during an input method's composition doesn't close Add.
  - Closing the main window with editors open saves them first, and keeps
    the window if the user keeps the Add input.
  - Edit Current follows changes made elsewhere (an undo, the reviewer's
    mark), and closes if the note is deleted. A save is dropped if the
    note's notetype changed meanwhile.
  - Add reopened with nothing typed starts again from the defaults (the
    reviewed card's deck), as `AddCards.reopen`.
  - `NSMicrophoneUsageDescription` is in `Info.plist`, for recording.
- **Media escaping:** the phase 0 gap is closed. Fields load through
  `encode_iri_paths` and save through `decode_iri_paths`, as
  `escape_media_filenames` does. A file name with a space now shows in the
  editor and is saved unescaped.
- **Paste:** `paste.rs` (`_pastePreFilter` and `_processMime`) and
  `pasteboard.rs` (NSPasteboard via objc2).
  - The page cancels the paste event and sends `paste`, as with Qt. The
    shell reads the pasteboard: HTML, file and web URLs, text, and a
    PNG/TIFF image re-encoded as PNG or JPEG per `PASTE_IMAGES_AS_PNG`.
  - It filters as Qt does (dom_query, already in the tree via wry), then
    calls `pasteHTML`. Remote images are downloaded into the media folder
    (30 s timeouts, Anki's user agent); failures are shown as Qt's
    warnings.
  - Internal pastes: the page sends `cutOrCopy` during the copy event,
    but the bridge is asynchronous, so WebKit may write the pasteboard
    before or after the shell hears of it. So, as Qt does on its clipboard
    signal, the shell captures the pasteboard 200 ms later. A paste is
    internal while the pasteboard still has that change count and HTML.
  - WebKit copies absolute `anki://localhost/` image URLs; they're turned
    back into media names.
  - Shift flips "strip formatting" (`NSEvent.modifierFlags`).
  - Not ported: Safari's web archive flavour, which Qt doesn't read either.
- **Drops:** Tauri's drop handler takes every drop, including drags
  between fields, so editor windows turn it off.
  - The page's capture-phase drop listener sends external drops: files as
    base64 from `dataTransfer`, plus HTML, text and uri-lists. They go
    through the same mime and filter path.
  - Drags inside the page stay WebKit's (Qt's `evt.source()` case).
  - An image dropped on the occlusion picker starts the mask editor.
- **Attach, record, image occlusion:**
  - Attach and "Add image for occlusion" use a modal NSOpenPanel on the
    main thread.
  - Record is a small window using the page's `getUserMedia` and
    `MediaRecorder`, which saves `rec<time>.m4a` (AAC; Qt made mp3). It
    hasn't been tried with a real microphone: the first use asks for
    permission, which a background test can't answer.
  - Image occlusion: add, edit (`setupMaskEditor` on load), from a file,
    from the pasteboard and from a drop. `saveOcclusions` runs before an
    add.
- **LaTeX preview:** `renderLatexPreview` reuses the phase 1 port
  (`latex::render_preview`, as `editor_latex_preview.py`).
  - Renders run off the bridge and answer `receiveLatexPreviewResult`.
  - Results are cached per editor (128 entries).
  - The image goes to the media folder only if a saved field already has
    that LaTeX; otherwise it's a data URL.
- **Formatting buttons:** Mini Format Pack's config had no user changes, so
  all 13 default actions were enabled. The collection uses every one: 732
  notes with `<pre>`, 2,270 with `<ul>`, 15 with `<abbr>`.
  - They're added the add-on's way, as `AddonButtons` on the toolbar, with
    Material Design icons, since the add-on's were PNGs.
  - The hotkeys are the add-on's (Qt's Ctrl is Cmd), matched on
    `KeyboardEvent.code`, because Option changes `key` on macOS.
  - `abbr` asks for its title in a dialog.
- **Hook points for phase 2:**
  - `Editors::listen` receives `EditorEvent`s: note loaded, field
    focused/unfocused (`editor_did_unfocus_field`), typing timer, tags
    updated, state changed, note added. The alfred bridge needs these.
  - `editor_window::set_side_panel(label, html)` shows an add-on's own
    document in a frame beside the fields, for the agent pane. It shares
    the editor's bridge and API.
  - `reload_note_keeping_focus` is the reload after a hook changes the
    note.
- **WebKit checks** (all pass on the profile copy):
  - `harness/editor-add.js`:
    - the bars' layout;
    - both pickers, with text carried over a notetype change;
    - the format buttons and a hotkey;
    - sticky fields and tags;
    - the note and its card's deck in the collection;
    - the next note, the history;
    - the empty-field warning (nothing added);
    - the duplicate mark.
  - `harness/editor-features.js`:
    - paste from a scratch pasteboard (never the user's clipboard):
      external HTML filtered, plain text escaped with runs of spaces kept,
      a field copy pasted back unfiltered;
    - undo and redo;
    - input method composition, sent through the WKWebView's
      `setMarkedText:`/`insertText:` (NSTextInputClient), so WebKit's real
      IME path runs and `compositionend` fires: "にほん" commits as 日本 and
      is saved;
    - the MathJax overlay: it opens, the formula renders in the field (in
      the element's shadow root) and saves as `\(x^2\)`;
    - the LaTeX preview;
    - a format button;
    - an option toggle;
    - the side panel and the frame limits.
  - `harness/editor-occlusion.js`: the Image Occlusion notetype, a
    pasteboard image, the mask editor, and the no-masks warning. Masks
    aren't drawn, because fabric ignores synthetic mouse events.
  - `editor-selection.js`, `editor-save.js` (fixed: it used the home
    page's `openEditor`, which phase 1 limited to the home page) and
    `main-flow.js` still pass.
- **WebKit undo:** WebKit coalesces typing into one undo step until an undo
  or redo. A pause, leaving the field, or moving the caret doesn't end it,
  so one Cmd+Z can take back typing from before a field switch. The fields
  share the document's undo stack. Phase 0 saw the same around wrap and
  surround. This wasn't compared with QtWebEngine.
- **Keystroke latency** (`harness/editor-typing.js` on the 154-MathJax
  note, release build): median 1 ms, p95 3-4 ms, max 15-17 ms, the same as
  main's binary.
  - The test windows were hidden from WebKit during the runs (no animation
    frames, and timers clamped to about 100 ms; probably the display or
    another Space). So this is the keystroke to a forced layout after the
    page's microtasks, not to paint; the harness reports which it
    measured.
  - Measure paint with the window on screen; the spike's paint numbers
    were 12-13 ms.
- **Not ported:**
  - Card layout, Fields, the browser (Show Duplicates and History use
    Edit Current instead);
  - the old Anki's editor geometry;
  - drawing masks in a test;
  - the agent pane and alfred events: done since, below.

### Phase 4: the other screens (2026-11-06)

What the user uses, from the old Anki's `prefs21.db` and a copy of the
collection (read only). Qt saves a window's geometry under a key with the
Qt version, and the daily Anki (25.9.2) runs Qt 6.11, so a 6.11 key means
the window was opened recently:

| Screen                         | Evidence                                               | Plan  |
| ------------------------------ | ------------------------------------------------------ | ----- |
| Card layout                    | `CardLayoutGeometry6.11`                               | port  |
| Filtered deck                  | `dyndeckconfGeometry6.11`; 69 filtered reviews in 2026 | port  |
| Custom study                   | (no geometry key); the filtered reviews                | port  |
| Media check, empty cards       | `checkmediadb…6.11`, `emptycards…6.11`                 | port  |
| Preferences                    | non-default settings                                   | port  |
| Dark mode                      | `theme` 0 (follow system), and macOS is dark           | port  |
| LaTeX toolchain                | "Edit LaTeX toolchain" enabled, custom pipeline        | port  |
| Manage Note Types, its options | `models…6.6`, `modelopts…6.6` (not since)              | defer |
| Fields                         | no trace (the dialog saves no geometry)                | defer |
| Import                         | `import…6.6`; last bulk import in 2017                 | defer |
| Export                         | an `exportDirectory`, nothing recent                   | defer |
| Deck description editing       | every deck description is empty                        | defer |
| Set due date, Forget           | no manual revlog entries, ever                         | defer |
| Create copy, record own voice  | no trace                                               | defer |

Deferred screens stay in the old Anki. The change-notetype and import pages
already exist in `ts/`; hosting them is small when they're wanted.

- **Dark mode** (`src/theme.rs`): the theme preference (system, light,
  dark) starts as the old Anki's `_global.theme`. Night mode is decided
  from it and the system's `AppleInterfaceStyle`, and windows get the
  matching appearance (`set_theme`).
  - Pages get Qt's markers (`night-mode` and `data-bs-theme` on `<html>`,
    `nightMode`/`night_mode` on `<body>`) from the protocol, which adds
    them to every page it serves: the SvelteKit shell and the assembled
    pages alike. A page never loads in the wrong theme and then switches.
  - Open pages, and their same-origin frames (the reviewer's bars), get
    Qt's `on_theme_did_change` script; the reviewer's next cards get the
    body classes. A system change arrives as Tauri's `ThemeChanged`.
- **Preferences** (`src/preferences.rs`, `ts/routes/preferences`): a new
  page, saved as you go. The collection's preferences go through
  `get_preferences`/`set_preferences`, which only this page gets (a
  per-kind list, `WebviewKind::own_backend`). The theme and LaTeX commands
  go through two bridge commands accepted only from this window. Left out:
  language, UI size and style, the Qt-only distractions settings, and the
  sync options, which are imported from the old Anki.
- **LaTeX toolchain:** the user's add-on replaces latex.py's commands with
  pdflatex, pdfcrop (`--gscmd /usr/local/bin/gs`) and `sips` (PNG) or
  pdf2svg (SVG). The shell rendered with latex and dvipng/dvisvgm, so new
  images would have looked different from the old ones.
  - The profile import reads the commands from any enabled add-on that
    assigns `latex.pngCommands`/`svgCommands` at module level. It parses
    the files with `ast` and never runs them.
  - Preferences shows them one per line, shell-quoted; a line with an
    unmatched quote is refused. The shell's own commands win over the
    imported ones, and "Use Default Commands" clears them.
  - Checked with the user's pipeline on the copy: an SVG came out of
    pdflatex, pdfcrop and pdf2svg. The pipeline includes a debugging
    `cp tmp.pdf ~/tmp.pdf`, so every SVG render rewrites that file, as in
    the old Anki.
- **Saving:** each change is saved shortly after typing stops, one save at
  a time. Closing the window or quitting first runs the page's flush
  (2 s at most), since a closing webview runs no more timers. Number
  boxes are clamped as Qt's spin boxes: the backend stores the rollover
  in a byte, so 300 would wrap to 44. A save refreshes the deck list and
  the undo menu, but not the reviewer's card, whose redraw would replay
  its audio. A full sync closes the window, which holds a copy of the
  settings. Each save is an undoable "Update Preferences" step.
- **Review** (two independent Opus passes), fixed: edits lost on close
  and quit, saves landing out of order, an emptied number box failing
  every later save, no refresh after a save, Preferences left open over a
  full sync, re-import not reapplying the commands and theme, the theme
  handler locking on the main thread, odd add-on files failing the whole
  import, and add-on panel pages not themed.
- **Build trap:** after a rebuilt SvelteKit bundle, one chunk on disk came
  from an earlier build (another `__sveltekit_*` global), and every page
  failed to start. Removing `out/sveltekit` and
  `out/qt/_aqt/data/web/sveltekit` and building again fixed it (the build
  doesn't prune old outputs).
- **Checks:** `harness/preferences.js` (`--test-page preferences`, a new
  test mode that opens a screen's window and runs the script there on
  each load). It checks the imported commands, dark mode in the open page
  and in a newly loaded one, a collection preference in the collection,
  the refused line, a render with custom commands, and the reset. Unit
  tests cover the theme markers, the protocol's theming and per-page API,
  the toolchain in `latex.rs`, the command text, and the import
  (`plugin_host/tests/test_import_prefs.py`).
- **Filtered decks, custom study, Check Media, Empty Cards**
  (`src/screens.rs`, `ts/routes/{filtered-deck,custom-study,media-check,empty-cards}`):
  new Svelte pages. Each gets only its own backend calls (`own_backend`);
  what needs the shell (moving the main window on, LaTeX, the media
  folder) goes through bridge commands accepted only from that window.
  - Filtered deck: `FilteredDeckConfigDialog`'s form. Tools > Create
    Filtered Deck… (`f`) builds a new one; the deck options of a filtered
    deck (overview, deck list gear, `o`) open it instead, as
    `onDeckConf`. A search that matches nothing shows the backend's error
    and keeps the dialog. Building shows the deck's overview. Left out:
    the Search and hint buttons, which open the browser.
  - Custom study: all six choices with Qt's ranges. The tag lists
    (`taglimit.py`) are shown under "Study by card state or tag" rather
    than in a second dialog. The overview has the button (`c`) for normal
    decks.
  - Check Media: the report, then Qt's buttons (Delete Unused with a
    confirmation, Tag Missing, Render LaTeX, Empty Trash, Restore Deleted,
    View Files). Render LaTeX is `render_all_latex` in Rust, with the
    toolchain; its first failure is shown as text. The report is run
    again after each action.
  - Empty Cards: the backend's HTML report is parsed into text (its
    notetype and card names are the user's), and Delete keeps a note's
    first card if asked, as `_delete_cards`.
  - A full sync closes all four, as it does the editor and deck options.
- **Review** (an independent Opus pass), fixed:
  - Cancel and Escape did nothing in the filtered deck dialog: a second
    `screenClose` registration replaced the first.
  - Render LaTeX didn't expand clozes, as `media.py` and the media check
    do, so `[$]{{c1::x}}[/$]` images stayed missing while it reported
    "All LaTeX rendered".
  - A second Enter after a successful build or custom study submitted
    again (a second deck, or twice the limit increase).
  - The windows aren't modal as Qt's media check was, so Delete Unused
    checks again first, and Empty Cards deletes only cards a fresh report
    still has.
  - Custom study's limit changes show the deck they changed, and opening
    a screen that's already open focuses it instead of reloading it.
- **Checks:** `harness/filtered-deck.js` (refused search, build, the
  overview, Options reopening it, rebuild with a new limit),
  `custom-study.js` (ranges, cramming into the session deck),
  `media-check.js` (unused and missing files, tagging, rendering with
  scratch commands, delete/restore/empty trash) and `empty-cards.js`, all
  against the collection. A screen's script follows the windows it opens
  (`{next: true, in: label}`), and a test whose last window closes
  without a result now fails (it used to exit 0). Vitest covers the pages'
  logic; `screens.rs` tests `render_all_latex`.
- **Card layout** (`src/card_layout.rs`, `ts/routes/card-layout`): a new
  page, opened by the editor's Cards… once the note's edits are saved
  (`after_saved`), on the editor's note, including Add mode's unsaved one.
  - Front, back and styling are edited as text; the preview follows after
    a 200 ms pause. Card types can be added (a copy of the current one),
    removed (with its card count), renamed, repositioned and flipped;
    adding or removing warns of the full sync. Saving is
    `update_notetype`; then the editor and reviewer redraw.
  - The preview shows card templates, so it's a frame served with `CSP:
    sandbox` and the iframe's `sandbox`: an opaque origin, whose requests
    the protocol refuses and which can't reach into the page, which may
    save the notetype. The shell renders each side as the reviewer does
    (`render_uncommitted_card` with the edited template and styling, then
    LaTeX, play buttons, and Qt's sample type box), and the page posts it
    to the frame. MathJax's fonts load there.
  - Left out: Deck Override, Browser Appearance, Restore to Default, Copy
    Info, Add Field (Fields is deferred), the template search box, audio
    in the preview, and the mobile class.
  - Checks: `harness/card-layout.js` on a fresh copy of a stock notetype
    (the preview following edits, sides and night mode; the frame's
    isolation; renaming, adding and removing card types; the discard
    prompt; the saved templates in the collection) and, in Edit Current,
    the Cards button. Unit tests render previews (edited template and
    styling, the sample type box) and check the frame is sandboxed.
