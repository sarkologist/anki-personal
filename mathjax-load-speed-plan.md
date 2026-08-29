# Plan: Optimise MathJax load speed

Branch: `claude/codex-sol-mathjax-speed-5982d8` (worktree `.claude/worktrees/codex-sol-mathjax-speed-5982d8`).
Orchestration: Claude coordinates; codex (`gpt-5.6-sol`, reasoning=high) agents do analysis/implementation.
PRs go to fork: `gh --repo sarkologist/anki-personal`, base `main`.

## Known facts (verified)

- Reviewer (`qt/aqt/reviewer.py:349`), clayout (`qt/aqt/clayout.py:365`), previewer
  (`qt/aqt/browser/previewer.py`) load `js/mathjax.js` (config bundle from `ts/mathjax/index.ts`)
  - `js/vendor/mathjax/tex-chtml-full.js` (1.3MB, MathJax 3 full CHTML build) unconditionally
    into the webview, whether or not any card uses math.
- `ts/mathjax/index.ts` config: loader lazily loads `[tex]/noerrors,mathtools,html` from
  `/_anki/js/vendor/mathjax`; `startup.typeset=false`; custom renderActions from
  `ts/mathjax/render-cache.ts` (cross-card-flip render cache, already merged).
- Editor: `ts/editable/mathjax.ts` does `import "mathjax/es5/tex-svg-full"` (2.2MB pre-esbuild)
  — statically bundled into editor JS; per-expression SVG conversion cached in
  `ts/editable/mathjax-cache.ts`.
- Vendored file list: `build/configure/src/web.rs` `MATHJAX_FILES` (includes a11y modules,
  woff-v2 CHTML fonts, tex extension `html.js`).
- `package.json`: mathjax `^3.1.2`. (A v4 bump exists as separate PR #4 — out of scope here;
  keep changes compatible in spirit.)
- This worktree is unbuilt (no `out/`, no `node_modules`). Main checkout
  `/Users/sark/code/anki` is fully built — use it read-only for baseline artifacts.

## Phases

### Phase 1 — parallel read-only analysis (codex ×3) [in progress]

- **A load-path audit**: every consumer of MathJax across webviews; byte/parse cost; what
  full-vs-lazy build buys; whether editor's static tex-svg-full import can be lazy/split.
- **B runtime-startup audit**: what blocks first paint/first typeset in the reviewer webview;
  conditional loading (only when content has math); interplay with render-cache.
- **C measurement harness**: reproducible benchmark of MathJax load+first-typeset in a real
  Chromium context, runnable against baseline (main checkout artifacts) and this worktree's
  built artifacts. Findings → `bench/` harness files + instructions.

Findings land in `codex-findings/` (agent stdout captured by orchestrator).

### Phase 2 — synthesis + decision (Claude) [done 2026-08-28]

