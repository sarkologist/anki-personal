# Native-shell spike

Question: would replacing the Qt + QtWebEngine + Python shell with a native Rust
shell on the OS WebView (WKWebView via wry/tao) make Anki substantially lighter
and faster, enough to justify losing Python add-ons and upstream shell merges?

Keep: `rslib` (via `Backend::run_service_method`) and the existing built web
assets in `out/qt/_aqt/data/web`. Replace: Python glue, Qt widgets, Chromium.

## Shape

- `anki://localhost/` custom protocol serves:
  - `POST /_anki/<camelMethod>`: the same protobuf endpoints mediasrv exposes,
    dispatched with `methods.json` (snake name → service/method index, generated
    from `out/pylib/anki/_backend_generated.py`).
  - `/_anki/...`: bundled web assets; other paths: media files (read-only).
  - `/`: a page assembled by the shell (what `AnkiWebView.stdHtml` does).
- The shell drives pages by evaluating JS, as Python does today.
- Always run against a copy of the collection; media is only read.

## Phases

1. [x] Reviewer: render real cards (`render_existing_card`) into the stock
   `reviewer.js` page, cycling question → answer → next.
2. [x] Editor: load the stock `editor.js` page and a real note (the same JS
   calls `Editor.loadNote` makes), with a stub bridge.
3. [x] Measure against a second stock Anki instance on the same collection copy,
   same window size, same display:
   - total memory footprint across all processes (shell + WebKit XPC
     processes vs Anki + QtWebEngineProcess), after load and after cycling
     N cards;
   - startup to first rendered page;
   - editor keystroke-to-paint latency on a large note.
4. [x] Note WebKit incompatibilities hit along the way (editor, MathJax, cards).

## Decision rule

Proceed only if the shell is clearly (>= 2x) better on memory or latency;
otherwise prefer targeted fixes in the current stack.

## Results (2026-11-05)

Setup: M-series Mac, both windows 1600x1000 pt at the same position on a 5K
display (2x), copy of the real collection (29,857 cards), stock = this fork's
`main` as installed wheels, second instance with no add-ons except the bench
controller. `harness/bench.py` reproduces the memory runs.

Memory, total footprint across all of each app's processes (two runs each):

| Checkpoint      | Spike (wry/WebKit) | Stock (Qt/Chromium/Python) |
| --------------- | ------------------ | -------------------------- |
| idle            | 135-219 MB         | 893-990 MB                 |
| after 40 cards  | 205-212 MB         | 948-1176 MB                |
| after 240 cards | 233-252 MB         | 1286-1506 MB               |

- Stock's main process alone is 670-900 MB (in-process GPU compositing, Qt
  backing store, Chromium browser heap, Python); its reviewer renderer grows
  223 -> 679 MB over 200 more cards, where WebKit's grows 147 -> 185 MB running
  the same `reviewer.js`.
- Startup to collection open + first page: spike 0.6-1.5 s, stock 1.4-1.6 s
  (to deck browser). Not a differentiator on a warm machine.

Editor, large note (154 MathJax, 9 fields), `bench-typing.js` keystroke→paint:

- Spike: median 12-13 ms, p95 13-14 ms, stable across runs.
- Stock: median 17-19 ms with p95 150-380 ms stalls; a third run degraded to a
  336 ms median. Caveats: stock window was a background window (possible
  Chromium throttling), and WebKit skips selection bookkeeping (below).
- Rendering parity: identical structure in both (`probe-editor.js`: 9 fields,
  same text lengths, 179/179 MathJax rendered).

WebKit incompatibilities found:

- `ShadowRoot.getSelection()` is Chromium-only. `tslib/cross-browser.ts`
  falls back to `document.getSelection()`, which can't see into the fields'
  shadow roots, so `saveSelection` returns null and `restoreSelection` throws
  (`location.anchor`). 40 call sites go through that helper; a port needs a
  `getComposedRanges`-based shim and testing of every selection-driven editor
  feature (formatting, cloze, overlays, undo).
- Nothing else surfaced in reviewer/editor load; not yet exercised: deck
  options, graphs, import, image occlusion, audio, the card browser table.

## Verdict

Memory clears the bar easily (~5x less), typing is at least not worse and
probably better. The cost side is unchanged: Python add-ons (AnkiConnect,
Frontier, agent pane, ~15 AnkiWeb add-ons), the Qt-only screens (browser
table, main window, sync/profiles/prefs), and the editor's shadow-DOM
selection layer.
