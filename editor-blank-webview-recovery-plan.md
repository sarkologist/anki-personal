# Editor webview blanks in Add window → data loss — recovery plan

## Problem

Intermittently, the **Add** window's editor webview goes **completely blank/white**
(toolbar + fields + tags all gone) while the native Qt chrome above it (Type notetype
chooser, Deck chooser) stays visible. Latest occurrence: the main field was populated,
the user could not recover it, and discarding on close **lost the data**.

New evidence (this session): **minimize + resize did NOT repaint it** — it stayed blank.

## Environment

- macOS (Darwin 25.2), Qt6. Default video driver mac+Qt6 = **Metal**
  (`qt/aqt/profiles.py:76-77`, `all_for_platform()` puts Metal first). GPU compositing on.

## Investigation findings (settled)

### What is blank

- Type/Deck rows are **Qt widgets** (`addcards.py:96-108`, `modelArea`/`deckArea`).
- Everything below is one **QtWebEngine webview** (`fieldsArea` → `editor.web`). Blank
  region == the editor webview.

### Two recovery paths exist; the Add window is missing the relevant one

- **Render-process crash (JS dead):** `EditorWebView.__init__` connects
  `page().renderProcessTerminated` → `_on_render_process_terminated` →
  `recover_webview_after_crash()` (`editor.py:1697-1719`, `873-879`). It calls
  `setupWeb()` + `loadNote(focusTo)`, reloading fields from `self.note`. This IS global
  (Add benefits). But it only fires on true render-process termination.
- **Compositor blank (JS alive, surface not painting):** `browser.py`
  `_refresh_editor_web_view_surface()` does `web.hide(); web.show(); web.update();
  web.repaint()` on resize / splitter-orientation / row-change. Added in commit
  `5159e81eb` "Recover browser editor webview when it blanks". **Browser-only — Add has
  no equivalent.**

### Data-loss mechanism

- Field edits flush JS→Python `self.note.fields[ord]` on a **600 ms debounce**
  (`NoteEditor.svelte:419-436`, `updateField` → ChangeTimer → `key:` bridge →
  `editor.py:468-483`). In ADD mode content lives only in `self.note.fields` in memory
  (note id 0, not persisted).
- On close: `closeEvent` → `ifCanClose` → `call_after_note_saved(afterSave)`; `afterSave`
  checks `fieldsAreBlank()` which reads `self.note.fields` (`editor.py:932-945`). If
  non-empty → "Discard current input?" prompt (`addcards.py:350-387`). Facing a blank
  editor, the user hit Discard → text that was still in `self.note.fields` was thrown away.
- `saveOnPageHide()` flushes on `visibilitychange==hidden` (`NoteEditor.svelte:542-548`,
  `861-862`) — a partial safety net (minimize/close), not a general one.

### Why "minimize/resize didn't help" matters

A pure compositor-surface-not-painting bug (JS alive) usually repaints on window
restore. It didn't. That argues **against** a simple compositor blank and **toward** one
of: (a) render-process crash whose reloaded page also failed to composite; (b) **GPU
process crash** (does NOT emit `renderProcessTerminated`, blanks the Metal surface);
(c) JS/Svelte exception unmounting the tree; (d) webview navigated/reloaded to blank.

## Root-cause ranking (Codex consult, gpt-5.6, read-only)

1. **GPU/compositor/Metal surface pipeline loss — most likely.** Chromium's final image
   is imported into Qt's pipeline via GPU interop; that import can fail while ordinary Qt
   widgets stay healthy, leaving a white webview with a perfectly healthy DOM/JS. Plain
   minimize does not run the Browser's explicit child `web.hide(); web.show()` +
   `update()/repaint()`, so "minimize didn't help" does NOT rule this out.
2. **Failed navigation / reload / editor bootstrap** → genuinely white body (editor page
   starts empty and mounts Svelte async after i18n; `base.ts:58`). Generic `loadFinished`
   handler doesn't inspect success/URL (`webview.py:386`).
3. **Renderer crash + failed recovery** — would emit `renderProcessTerminated`
   (`editor.py:1710`); check stderr for `editor webview render process terminated`.
4. **JS/Svelte exception unmounting everything — least likely** (whole tree lives under
   `.note-editor`; handler exceptions leave mounted DOM in place).

Key incident conclusion: the user **saw/clicked the Discard prompt**, which only appears
when `fieldsAreBlank()` is False → **the text was in `self.note.fields`** (mirror worked)
and was lost at Discard, not at the source. Agent pane is host-safe (updates note.fields
before `applyAgentProposal`, addon `runtime.py:2166`) — not a loss vector.

Codex-flagged latent issues (deferred, see follow-ups):

- **Save-ack race**: `call_after_note_saved` treats the `runJavaScript` callback as
  "saved", but JS `saveNow()` only _fires_ async bridge commands (`NoteEditor.svelte:438`)
  over the async QWebChannel — the eval callback can precede the Python field write; and
  the 5 s timeout is silently converted to success (`editor.py:861`).
