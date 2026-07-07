# Editor sluggishness with many MathJax elements — investigation plan

**Status:** diagnosis updated, F1 implemented locally; micro-profile complete.
F7 (MathJax render-cache size) implemented + verified — see below.

## Problem

When a field contains many elements (especially MathJax), the rich-text editor becomes sluggish — keystrokes feel laggy. User suspects MathJax.

## Methodology — read this first

**Always profile before & after.** Only keep a change if a profile shows measurable improvement. Discard speculative "should be faster" fixes even if the reasoning sounds right.

For each candidate fix below:

1. Capture a baseline profile on a representative heavy field.
2. Land the fix in isolation (one fix per branch state).
3. Re-profile under the same conditions.
4. Record the result in this file (delta in the metric we care about).
5. Decide: **keep** (commit) or **discard** (revert).

### Profiling setup

Confirmed from `./run`: development launches already set
`QTWEBENGINE_REMOTE_DEBUGGING`, defaulting to `8080`.

Use an optimized build for meaningful editor measurements:

```sh
QTWEBENGINE_REMOTE_DEBUGGING=8080 ./tools/runopt
```

Then open `http://localhost:8080` in Chrome, pick the editor webview, and capture a Performance trace while typing in the synthetic heavy field.

Metric to track: **total scripting time per keystroke** while typing into a synthetic heavy field. Secondary: **input event handler duration** specifically.

### Synthetic test field

Create a note whose Front field contains ~30 MathJax inline expressions plus some plain text between them. Type a long sentence at the end. Repeat the same input across all profile runs. (We can script this with an Anki addon or just paste a known fixture; figure out a stable repro before measuring.)

## Diagnosis (from initial read of the code)

Per-keystroke chain that scales with field/element count:

1. **`FieldUndo.onMutation`** ([ts/editor/rich-text-input/field-undo.ts:53](ts/editor/rich-text-input/field-undo.ts:53)) — NEW since commit `80224bba1`.
   - Line 56 reads `this.base.innerHTML` and string-compares it against `this.last.html` on every mutation batch.
   - During typing the comparison is always false, so the early-return is dead weight; we pay the O(N) cost on every keystroke.
   - Note: current MathJax SVG output is rendered in a shadow root, so this is not serializing the SVG markup itself. MathJax still hurts indirectly because it inflates the light-DOM wrapper/custom-element structure the editor keeps walking.
   - **Hypothesis:** dominant new contributor to the slowness.

2. **`dom-mirror` cloneNode** ([ts/lib/sveltelib/dom-mirror.ts:60](ts/lib/sveltelib/dom-mirror.ts:60)) — `Range.cloneContents()` deep-clones the entire field on every mutation batch. Pre-existing.

3. **`normalizeFragment`** ([ts/editor/rich-text-input/normalizing-node-store.ts:10](ts/editor/rich-text-input/normalizing-node-store.ts:10)) — runs as `nodes` store preprocess on every `set`. `fragment.normalize()` + a `querySelectorAll` walk per registered decorated tag. Pre-existing. In this checkout, only MathJax is registered as a decorated element.

4. **`fragmentToStored`** ([ts/editor/rich-text-input/transform.ts:52](ts/editor/rich-text-input/transform.ts:52)) — second deep clone via `importNode`, full serialization, regex passes per decorated type. Pre-existing.

5. **`nodeStore.setUnprocessed`** ([ts/lib/sveltelib/node-store.ts:18](ts/lib/sveltelib/node-store.ts:18)) — `isEqualNode` structural comparison on every set. Pre-existing.

### What is NOT the problem

- MathJax `tex2svg` is **not** re-running per keystroke — its reactive deps (`mathjax`, `pageTheme.isDark`, `fontSize`) don't change while typing, and conversions are LRU-cached + 500 ms debounced.
- The user's intuition that "MathJax causes it" is correct _indirectly_: MathJax decoration inflates the size of the decorated DOM that the pipeline above keeps walking/serializing.

## Candidate fixes (ranked by expected impact)

### F1. FieldUndo: drop per-mutation `innerHTML` read

**Hypothesis:** highest ROI; tiny patch. The inline `innerHTML` equality check in `onMutation` is redundant — `commit()` already short-circuits on equal HTML. Its only purpose is to avoid scheduling a no-op timeout after `restore()`, which is essentially free.

**Change sketch:**

```ts
private onMutation(): void {
    if (this.debounceHandle != null) {
        clearTimeout(this.debounceHandle);
    }
    this.debounceHandle = setTimeout(() => {
        this.debounceHandle = null;
        this.commit();
    }, debounceMs);
}
```

