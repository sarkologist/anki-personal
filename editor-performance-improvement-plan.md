# Editor sluggishness with many MathJax elements — investigation plan

**Status:** diagnosis complete, no changes landed yet

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

### Profiling setup — TODO

How to get a Chromium DevTools Performance trace from the Anki editor needs to be confirmed before we start. Candidates:

- Anki's debug console (Ctrl+Shift+;) gives a Python repl; webview devtools may be reachable via `QWEBENGINE_REMOTE_DEBUGGING` env var.
- Setting `QTWEBENGINE_REMOTE_DEBUGGING=8888` (or similar) before launching, then opening `http://localhost:8888` in Chrome.
- Confirm by launching from the worktree (`./run` or whatever the dev launcher is — verify in build/).

Metric to track: **total scripting time per keystroke** while typing into a synthetic heavy field. Secondary: **input event handler duration** specifically.

### Synthetic test field

Create a note whose Front field contains ~30 MathJax inline expressions plus some plain text between them. Type a long sentence at the end. Repeat the same input across all profile runs. (We can script this with an Anki addon or just paste a known fixture; figure out a stable repro before measuring.)

## Diagnosis (from initial read of the code)

Per-keystroke chain that scales with field/element count:

1. **`FieldUndo.onMutation`** ([ts/editor/rich-text-input/field-undo.ts:53](ts/editor/rich-text-input/field-undo.ts:53)) — NEW since commit `80224bba1`.
   - Line 56 reads `this.base.innerHTML` (full decorated serialization, includes every MathJax SVG data URL) and string-compares it against `this.last.html` on every mutation batch.
   - During typing the comparison is always false, so the early-return is dead weight; we pay the O(N) cost on every keystroke.
   - **Hypothesis:** dominant new contributor to the slowness.

2. **`dom-mirror` cloneNode** ([ts/lib/sveltelib/dom-mirror.ts:60](ts/lib/sveltelib/dom-mirror.ts:60)) — `Range.cloneContents()` deep-clones the entire field on every mutation batch. Pre-existing.

3. **`normalizeFragment`** ([ts/editor/rich-text-input/normalizing-node-store.ts:10](ts/editor/rich-text-input/normalizing-node-store.ts:10)) — runs as `nodes` store preprocess on every `set`. `fragment.normalize()` + 3 separate `querySelectorAll` walks (one per decorated tag). Pre-existing.

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

**Status:** not started. Profile result: TBD. Decision: TBD.

### F2. FieldUndo: drive off `inputHandler` events instead of MutationObserver

**Hypothesis:** filters out internal Svelte/MathJax DOM churn the user doesn't care about, and avoids the parallel observer entirely. The existing `inputHandler` ([ts/lib/sveltelib/input-handler.ts](ts/lib/sveltelib/input-handler.ts)) already broadcasts user-driven `beforeInput`/`afterInput` events.

**Risk:** toolbar mutations (Surround) bypass `beforeInput` — that's exactly why FieldUndo was added. We'd need toolbar code to push undo steps explicitly (it already does, via `pushUndoSnapshot`), and to ensure we don't miss paste/IME paths.

**Status:** not started. Profile result: TBD. Decision: TBD. (Only pursue if F1 isn't enough.)

### F3. FieldUndo: defer `commit()` body to `requestIdleCallback`

**Hypothesis:** even with F1, `commit()` reads `innerHTML` + `saveSelection`. Wrapping it in `requestIdleCallback` (with a setTimeout fallback) makes worst-case typing latency independent of field size. The 300 ms debounce already gives a budget; idle deferral makes it strictly off the critical path.

**Status:** not started. Profile result: TBD. Decision: TBD.

### F4. Combine the three `querySelectorAll` passes in `normalizeFragment`

**Hypothesis:** small win. Replace three full-tree walks with one `querySelectorAll('anki-mathjax, anki-image, anki-frame')` and dispatch by `tagName`.

**Status:** not started. Profile result: TBD. Decision: TBD.

### F5. Avoid the double clone (dom-mirror + transform)

**Hypothesis:** [`dom-mirror.ts:60`](ts/lib/sveltelib/dom-mirror.ts:60) clones the field, then [`fragmentToStored`](ts/editor/rich-text-input/transform.ts:53) clones it again via `importNode` to keep `adjustOutputFragment.lastChild.remove()` from mutating the original. We could either (a) snapshot the trailing-`<br>` decision without cloning, or (b) share a single clone for the round-trip.

**Status:** not started. Profile result: TBD. Decision: TBD.

### F6. Bigger refactor: skip the per-keystroke `nodes ↔ content` round-trip while focused

**Hypothesis:** the bidirectional binding in [`RichTextInput.svelte:268`](ts/editor/rich-text-input/RichTextInput.svelte:268) serializes → re-parses → re-decorates internally on every keystroke, even though `mirrorFromFragment` is paused while focused (so the result is thrown away). `content` only needs to be in sync at save time (already 600 ms debounced in `NoteEditor`). Moving `fragmentToStored` behind that same debounce would eliminate steps 4 and 5 above.

**Risk:** touches the plain-text↔rich-text mirroring contract; deserves a proper plan. Don't pursue speculatively.

**Status:** not started. Profile result: TBD. Decision: TBD.

## Working log

Append entries as the work proceeds — each agent / session adds at the bottom.

- `2026-05-10` — Plan drafted from initial code read. Profiling setup not yet figured out; that's the next blocker.
