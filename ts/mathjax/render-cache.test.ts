// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { beforeEach, expect, test } from "vitest";

import type { MathDocumentLike, MathItemLike } from "./render-cache";
import {
    beginRenderPass,
    MAX_ENTRIES,
    renderCacheActions,
    resetRenderCache,
    restoreFromCache,
    STATE_RESTORED,
    STATE_TYPESET,
    storeInCache,
} from "./render-cache";

const STATE_FINDMATH = 10;
const STATE_COMPILED = 20;
const STATE_CONTEXT_MENU = 170;
const STATE_INSERTED = 200;

function fakeItem(tex: string, display: boolean, parent?: HTMLElement): MathItemLike {
    const host = parent ?? document.createElement("div");
    const node = document.createTextNode(display ? `\\[${tex}\\]` : `\\(${tex}\\)`);
    host.appendChild(node);

    let state = STATE_FINDMATH;
    return {
        math: tex,
        display,
        start: { node },
        typesetRoot: null,
        state(value?: number): number {
            if (value != null) {
                state = value;
            }
            return state;
        },
    };
}

/** Simulate MathJax's own render pipeline for a cache miss. */
function renderNormally(item: MathItemLike, markup: string): void {
    const root = document.createElement("mjx-container");
    root.innerHTML = markup;
    item.typesetRoot = root;
    item.state(STATE_INSERTED);
}

beforeEach(() => {
    resetRenderCache();
});

test("miss leaves the item untouched, then store+lookup round-trips", () => {
    const first = fakeItem("\\alpha", false);
    expect(restoreFromCache(first)).toBe(false);
    expect(first.state()).toBe(STATE_FINDMATH);

    renderNormally(first, "<mjx-math>a</mjx-math>");
    storeInCache(first);

    const second = fakeItem("\\alpha", false);
    expect(restoreFromCache(second)).toBe(true);
    // past typeset and assistive-mml (the clone carries both), before the
    // menu-attach and insert stages
    expect(second.state()).toBe(STATE_RESTORED);
    expect(second.state()).toBeGreaterThan(STATE_TYPESET);
    expect(second.state()).toBeLessThan(STATE_INSERTED);
    expect((second.typesetRoot as Element).outerHTML).toBe(
        (first.typesetRoot as Element).outerHTML,
    );
    // must be a clone, not the same node
    expect(second.typesetRoot).not.toBe(first.typesetRoot);
});

test("each hit gets its own clone", () => {
    const rendered = fakeItem("x^2", false);
    restoreFromCache(rendered);
    renderNormally(rendered, "<mjx-math>x</mjx-math>");
    storeInCache(rendered);

    const a = fakeItem("x^2", false);
    const b = fakeItem("x^2", false);
    restoreFromCache(a);
    restoreFromCache(b);
    expect(a.typesetRoot).not.toBe(b.typesetRoot);
});

test("display and inline forms are cached separately", () => {
    const inline = fakeItem("\\beta", false);
    restoreFromCache(inline);
    renderNormally(inline, "<mjx-math>inline</mjx-math>");
    storeInCache(inline);

    const display = fakeItem("\\beta", true);
    expect(restoreFromCache(display)).toBe(false);
});

test("parent font size participates in the key", () => {
    const small = document.createElement("div");
    small.style.fontSize = "16px";
    const big = document.createElement("div");
    big.style.fontSize = "32px";

    const inSmall = fakeItem("\\gamma", false, small);
    restoreFromCache(inSmall);
    renderNormally(inSmall, "<mjx-math>s</mjx-math>");
    storeInCache(inSmall);

    expect(restoreFromCache(fakeItem("\\gamma", false, small))).toBe(true);
    expect(restoreFromCache(fakeItem("\\gamma", false, big))).toBe(false);
});

test.each(["\\label{eq:1} x", "\\ref{eq:1}", "\\eqref{eq:1}", "x \\tag{3}", "\\ref1"])(
    "sources with cross-expression state are never cached: %s",
    (tex) => {
        const item = fakeItem(tex, true);
        expect(restoreFromCache(item)).toBe(false);
        renderNormally(item, "<mjx-math>t</mjx-math>");
        storeInCache(item);

        expect(restoreFromCache(fakeItem(tex, true))).toBe(false);
    },
);

test("items restored from cache are not stored back", () => {
    const first = fakeItem("\\delta", false);
    restoreFromCache(first);
    renderNormally(first, "<mjx-math>1</mjx-math>");
    storeInCache(first);

    const hit = fakeItem("\\delta", false);
    restoreFromCache(hit);
    // mutate the clone; storing it back would corrupt the cache
    (hit.typesetRoot as Element).innerHTML = "<mjx-math>corrupt</mjx-math>";
    storeInCache(hit);

    const third = fakeItem("\\delta", false);
    restoreFromCache(third);
    expect((third.typesetRoot as Element).innerHTML).toBe("<mjx-math>1</mjx-math>");
});