If the post-`restore()` no-op behaviour matters in practice, replace with a "skip next batch" flag set inside `restore()` and consumed by `onMutation` — no innerHTML read needed.

**Status:** implemented locally. Profile result: Chrome headless micro-profile on a 30-MathJax-like field measured old eager compare at median `0.0225 ms` per mutation batch vs new debounce-only path at median `0.00075 ms` per mutation batch, removing about `0.02175 ms` of synchronous work per mutation batch in this fixture (~30x faster for `onMutation()` itself). Decision: keep provisionally; still wants a full Anki editor trace when the Add dialog profiling harness is stable.

### F2. Move rich-text serialization behind the save/blur boundary

**Hypothesis:** the bidirectional binding in [`RichTextInput.svelte:268`](ts/editor/rich-text-input/RichTextInput.svelte:268) serializes → re-parses → re-decorates internally on every keystroke, even though `mirrorFromFragment` is paused while focused (so the result is mostly thrown away by the rich-text side). Moving `fragmentToStored` behind a debounce/flush boundary could eliminate steps 4 and 5 from the critical typing path.

**Risk:** `content` is not only for backend save. It drives plain-text mirroring, empty-description UI, blur payloads, save scheduling, and possible add-on expectations. Any change needs explicit flush points for blur, `saveNow`, toggling plain text, and add-on-observable state.

