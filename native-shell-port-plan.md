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

- [ ] Deck list, overview, congrats, top toolbar (Svelte).
- [ ] Reviewer: question/answer, answer bar, configurable shortcuts (replaces
      hjkl), undo/redo, flags, marks, bury/suspend, auto-advance, timers,
      typed answers, card text preparation, the scheduling-state endpoints.
- [ ] LaTeX image generation ported to Rust.
- [ ] Audio via HTML5 `<audio>` in the page (mpv fallback if formats need
      it); TTS via `AVSpeechSynthesizer`.
- [ ] Sync: login, normal and media sync, full-sync prompts, sync on open and
      close.
- [ ] Native menus.

Exit: a week of real reviews only in the shell; memory checked with the
`memtrack` sampler. **Decision gate:** if memory or latency isn't still clearly
better under real use, stop here.

### Phase 2: plugin host and Frontier

- [ ] Sidecar process, `RemoteBackend` (attach, no open/close, no
      buildhash), `OpChanges` forwarding both ways, `aqt` shim, events,
      contributions, `/_addons/` assets, logging, restart.
- [ ] chinese_frontier running unchanged except its UI, which moves to web
      panels.

### Phase 3: editing

- [ ] Add Cards and Edit Current windows; every editor bridge command; paste,
      media import and recording; LaTeX preview; MathJax; image occlusion
      entry points.
- [ ] Notetype/deck/tag choosers.
- [ ] editor_agent_pane as a web side panel; alfred bridge events; formatting
      buttons.

Exit: a week of adding and editing only in the shell.

### Phase 4: browser and the rest

- [ ] Card browser: table, sidebar, search, column config including the plugin
      column API (suspended_card_priority), find/replace, duplicates,
      previewer, card info.
- [ ] Card layout, notetypes, fields, filtered decks, custom study,
      preferences, import/export, media check, empty cards, LaTeX toolchain
      setting.

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