Agent A report: `codex-findings/A-load-path.md`; B: `codex-findings/B-runtime-startup.md`.
Key extra facts from A (spot-checked): MathJax = 4.66MB of 6.52MB built `editor.js` (71%);
`tex-chtml` non-full saves only 166KB and breaks package compat (upstream deliberately chose
full in e88dfb68a; editor needs full for sync `tex2svg`, ae18ba2a); upstream **origin/main
already lazy-loads reviewer MathJax**: 00dcfc3e8 (#5171) + 9c2d9d11d (#5254 preload) +
38f3603d1 (#5416 reuse-if-present) — small diffs confined to `ts/reviewer/index.ts` +
2 lines of `qt/aqt/reviewer.py`; fork main predates them.

**Decided implementation slate (in order):**

1. Fix fork-specific `js/mathjax.js` bundling defect (bundle render-cache import properly;
   built config must be a self-contained classic script). Prerequisite for everything.
2. Backport upstream lazy-load trio (cherry-pick, adapt to fork's index.ts); verify math
   detection covers `<anki-mathjax>` elements (upstream regex only matches `\(\)`/`\[\]`).
3. Editor: stop bundling `tex-svg-full` into editor.js; vendor it as a separate file
   (MATHJAX_FILES), memoized lazy loader on first enabled anki-mathjax decoration,
   placeholder until ready, then keep sync `tex2svg`. Watch `setMathjaxEnabled` ordering
   (editor.py sends it after setFields). Test list in A §editor.
   Skipped (no load-speed win / risk): tex-chtml non-full swap, font preload, mediasrv cache
   headers, gzip, a11y/SRE + editable.js packaging cleanups (candidates for separate tasks).

### Phase 3 — implementation (codex, workspace-write, red/green TDD)

Single implementer agent; targeted checks (`./ninja check:svelte`, `cargo check`, dmypy).

### Phase 4 — verify + ship (Claude)

Before/after numbers via Phase-1C harness; `./check`-level targeted checks (beware stale
worktree-minilints trap); codex review of diff; commit (excluding this plan + scratch files);
PR to fork.

## Status log

- 2026-08-28: plan created; facts above verified; Phase 1 agents launched.
- 2026-08-28: Agent B report in (`codex-findings/B-runtime-startup.md`). Headlines, with (*) =
  verified by orchestrator:
  - (*) **Built `js/mathjax.js` is broken**: `bundle:false` in `ts/transform_ts.mjs` leaves a
    bare ESM `import ./render-cache` in a classic script → SyntaxError → entire MathJax config
    (incl. render cache, `startup.typeset=false`, package list) inert in Qt builds. Fix first.
  - No extension waterfall exists: `tex-chtml-full` pre-registers html/mathtools/noerrors —
    "preload the 3 extensions" is a non-optimisation. Only `html.js` is even vendored.
  - Scripts are classic/parser-blocking in order webview.js → mathjax.js → tex-chtml-full.js
    (1.33MB) → reviewer.js (1.5MB) → `#qa`; MathJax is ~46% of reviewer JS bytes and is paid
    even with zero math.
  - Top-ranked: (0) fix config bundling; (1) conditional MathJax load owned by reviewer runtime
    (drop from reviewer/clayout/previewer script lists); (2) hybrid first-card detection +
    background answer prefetch; (3) inline config so component fetch starts immediately;
    (4) versioned immutable caching in mediasrv (`_handle_builtin_file_request` sends no cache
    headers); lower: targeted defer, font preload, precompression.
  - Render cache helps flips/repeats only; slight first-render overhead; per-webview.
- 2026-08-28: Agent C delivered `bench/` harness + baseline (`codex-findings/C-harness.md`,
  `bench/baseline.json`): QtWebEngine cold driver, M2 Max — math first-card 114.5ms,
  no-math 92.2ms, MathJax main bundle parse+eval ~72ms, 1.35MB served.
- 2026-08-28: Agent D implemented slate steps 1-2 (`codex-findings/D-impl-reviewer.md`);
  orchestrator reconstructed history: 59e353a10 (config bundling fix, verified: built
  mathjax.js now valid IIFE setting window.MathJax + renderActions), cherry-picks -x
  e3a28384d/05623d46c/b4b9ffe39 (upstream #5171/#5254/#5416, all clean), 2f10d6185
  (<anki-mathjax> detection + loader extraction + tests). check:svelte + check:vitest green.
- 2026-08-28: orchestrator extended bench with `*-lazy` cases replicating the new reviewer
  flow (dynamic config→component load after detection) + beacon-based Chrome driver (the old
  --virtual-time-budget/--dump-dom deadlocks on dynamically injected scripts). Qt-driver
  smoke run (n=2, main-checkout artifacts + fixture config): nomath 89.9ms → nomath-lazy
  15.8ms (bytes 1.35MB → 4.8KB); math 196.9ms → math-lazy 115.8ms (n too small; final run
  pending with candidate artifacts).
- 2026-08-28: Agent E (editor lazy-load) launched.
- 2026-08-28: Phase 4 complete. `./check` green (one pre-existing eslint error on main fixed
  in passing, 7fc6a8adc). Codex review (`codex-findings/F-review.md`) found 4 must-fix + 1
  nice-to-have; all five implemented by fixer agent + verified (`G-fixes.md`, dfe2cab9a):
  reviewer failure-path hardening, template-script MathJax awaiting + notetype-before-fields
  ordering, editor textmacros parity, pending-adoption in the editor loader, render-generation
  capture at schedule time.

## Results (QtWebEngine cold driver, Apple M2 Max, medians)

| Case                                     | Before           | After           | Δ             |
| ---------------------------------------- | ---------------- | --------------- | ------------- |
| Reviewer first card, no math (n=7)       | 91.4ms / 1.36MB  | 12.9ms / 4.8KB  | −86% / −99.6% |
| Reviewer first card, with math (n=7)     | 116.3ms          | 116.8ms         | unchanged     |
| editor.js fetch+parse+eval (Chrome, n=5) | 122.5ms / 6.52MB | 55.8ms / 3.59MB | −54% / −45%   |

Raw data: `bench/baseline.json` (eager, pre-change), `bench/candidate.json` (all four cases on
the branch's built artifacts). Reproduce with `bench/run-qt-cold.sh <web-dir> 7 --cases
math,nomath,math-lazy,nomath-lazy`.
