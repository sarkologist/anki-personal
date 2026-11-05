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
- **Repurpose:** `pylib`. It is a thin wrapper over
  `backend.command(service, method, bytes)`, so pointing that one call at a
  socket makes all existing Python collection code work out of process. This is
  what makes the plugin host cheap (see "Add-ons").

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
   through pylib-over-IPC, and UI comes from a small web contribution API. Python
   add-ons that patch Qt internals (`aqt.*` monkeypatching) are not supported,
   and are replaced case by case.

## Add-ons

| Add-on (enabled)             | Plan                                                                                                                                                     |
| ---------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| AnkiConnect                  | Native: a Rust HTTP server on :8765 implementing the subset actually used (Claude workflows, Alfred). Phase 0, so automations keep working from day one. |
| chinese_frontier (11.5k py)  | Plugin host. Its data logic (239 `col.*` calls) runs as-is over pylib-IPC; its Qt dialogs (about 80 widget uses) become web panels. Phase 2.             |
| editor_agent_pane (13.6k py) | Plugin host. The agent processes stay in Python; the pane becomes a web side panel in the editor window. Phase 3.                                        |
| suspended_card_priority      | Needs a "custom browser column" plugin API (`browser_did_fetch_row`-style). Phase 4.                                                                     |
| alfred_anki_html_bridge      | Editor hooks (`editor_did_unfocus_field`, typing timer) are forwarded as plugin events. Phase 3.                                                         |
| hjkl answer keys             | Native configurable reviewer shortcuts. Phase 1.                                                                                                         |
| Edit LaTeX toolchain         | Native setting for the LaTeX command. Phase 4.                                                                                                           |
| Mini Format Pack             | Pick the buttons actually used and add them to the editor toolbar. Phase 3.                                                                              |
| Image Occlusion Enhanced     | Retire: built-in IO (`ts/routes/image-occlusion`) replaces it.                                                                                           |

### Plugin host

- **Process:** a `python` process started by the shell. It runs with this
  repo's `pylib` and a `RemoteBackend`, which implements `_run_command` and
  `db_command` over a Unix socket using length-prefixed (service, method, bytes)
  frames. The shell owns the only `Backend`; plugin calls are serialized through
  it like page calls.
- **API (`anki_plugin`):**
  - events: profile open/close, `operation_did_execute`, editor
    load/unfocus/typing, reviewer show question/answer, browser row fetch;
  - contributions: menu items, toolbar/editor buttons, side panels and dialogs.
    A contribution is a web page served by the plugin, or a small JSON form
    spec;
  - `taskman`-style background work.
- **Lifecycle:** restarted on crash, and its stdout/stderr go to a log the shell
  shows. A plugin crash never takes the reviewer down.

## Bridge and screen inventory

Bridge commands the existing pages send. About 40 in total, mostly editor:
`key:/blur:/focus:` field saves, `paste`, `cutOrCopy`, `attach`, `record`,
`saveTags`, `setTagsCollapsed`, `toggleSticky*`, `toggleMathjax`,
`toggleShrinkImages`, `toggleCloseHTMLTags`, `lastTextColor`,
`lastHighlightColor`, `dupes`, `cards`, `fields`, `preview`, `editorState`,
`addImageForOcclusion*`, `browserSearch`, `ans`, `updateToolbar`, deck browser
clicks. `qt/aqt/editor.py`'s `onBridgeCmd` and paste/media handling
(about 2.1k lines) are the reference.

| Screen                                                                                                  | Today                                                         | Work                                                                                                                           |
| ------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ |
| Reviewer                                                                                                | `reviewer.js` + Python driver                                 | Driver in Rust (proven in the spike), answer bar, shortcuts, undo, flags/marks, bury/suspend, audio, timers, auto-advance      |
| Deck list, overview, top toolbar                                                                        | Python-generated HTML                                         | New Svelte pages over existing backend calls (`deck_tree`, `congrats_info`, ...)                                               |
| Editor (Add / Edit Current)                                                                             | Svelte + `editor.py` bridge                                   | Bridge in Rust, plus the WebKit selection shim (below)                                                                         |
| Card browser                                                                                            | Qt model/view table, sidebar tree (about 3.6k lines)          | New Svelte virtualized table over `browser_row_for_id`, sidebar over `sidebar` tree calls, find/replace, duplicates, previewer |
| Card info, deck options, graphs, change notetype, import, image occlusion                               | Svelte routes                                                 | Host them, plus their bridge handlers (`deck_options_ready`, `import_done`, ...)                                               |
| Card layout, notetypes, fields                                                                          | Qt (`clayout.py`, `models.py`, `fields.py`, about 1.6k lines) | New Svelte pages                                                                                                               |
| Filtered deck, custom study, preferences, profiles, sync login, export, media check, empty cards, about | Qt dialogs                                                    | New Svelte pages (mostly forms)                                                                                                |
| Debug console, add-on manager                                                                           | Qt                                                            | Drop; replaced by the plugin host log/settings                                                                                 |

## Known engine gaps (WebKit)

- **Shadow-DOM selection.** `ts/lib/tslib/cross-browser.ts#getSelection` uses
  `ShadowRoot.getSelection()`, which only Chromium has. In WebKit,
  `saveSelection` returns null and `restoreSelection` throws. Fix with a shim
  based on `Selection.getComposedRanges({shadowRoots})` behind the same helpers
  (40 call sites), under red/green vitest tests. Then exercise formatting, cloze
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

- [ ] `shell/` Tauri 2 crate in the workspace; `./ninja shell` builds it
      against `out/` web assets; app bundle.
- [ ] Backend in-process: open profile/collection, lock detection, close/backup
      on quit (`rslib` backups).
- [ ] Protocol handler (`/_anki/*`, media, pages) and a generic bridge
      dispatcher.
- [ ] Settings: own JSON, plus a one-time import of the profile list and sync
      key from `prefs21.db` (Python pickles: `serde-pickle`).
- [ ] Native AnkiConnect subset; check it against the actions existing
      automations call (Claude workflows, Alfred).
- [ ] WebKit selection shim with tests, so the editor works with selection in
      WebKit.
- [ ] Leak/perf harness: the spike's `bench.py` as a CI-runnable check.

Exit: the shell opens your profile, the AnkiConnect smoke test passes, and the
editor selection tests pass in WebKit.

### Phase 1: review-only daily driver

- [ ] Deck list, overview, congrats, top toolbar (Svelte).
- [ ] Reviewer: question/answer, answer bar, configurable shortcuts (replaces
      hjkl), undo/redo, flags, marks, bury/suspend, auto-advance, timers.
- [ ] Audio via HTML5 `<audio>` in the page; TTS via `AVSpeechSynthesizer`.
- [ ] Sync: login, normal and media sync, full-sync prompts, sync on open and
      close.
- [ ] Native menus.

Exit: a week of real reviews only in the shell; memory checked with the
`memtrack` sampler. **Decision gate:** if memory or latency isn't still clearly
better under real use, stop here.

### Phase 2: plugin host and Frontier

- [ ] Sidecar process, `RemoteBackend`, events, contributions, logging,
      restart.
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

| Phase | Size | Main cost                                        |
| ----- | ---- | ------------------------------------------------ |
| 0     | M    | Selection shim + test coverage; settings import  |
| 1     | M    | Deck list/overview pages; sync UX                |
| 2     | M    | Plugin API design; Frontier's Qt UI to web       |
| 3     | L    | Editor bridge + paste/media; agent pane UI       |
| 4     | L    | Browser table + sidebar; many small form screens |

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