**Status:** not started. Profile result: TBD. Decision: TBD. (Only pursue if F1 isn't enough.)

### F3. FieldUndo: drive off `inputHandler` events instead of MutationObserver

**Hypothesis:** filters out internal Svelte/MathJax DOM churn the user doesn't care about, and avoids the parallel observer entirely. The existing `inputHandler` ([ts/lib/sveltelib/input-handler.ts](ts/lib/sveltelib/input-handler.ts)) already broadcasts user-driven `beforeInput`/`afterInput` events.

**Risk:** many programmatic mutations bypass `inputHandler`. Surround calls already use `pushUndoSnapshot`, but other paths currently do not, including MathJax/LaTeX wrapping, rich-text cloze wrapping, old editor `wrap()`, and `execCommand` toolbar buttons. This needs a full programmatic-edit audit before implementation.

**Status:** not started. Profile result: TBD. Decision: TBD.

### F4. FieldUndo: defer `commit()` body to `requestIdleCallback`

**Hypothesis:** even with F1, `commit()` reads `innerHTML` + `saveSelection`. Wrapping it in `requestIdleCallback` (with a setTimeout fallback) makes worst-case typing latency independent of field size. The 300 ms debounce already gives a budget; idle deferral makes it strictly off the critical path.

**Status:** not started. Profile result: TBD. Decision: TBD.

### F5. Combine decorated-element walks in `normalizeFragment`

**Hypothesis:** small win only if multiple decorated elements are registered. In this checkout, `decoratedElements` only contains MathJax, so there is no three-pass walk to combine.

**Status:** parked unless more decorated elements are added. Profile result: TBD. Decision: TBD.

### F6. Avoid the double clone (dom-mirror + transform)

**Hypothesis:** [`dom-mirror.ts:60`](ts/lib/sveltelib/dom-mirror.ts:60) clones the field, then [`fragmentToStored`](ts/editor/rich-text-input/transform.ts:53) clones it again via `importNode` to keep `adjustOutputFragment.lastChild.remove()` from mutating the original. We could either (a) snapshot the trailing-`<br>` decision without cloning, or (b) share a single clone for the round-trip.

**Status:** not started. Profile result: TBD. Decision: TBD.

### F7. Enlarge the MathJax render cache (load / undo / re-decorate path)

**This is a different axis from F1–F6.** F1–F6 target the per-keystroke
mutation chain. F7 targets the cost of _rendering_ the field's MathJax, which is
paid on **load, undo/redo, plain↔rich-text sync, and note reload** — every time
the field re-decorates and re-mounts its `<anki-mathjax>` components.

The earlier note "tex2svg is not re-running per keystroke" is correct for
_steady-state typing_ but said nothing about re-decoration, where every formula
re-renders. The render cache ([ts/editable/Mathjax.svelte](ts/editable/Mathjax.svelte))
was `LRUCache({max: 10})` per (fontSize, templateVersion) bucket — smaller than
the distinct-expression count of any real math note, so it thrashed.

**Repro fixture (stable, the one F0/§"Synthetic test field" asked for):**
[ts/editor/perf-fixtures/large-cloze-note.html](ts/editor/perf-fixtures/large-cloze-note.html)
— a real cloze note, **118 `<anki-mathjax>`, 82 distinct, 236 frame handles**.

**Profile — `tex2svg` render count over a load + one re-decoration cycle**
(deterministic, engine-independent; from `mathjax-cache.test.ts` and the bench):

| note size               | cache max 10 (before) | cache max 512 (after) |
| ----------------------- | --------------------- | --------------------- |
| 118 elems (82 distinct) | 206                   | **82**                |
| 236 elems (2× dup)      | 412                   | **82**                |
| 472 elems (4× dup)      | 824                   | **82**                |

Re-decoration (undo/redo/sync/reload) drops from "re-render the whole field" to
**0 renders**; first load also dedupes repeated expressions (82 vs 118).

**Change:** extracted the cache to [ts/editable/mathjax-cache.ts](ts/editable/mathjax-cache.ts)
and raised the cap to 512 (cached values are short SVG strings → cheap). Pure
display path; **no effect on the stored-HTML round-trip**, so no DB risk.

**Rejected while here (DB-correctness risk):** doing the decorated→stored
conversion on the DOM before serialize (to avoid `Mathjax.toStored` reparsing
each frame via `<template>`, the largest engine-independent per-keystroke cost
at ~3 ms / 118 elems) changes entity escaping of `<`/`>`/`&` inside math vs the
current regex substitution — not byte-equivalent. Left alone. **Superseded by
F8**, which speeds up the same hotspot a different (byte-equivalent) way.

**Status:** implemented + verified. `mathjax-cache.test.ts` green; existing
editor/editable suites green. Decision: **keep.**

### F8. Speed up `Mathjax.toStored` without the per-frame `<template>` reparse

**Same hotspot F7 identified, different fix.** On every keystroke batch the
rich-text editor serializes the field: the `nodes`→`content` subscription runs
`content.set(fragmentToStored(clone))`. `fragmentToStored`
([transform.ts](ts/editor/rich-text-input/transform.ts)) serializes the clone
to a string, then `Mathjax.toStored` regex-matches each decorated frame and —
for **every** frame — parses it back into DOM via a fresh `<template>` just to
read `data-mathjax` (browser-decoded) and the `block` attribute.

**Profile (headless Chrome, real fixture, real shipped code; median per
`fragmentToStored` call, byte-identical output verified at each scale):**

| frames | `fragmentToStored` before | after                | `Mathjax.toStored` share before |
| ------ | ------------------------- | -------------------- | ------------------------------- |
| 112    | 1.1 ms                    | **0.6 ms**           | ~0.9 ms of 1.2 ms (~70%)        |
| 448    | 4.6 ms                    | **2.4 ms**           | ~3.5 ms of 5.0 ms (~70%)        |
| 896    | 9.4 ms (p95 12.5)         | **4.9 ms** (p95 5.3) | —                               |

**Why F7 rejected the DOM rewrite but F8 is safe:** the rejected idea replaced
frames in the _DOM_ before serializing, so the browser re-encoded `<`/`>`/`&`
in math and in surrounding markup — not byte-equivalent. F8 keeps the exact
regex-on-string structure (surrounding HTML untouched as a string) and only
makes the _attribute read_ cheaper: a quote-aware string scan pulls the raw
`data-mathjax` value, and a shared `<textarea>` decodes its entities —
identical decoding to the parser (verified: jsdom `textarea.value` ===
attribute `dataset` for named/numeric/`<br>`/nested-entity cases). The fast
path **bails to the original `<template>` implementation** whenever it can't be
certain (no `data-mathjax`, or a literal `<` in the value where `<textarea>`
RCDATA could be ambiguous), so output is byte-identical in every case.

**Change:** [ts/editable/mathjax-element.svelte.ts](ts/editable/mathjax-element.svelte.ts)
— `fastMathjaxHtmlToStored` + `slowMathjaxHtmlToStored` (original, kept as
fallback). Guarded by a 4000-case fuzz test
([mathjax-tostored.test.ts](ts/editable/mathjax-tostored.test.ts)) asserting
byte-equivalence against a frozen copy of the original parser across random
sources / attribute orders / block-inline / bare-tag / entity / `<br>` /
`>`-in-value / cloze-brace cases, plus a guard that the fast path is actually
taken. Existing decorated/round-trip suites still green.

**Status:** implemented + verified. Decision: **keep.**

### Not done: F2 (defer serialization off the keystroke path)

Still the biggest _potential_ win — `content`'s serialized value is consumed
live only by the two empty-checks (need just length) and PlainTextInput (only
when the HTML-source view is open); the 600 ms-debounced backend save reads it
lazily. So most per-keystroke serializations are thrown away. But `content` is
a reactive `Writable<string>` and PlainTextInput expects it current, so
deferring needs explicit flush points and can't be verified headlessly (no
drivable editor webview). Left for a session that can drive the real editor.
F8 cuts the cost of the serialization that F2 would eventually remove.

