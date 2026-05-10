# Issue #3601 — Mathjax/blockquote backspace corruption

Upstream: https://github.com/ankitects/anki/issues/3601

## Repro

Field HTML:

```html
<div><div><anki-mathjax></anki-mathjax><blockquote>a</blockquote></div></div>
```

1. Open the note in the editor.
2. Place caret immediately after the letter `a` in the blockquote.
3. Press Backspace.

**Expected:** the `a` is deleted; structure is otherwise untouched.

**Actual:** the surrounding `<div>` wrappers are lost and the `<anki-mathjax>` element ends up undecorated / mis‑structured. In real notes this "messes up all the mathjax in the note."

## Runtime DOM

After `<anki-mathjax>` decorates itself ([`ts/editable/mathjax-element.svelte.ts:111-119`](ts/editable/mathjax-element.svelte.ts:111)) the live DOM is:

```html
<div><div>
  <anki-frame data-frames="anki-mathjax" block="false">
    <frame-start data-frames="anki-mathjax"> </frame-start>
    <anki-mathjax contenteditable="false" decorated="true">…</anki-mathjax>
    <frame-end data-frames="anki-mathjax"> </frame-end>
  </anki-frame>
  <blockquote>a</blockquote>
</div></div>
```

So the block immediately preceding the blockquote is `<anki-frame>`, whose last child is the `<frame-end>` hairline‑space handle.

## Root cause

Chromium's `contenteditable` handles "Backspace at the boundary of an empty/about‑to‑be‑empty block adjacent to another block" by mutating the surrounding DOM — typically merging or outdenting the empty block into the previous block. When the previous block is `<anki-frame>`, that mutation moves nodes into / out of the frame and its handles.

Anki's frame `MutationObserver` then "repairs" the structure, and _that repair is what destroys the wrappers and the mathjax_. Two pieces of repair logic over‑react:

### Over‑reactive branch ([`ts/editable/frame-element.ts:46-67`](ts/editable/frame-element.ts:46))

```ts
for (const node of mutation.removedNodes) {
    if (!isFrameHandle(node)) {
        continue;
    }

    if (
        /* avoid triggering when (un)mounting whole frame */
        mutations.length === 1
        && !node.partiallySelected
    ) {
        // Similar to a "movein", this could be considered a
        // "deletein" event and could get some special treatment, e.g.
        // first highlight the entire frame-element.
        frameElement.remove();
        continue;
    }

    if (frameElement.isConnected) {
        frameElement.refreshHandles();
        continue;
    }
}
```

The heuristic is: _if a single mutation removed a frame handle and the handle wasn't partially selected, the user intended to delete the whole frame, so remove it._ This treats handle removal as a **proxy for user intent to delete the frame**.

That heuristic is wrong when the handle was removed by a browser side‑effect (Chromium's block‑merge during backspace, normalization, BR placeholder insertion, etc.), not by the user. In the #3601 repro the framed `<anki-mathjax>` is still present and intact, but the frame nukes itself anyway, taking the wrapper structure with it as the cascade continues.

### Cascade

Once `frameElement.remove()` runs, further mutations fire on the parent divs, the now‑bare `<anki-mathjax>` re‑decorates / re‑frames itself in a different position, and `restoreHandleContent` ([`ts/editable/frame-handle.ts:47`](ts/editable/frame-handle.ts:47)) moves nodes around with `moveChildOutOfElement`, lifting children out of the inner `<div>`s. The end state is the corruption the user reports.

## Planned fix

Make frame removal conditional on the **framed element** being gone, not on a handle being gone. The framed element (`anki-mathjax`) is the canonical "is this frame still meaningful?" signal — there's already a branch using exactly that signal at [`ts/editable/frame-element.ts:19-24`](ts/editable/frame-element.ts:19):

```ts
const framed = frameElement.querySelector(frameElement.frames!) as HTMLElement;

if (!framed) {
    frameElement.remove();
    continue;
}
```

That branch is the right one. The handle‑removal branch should not duplicate it with a weaker proxy.

### Change

In [`ts/editable/frame-element.ts`](ts/editable/frame-element.ts), inside `restoreFrameHandles`, replace the over‑reactive branch (lines 51‑60 above) with logic that **only** restores handles and never removes the frame from this branch. Specifically: drop the `mutations.length === 1 && !node.partiallySelected` → `frameElement.remove()` path entirely. Keep the `frameElement.refreshHandles()` recovery.

Resulting shape:

```ts
for (const node of mutation.removedNodes) {
    if (!isFrameHandle(node)) {
        continue;
    }

    if (frameElement.isConnected) {
        frameElement.refreshHandles();
    }
}
```

Rationale:

- If the user genuinely deleted the whole frame (e.g. selected it and pressed Delete), the framed `<anki-mathjax>` is also gone, and the existing `if (!framed) frameElement.remove()` branch (lines 19‑24) handles it correctly.
- If only a handle was removed by a browser side‑effect, recreating the handle via `refreshHandles()` is the safe recovery — the frame and its mathjax stay intact.
- The `partiallySelected` flag and the comment about "deletein" become dead code for this branch; the flag is still read elsewhere ([`ts/editable/frame-handle.ts:341-345`](ts/editable/frame-handle.ts:341)) so leave the field itself in place.

### Out of scope (follow‑up)

Intercepting `deleteContentBackward` in [`ts/lib/sveltelib/input-handler.ts:60`](ts/lib/sveltelib/input-handler.ts:60) to prevent Chromium from doing the destructive merge in the first place is the structural fix and should be a separate change. This patch is the minimal, defensive fix that makes the observer stop amplifying browser surprises into frame destruction.

## Verification

1. **Manual repro** — load the field HTML from the Repro section, place caret right of `a`, press Backspace. Expected: `a` is deleted; the `<anki-frame>` / `<anki-mathjax>` structure and the surrounding `<div>` wrappers remain intact.
2. **Regression — explicit frame delete** — select the entire mathjax frame in the editor (e.g. drag‑select across it) and press Delete/Backspace. Expected: the frame goes away (this still works because the `!framed` branch removes the now‑orphan frame).
3. **Regression — typing around mathjax** — type before/after a mathjax element; the existing handle‑restore behavior should be unchanged.
4. **Build/check** — run `./check` (or at minimum `./ninja check:svelte`) before marking complete, per [`CLAUDE.md`](CLAUDE.md).

## Files touched

- [`ts/editable/frame-element.ts`](ts/editable/frame-element.ts) — single edit in `restoreFrameHandles`.

No other files need to change.
