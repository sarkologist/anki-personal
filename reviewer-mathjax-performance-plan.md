# Reviewer sluggishness with many MathJax expressions — plan

**Status:** done — render cache implemented, profiled ~2× faster steady-state
flips, kept.

Companion to [editor-performance-improvement-plan.md](editor-performance-improvement-plan.md),
which covered the editor. This covers the review/preview surfaces.

## Problem

Card flips are slow when cards contain many MathJax expressions. Every flip
(`_updateQA` in [ts/reviewer/index.ts](ts/reviewer/index.ts)) replaces
`#qa`'s HTML, calls `MathJax.typesetClear()`, and re-typesets the whole card
from scratch. Two structural sources of waste:

- the answer usually embeds `{{FrontSide}}`, so the math just rendered for the
  question is re-typeset immediately on the answer flip;
- consecutive cards of the same deck share most expressions (same symbols and
  formulas over and over), and none of that reuse is exploited.

## Methodology

Same as the editor plan: **profile before & after; discard changes without
measurable improvement.**

### Profiling harness

Real-browser (headless Chrome) harness replicating the reviewer flip flow with
the same vendored MathJax 3.2.2 `tex-chtml-full.js` and the same config as
[ts/mathjax/index.ts](ts/mathjax/index.ts): set `#qa` innerHTML →
`replaceEditorMathjaxElements` → `typesetClear()` + `typesetPromise([qa])`.
Content: [ts/editor/perf-fixtures/large-cloze-note.html](ts/editor/perf-fixtures/large-cloze-note.html)
(118 expressions, 82 distinct), scaled ×1/×2/×4; answer = question + question
(as `{{FrontSide}}` templates produce). Harness generator lives in the session
scratchpad (`mjbench/gen.mjs`); metric = time of the typeset stage per flip.

### Baseline (Chrome headless, M-series; QtWebEngine will be slower)

| scale | expressions Q / A | Q flip typeset | A flip typeset |
| ----- | ----------------- | -------------- | -------------- |
| ×1    | 118 / 236         | 27–63 ms       | 54–61 ms       |
| ×2    | 236 / 472         | 45–52 ms       | 97–107 ms      |
| ×4    | 472 / 944         | 97–104 ms      | 188–200 ms     |

Cost is linear at ~0.2 ms/expression; the DOM-replacement stage is ~1–3 ms,
i.e. **typesetting is the whole cost**.

## Fix: per-expression render cache via `renderActions`

Mirror of the editor's F7 cache, but for the CHTML reviewer pipeline. A
`renderActions` pair in the MathJax config:

- **lookup** (early, before the expensive stages): key =
  `(display, parent font-size/family, tex source)`; on hit, set
  `typesetRoot` to a clone of the cached node and advance the item state so
  the metrics/typeset stages skip it; the standard insert stage then places
  the clone.
- **store** (after insert): clone the freshly typeset root into an LRU cache.

Safety considerations:

- Sources containing `\label`/`\ref`/`\eqref`/`\tag` are never cached — they
  carry cross-expression document state. (Matched with a TeX control-word
  boundary `(?![a-zA-Z])`, not `\b`: `\ref1` is `\ref` applied to `1`.)
- TeX macro state: `\newcommand`/`\def`/... execute at compile time, and
  compile always runs (even for hits), so macro state evolves identically
  with the cache. Renders are keyed by a rolling signature of the
  definition-bearing sources compiled so far (in document order, consecutive
  repeats folded), so a macro redefinition — e.g. switching to a deck that
  defines the same symbol differently — invalidates reuse, while the common
  "same macro block on every card of a template" pattern keeps full hits.
- Renders are stored _before_ the menu stage: storing after it would bake
  `CtxtMenu_Attached_*` markers into clones, and MathJax's
  `MenuStore.insertElement` skips elements that already carry them — cached
  math would silently lose its contextual menu.
- The MathJax menu settings (scale, renderer, assistive toggles) are
  fingerprinted into the key each render pass, so settings changes
  invalidate cleanly.
- The CHTML font-data cache accumulates for the lifetime of the page (Anki
  never calls `output.clearCache()`), so styles for previously rendered
  characters remain valid for clones. `typesetClear()` only clears the math
  item list, not the font data.
- Cache key includes the parent's computed font-size/family, so the same
  expression in an `<h1>` vs body text renders at the right scale.
- Colors are inherited via `currentColor`, so theme changes don't invalidate.

## Results (same harness/fixture as baseline)

| scale | expressions Q / A | Q flip typeset        | A flip typeset         |
| ----- | ----------------- | --------------------- | ---------------------- |
| ×1    | 118 / 236         | 27–63 → **13–14 ms**  | 54–61 → **27–31 ms**   |
| ×2    | 236 / 472         | 45–52 → **26–27 ms**  | 97–107 → **53–60 ms**  |
| ×4    | 472 / 944         | 97–104 → **47–51 ms** | 188–207 → **93–95 ms** |

Steady-state flips ~2× faster (first-ever render of an expression is
unchanged, as expected). Verified in the browser harness: answer HTML is
byte-identical across a fresh-typeset flip and a fully cached flip, and
TeX-error counts/DOM locations exactly match the no-cache baseline.

The residual cost is the compile (TeX parse) stage plus DOM insertion.
Compile is kept deliberately: later pipeline stages need `math.root` — the
contextual-menu attach reads it eagerly and crashes without it (verified: a
skip-compile variant threw `Cannot read properties of null (reading 'kind')`
from the menu handler).

### Pitfall found during verification

Advancing restored items only past the typeset state (150) made the
assistive-mml stage (state 153) attach a _second_ `<mjx-assistive-mml>` to
every cache hit — the stored clone already contains one. Restored items are
therefore advanced to state 155: past typeset and assistive-mml, but below
the menu stage (170, must re-run — event listeners don't survive
`cloneNode`) and the insert stage (200).

## Working log

- `2026-07-06` — Plan drafted. Baseline profiled with the headless-Chrome
  harness (table above). Confirmed reviewer re-typesets everything on every
  flip and that cost is linear in expression count.
- `2026-07-06` — Implemented `ts/mathjax/render-cache.ts` + wired into the
  config in `ts/mathjax/index.ts` via `options.renderActions`; vitest suite
  `ts/mathjax/render-cache.test.ts`. Found and fixed the duplicate
  assistive-MathML pitfall above by re-profiling with error-count and
  HTML-equality checks in the harness. Final numbers in the table.
  Decision: **keep.**
- `2026-07-07` — Codex review found four issues, all fixed and covered by
  tests: stale renders after `\newcommand` redefinition (now: TeX-state
  signature in the key), cached clones losing the contextual menu (now:
  store before the menu stage; verified via `ctxtmenu_counter` attach counts
  in the harness), menu settings changes not invalidating (now: settings
  fingerprint in the key), and `\ref1`-style sources slipping past the `\b`
  boundary (now: `(?![a-zA-Z])`). Re-profiled: speedup unchanged (x4 answer
  flip 187–191 → 96–99 ms), normalized answer HTML identical across flips,
  error counts equal to baseline, menu attached to all containers.