## Working log

Append entries as the work proceeds — each agent / session adds at the bottom.

- `2026-05-10` — Plan drafted from initial code read. Profiling setup not yet figured out; that's the next blocker.
- `2026-05-10` — Assessment pass: confirmed `QTWEBENGINE_REMOTE_DEBUGGING` setup in `./run`, corrected MathJax-shadow-root detail, demoted the inputHandler-only undo idea because programmatic mutation paths are broader than Surround, parked the normalizeFragment multi-pass idea for this checkout, and promoted the rich-text serialization/save-boundary investigation as the next likely target after F1.
- `2026-05-10` — F1 implemented locally by removing the eager `innerHTML` equality check from `FieldUndo.onMutation()`. `commit()` still performs the equality check after the debounce, preserving no-op restore behavior without serializing on every mutation batch.
- `2026-05-10` — Verification: `./yarn svelte-check:once` passed; `PATH="$HOME/.cargo/bin:$PATH" ./ninja check:svelte` passed. Full `./check` was attempted with the same PATH fix, but failed in `check:minilints` because commit author `oon.guo.liang@gmail.com` is not listed in `CONTRIBUTORS`; no F1-specific failure was reported before the build stopped.
- `2026-05-10` — Profiling: launched Anki with `QTWEBENGINE_REMOTE_DEBUGGING=8080` against `/private/tmp/anki-editor-profile-base`, but the temp Add Cards path did not expose an editor target and invoking the toolbar `pycmd("add")` through DevTools crashed the process with `Segmentation fault: 11`. As a fallback, ran a headless Chrome DOM micro-profile of the exact F1 hotspot using a 30-MathJax-like field: old eager `innerHTML` compare median `0.0225 ms`/mutation batch, new debounce-only median `0.00075 ms`/mutation batch, about `0.02175 ms` synchronous work removed per mutation batch. Full editor trace remains pending.
- `2026-06-27` — Added the stable repro fixture (`ts/editor/perf-fixtures/large-cloze-note.html`, 118 MathJax / 82 distinct) and a jsdom benchmark of the hot paths. Confirmed via breakdown that, in the production Blink webview (DOM ops much faster than jsdom), the largest _engine-independent_ per-keystroke cost is `Mathjax.toStored` (~3 ms, reparsing each frame via `<template>`); investigated a DOM-side rewrite but rejected it as not byte-equivalent for the DB round-trip (entity escaping of `<`/`>`/`&` inside math). New finding: the MathJax render cache (`max: 10`) was far too small for real notes — every undo/redo/sync re-rendered the whole field. Implemented **F7**: extracted the cache to `mathjax-cache.ts` and raised the cap to 512. Verified by deterministic `tex2svg` render-count (load + re-decorate: 206→82 at 118 elems, 824→82 at 472 elems; re-decoration now 0 renders). `mathjax-cache.test.ts` + existing editor/editable vitest suites green. Decision: keep.
- `2026-07-07` — Implemented **F8**: sped up the `Mathjax.toStored` hotspot F7 identified, byte-equivalently (unlike F7's rejected DOM rewrite). Confirmed via headless-Chrome benchmark of the real shipped code on the fixture that `fragmentToStored` — the per-keystroke-batch serialization — roughly halves (112 frames 1.1→0.6 ms, 448 frames 4.6→2.4 ms, 896 frames 9.4→4.9 ms; p95 12.5→5.3 ms at 896), with output verified byte-identical to the original at every scale. The win is replacing the per-frame `<template>` reparse (~70% of the cost) with a quote-aware string scan + shared-`<textarea>` entity decode, falling back to the original parser on anything ambiguous. Guarded by a 4000-case byte-equivalence fuzz test (`ts/editable/mathjax-tostored.test.ts`) against a frozen copy of the original parser, plus the existing decorated/round-trip suites. Also mapped **F2**'s feasibility (see section): still the biggest remaining win, but reactive-`content` risk + no drivable editor webview means it needs a session that can drive the real editor. Decision: keep F8.