- **Overwrite risk**: `recover_webview_after_crash` immediately `setupWeb()`+reload from
  `self.note` (`editor.py:873`) — safe after a confirmed dead renderer, data-destroying
  after a false-positive health check.

## Fix approach — staged

### PR 1 (this change) — Python-only; directly targets the incident

1. **Centralize surface refresh**: move Browser's `_refresh_editor_web_view_surface`
   (hide/show + `update()/repaint()`) into `Editor.refresh_web_view_surface()`; Browser
   delegates. Both windows share one implementation.
2. **Add-window triggers**: `AddCards.changeEvent` (WindowActivate / un-minimize) →
   `editor.refresh_web_view_surface()`; the Browser already does this on focus change
   (`browser.py:216`), Add had nothing.
3. **Guard `recover_webview_after_crash`**: reentrancy flag + rate-limit (stop after N
   reloads / window) to prevent crash loops; keep reload-from-`self.note` for the genuine
   `renderProcessTerminated` path (JS is dead there — nothing newer to lose).
4. **Fail-safe close**: force a surface refresh at the start of the close flow so a
   compositor-blanked editor repaints _before_ the Discard prompt — the user never
   decides to discard over a white editor.

### Follow-ups (separate PRs, documented not built)

- Real save-acknowledgement protocol (JS returns/› tokenizes an applied `{fields,tags}`
  snapshot; `call_after_note_saved` reports SUCCESS/TIMEOUT/FAILED, no silent
  timeout→success).
- Eager host-draft mirror in Add mode (~100 ms, or per-input) so a renderer-crash reload
  loses ~nothing — perf-tested (large MathJax fields).
- JS heartbeat watchdog (page-generation probe, 2-consecutive-fail before any destructive
  action, snapshot-JS-before-reload) + load/URL instrumentation.
- Emergency close dialog listing retained fields, Keep-Open default, copy/export before
  explicit discard.
- Diagnostics: `QTWEBENGINE_CHROMIUM_FLAGS=--enable-logging` +
  `qt.webenginecontext=true`/`qt.webengine.compositor=true` to capture the next blank.

## Testing plan

- Red/green (Python, pytest): `recover_webview_after_crash` reentrancy/rate-limit guard;
  refresh delegation; close flow forces a refresh before the discard prompt.
- Manual: with the perf harness (2nd instance + CDP), kill the render process and confirm
  Add recovers; simulate compositor blank and confirm activation repaints.

## Workflow

- Branch + PR (never main). Codex-review the diff before PR. Any perf-relevant change
  (eager mirror / heartbeat interval) profiled before/after.

## Status

- [x] Investigation
- [x] Codex consult (gpt-5.6, read-only) — root-cause ranking + fix guidance incorporated
- [x] Finalize fix design (PR 1 scope above)
- [x] Implement (editor.py, addcards.py, browser.py)
- [x] Tests (red/green) — qt/tests/test_editor_webview_recovery.py, 9 pass
- [x] dmypy clean (202 files); ruff format+lint clean; full qt/tests 221 pass
- [~] `./check` — relevant gates green; fails only on a PRE-EXISTING env issue:
  `build:minilints` globs 33,801 files from nested `.claude/worktrees/*` (gitignored
  but filesystem-globbed) and an ephemeral SvelteKit synthetic type file in the
  `fix-mathjax-copy-paste-block` worktree was deleted → `out/build.ninja:902` missing
  input. Unrelated to this change. Fix separately: exclude `.claude/worktrees` from the
  minilints input glob, or remove the nested worktrees.
- [x] Codex review diff — 2 findings (retry-wedge High, dialog/repaint race Medium) fixed
      + covered by tests
- [x] PR — sarkologist/anki-personal#38

## Related: first editor load blanked the last field on the next flush (2026-10-22)

Found while live-checking the agent pane: opening Edit Current from the reviewer and
closing it without typing wrote `""` into Back. It was not webview-specific (repro'd
with a visible window, warm or cold page) and not the save-ack race above. The cause:
`setFields` creates and subscribes the field stores before suppressing saves, so
Svelte's immediate `""` emission queued `key:<last field>:""` on the shared
`fieldSave` timer. `fieldSave.clear()` then cancelled only the timeout and kept the
action, and the next `saveNow`/field blur/agent apply ran it, reading the now-loaded
note id at fire time. Fixed by making `ChangeTimer.clear()` discard the action and
binding each queued save to the note id current when it was queued. Test:
`ts/editable/change-timer.test.ts`. Live check on the unfixed build: closing Edit
Current after 0.3 s, 1 s or 3 s, or just minimizing it (`saveOnPageHide`), each
sent `key:1:<nid>:` and emptied Back; Browser row switching did not. All pass on the
fix (sarkologist/anki-personal#102).