test("items that never finished rendering are not stored", () => {
    const item = fakeItem("\\epsilon", false);
    restoreFromCache(item);
    // no render happened; state still below inserted, typesetRoot null
    storeInCache(item);
    expect(restoreFromCache(fakeItem("\\epsilon", false))).toBe(false);
});

test("detached items (no parent element) are skipped gracefully", () => {
    const item = fakeItem("\\zeta", false);
    (item.start.node as ChildNode).remove();
    expect(restoreFromCache(item)).toBe(false);
    renderNormally(item, "<mjx-math>z</mjx-math>");
    storeInCache(item);
    // nothing cached under a usable key
    expect(restoreFromCache(fakeItem("\\zeta", false))).toBe(false);
});

test("cache is bounded", () => {
    for (let i = 0; i < MAX_ENTRIES + 1; i++) {
        const item = fakeItem(`x_{${i}}`, false);
        restoreFromCache(item);
        renderNormally(item, `<mjx-math>${i}</mjx-math>`);
        storeInCache(item);
    }
    // oldest entry evicted, newest present
    expect(restoreFromCache(fakeItem("x_{0}", false))).toBe(false);
    expect(restoreFromCache(fakeItem(`x_{${MAX_ENTRIES}}`, false))).toBe(true);
});

test("renderActions expose lookup after compile and store before the menu stage", () => {
    const { ankiCacheLookup, ankiCacheStore } = renderCacheActions();
    // compile must still run for cached items (later stages need math.root),
    // so lookup slots after compile but before the expensive stages
    expect(ankiCacheLookup[0]).toBeGreaterThan(STATE_COMPILED);
    expect(ankiCacheLookup[0]).toBeLessThan(STATE_TYPESET);
    // storing after the menu stage would bake CtxtMenu markers into clones,
    // and the menu skips elements that already carry them
    expect(ankiCacheStore[0]).toBeGreaterThan(STATE_TYPESET);
    expect(ankiCacheStore[0]).toBeLessThan(STATE_CONTEXT_MENU);

    // doc-level actions iterate doc.math
    const seed = fakeItem("\\eta", false);
    restoreFromCache(seed);
    renderNormally(seed, "<mjx-math>e</mjx-math>");

    const doc = { math: [seed] };
    (ankiCacheStore[1] as (doc: unknown) => void)(doc);

    const hit = fakeItem("\\eta", false);
    const hitDoc = { math: [hit] };
    (ankiCacheLookup[1] as (doc: unknown) => void)(hitDoc);
    expect(hit.state()).toBe(STATE_RESTORED);
});

test("a macro definition invalidates renders of later expressions", () => {
    const use = fakeItem("\\foo", false);
    restoreFromCache(use);
    renderNormally(use, "<mjx-math>error-render</mjx-math>");
    storeInCache(use);

    // a card defines the macro; subsequent \foo must not reuse the old render
    restoreFromCache(fakeItem("\\newcommand{\\foo}{x}", false));
    expect(restoreFromCache(fakeItem("\\foo", false))).toBe(false);
});

test("the definition takes effect after, not before, its own expression", () => {
    const before = fakeItem("\\foo", false);
    restoreFromCache(before);
    renderNormally(before, "<mjx-math>plain</mjx-math>");
    storeInCache(before);

    // same flip, before the definition appears: still a hit
    expect(restoreFromCache(fakeItem("\\foo", false))).toBe(true);
});

test("re-running the same definition block keeps cache hits", () => {
    // card 1: template macro block, then a use
    restoreFromCache(fakeItem("\\providecommand{\\R}{\\mathbb{R}}", false));
    const use = fakeItem("\\R", false);
    restoreFromCache(use);
    renderNormally(use, "<mjx-math>R</mjx-math>");
    storeInCache(use);

    // card 2: the same macro block runs again (e.g. via {{FrontSide}})
    restoreFromCache(fakeItem("\\providecommand{\\R}{\\mathbb{R}}", false));
    expect(restoreFromCache(fakeItem("\\R", false))).toBe(true);
});

test("changed menu settings invalidate cached renders", () => {
    const docAt = (scale: number): MathDocumentLike => ({
        math: [],
        menu: { settings: { scale } },
    });

    beginRenderPass(docAt(1));
    const item = fakeItem("\\theta", false);
    restoreFromCache(item);
    renderNormally(item, "<mjx-math>t</mjx-math>");
    storeInCache(item);
    expect(restoreFromCache(fakeItem("\\theta", false))).toBe(true);

    beginRenderPass(docAt(2));
    expect(restoreFromCache(fakeItem("\\theta", false))).toBe(false);
});
