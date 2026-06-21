# Multiline legacy-LaTeX editor preview

## Problem

The legacy LaTeX editor preview (`<anki-latex>`) breaks for multiline LaTeX. A
`\begin{tikzcd} … \end{tikzcd}` diagram renders as a "LaTeX error" placeholder and
the body of the diagram leaks out of the element as plain text in sibling `<div>`s.

Observed field HTML (abridged):

```html
forgetful functor <anki-latex data-latex-kind="display"><span>U</span></anki-latex>, …
<div><anki-latex data-latex-kind="display">\begin{tikzcd}&nbsp;…</anki-latex></div>
<div>&amp; 1 \ar[dl,"1",swap] \ar[dr,"a"] &amp; \\</div>
<div>U(\mathbb N) \ar[rr, "Uf", dotted] &amp;&amp; U(M) \\</div>
<div>\mathbb N \ar[rr, "f", dotted] &amp;&amp; M</div>
<div>\end{tikzcd}</div>
```

Only `\begin{tikzcd}` is inside `<anki-latex>`; every other line is a sibling `<div>`.
The single-line `U` element is intact — only the multiline one is split.

## Root cause

Creating a LaTeX element from a selection goes through the toolbar:

`LatexButton.svelte` → `surround()` → `wrapInternal()` (`ts/lib/tslib/wrap.ts`)

`wrapInternal` clones the selection's HTML and inserts
`<anki-latex …>` + selectionHTML + `</anki-latex>` via `execCommand("inserthtml")`.
A multiline selection's line breaks are block `<div>`s. `<anki-latex>` is an
**inline** custom element, so the browser hoists the block `<div>`s out of it
("block-in-inline" splitting), leaving only the first line inside the element and
the rest as siblings — exactly the observed HTML.

The render pipeline already handles multiline correctly: Rust
`extract_latex`/`strip_html_for_latex` (`rslib/src/latex.rs`) converts
`<br>`/`<div>` → `\n` and strips tags. So _intact_ multiline source renders fine;
the only defect is the editor tearing the source apart at creation.

## Fix

Multiline math source is canonically a single text run with `\n` newlines: these
elements use `white-space: pre`, and `trimBreaks` (mathjax) /
`normalizeLegacyLatexSource` (legacy) already normalize line breaks to `\n`.

When wrapping a selection as an inline `<anki-latex>`, flatten the selection's
block structure to newlines **before** inserting, so no block element remains
inside the inline element and the browser cannot split it.

- `ts/editor/latex-overlay/convert-to-mathjax.ts`: add
  `flattenBlocksToNewlines(fragment)` — reuse `textContentWithLegacyBreaks`
  (DIV/`<br>` → `\n`), strip tags to text, normalize `` → space, trim outer
  newlines, replace the fragment's children with one text node.
- `ts/editor/editor-toolbar/LatexButton.svelte`: `onLatexEquation` /
  `onLatexMathEnv` pass a normalize that runs `undecorateFragment` then
  `flattenBlocksToNewlines`.

Editing existing multiline through the overlay CodeMirror already works (it stores
`\n`). Re-applying "LaTeX math environment" over a previously-split region
(decorated element + leaked sibling `<div>`s) undecorates then flattens, recovering
intact source — so users can repair old notes by re-selecting and re-applying.

Out of scope: `<anki-mathjax>` wraps share the same latent block-in-inline issue,
but the report is about legacy LaTeX; left unchanged for now.

## Tests

- `convert-to-mathjax.test.ts`: `flattenBlocksToNewlines` flattens `<div>`/`<br>`
  to newlines, strips inline tags, normalizes nbsp, trims.
- wrap-path test: wrapping a multiline selection as `<anki-latex>` yields a single
  `<anki-latex …>line1\nline2\nline3</anki-latex>` with no `<div>` inside.

## Verification

- `./check` (svelte-check, eslint, vitest, rust).
- jsdom can't reproduce the browser's block-in-inline split, so the unit test
  asserts the inserted content is flattened (an inline element holding only
  text + newlines cannot be split). Real-app spot check if feasible.

## Status

- [x] plan written
- [x] `flattenBlocksToNewlines` helper
- [x] wire into LaTeX wrap
- [x] tests (`convert-to-mathjax.test.ts`: 4 new, all green)
- [x] `./check` green

Note: jsdom cannot reproduce the browser's block-in-inline split, so the wrap
tests assert the _inserted_ HTML is a single newline-separated `<anki-latex>` with
no `<div>` (an inline element holding only text + `\n` cannot be split). Live
in-app spot check still pending.
