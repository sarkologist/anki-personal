// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { LRUCache } from "lru-cache";

/**
 * Per-expression render cache for the reviewer/preview MathJax pipeline.
 *
 * Every card flip replaces `#qa` wholesale and re-typesets the entire card,
 * even though the answer usually embeds `{{FrontSide}}` (so the question's
 * math is re-rendered immediately after it was just shown) and consecutive
 * cards repeat most expressions. Typesetting cost is linear in expression
 * count (~0.2 ms each in desktop Chrome, more in QtWebEngine), so heavy
 * cards pay hundreds of milliseconds per flip.
 *
 * These hooks plug into MathJax's `renderActions` pipeline:
 *
 * - `restoreFromCache` runs after the compile stage: on a hit it installs a
 *   clone of the previously rendered element as the item's `typesetRoot` and
 *   advances the item state past the typeset stage, so the metrics/typeset
 *   stages skip the item and the standard insert stage places the clone;
 * - `storeInCache` runs after the typeset and assistive-mml stages but
 *   before the menu stage, cloning freshly rendered output into the cache.
 *   Storing later would bake the menu's `CtxtMenu_Attached_*` markers into
 *   the clones, and the menu skips elements that already carry them — so
 *   restored math would silently lose its contextual menu.
 *
 * Compile is never skipped, even on a hit: later stages (menu attach) read
 * `math.root` eagerly, and more importantly TeX state mutations
 * (`\newcommand` etc.) happen at compile time, so the document's macro state
 * evolves identically with and without the cache.
 *
 * Why clones stay valid across typeset runs: the CHTML output jax
 * accumulates its font data for the lifetime of the page (Anki never calls
 * `output.clearCache()`), so the stylesheet always covers characters used by
 * earlier renders. `MathJax.typesetClear()` — called on every flip — only
 * clears the math item list, not the font data. Colors are inherited via
 * `currentColor`, so theme changes don't invalidate entries.
 */
export const MAX_ENTRIES = 1024;

/**
 * MathItem states (stable protocol values). Core: compiled 20, typeset 150,
 * inserted 200. The combined component also registers assistive-mml at 153
 * and the contextual menu at 170.
 */
const STATE_COMPILED = 20;
export const STATE_TYPESET = 150;
const STATE_ASSISTIVEMML = 153;
const STATE_CONTEXT_MENU = 170;

/**
 * State given to restored items: past typeset (150) and assistive-mml (153)
 * — the stored clone already contains both the CHTML output and the
 * assistive MathML, so letting the assistive stage run again would attach a
 * duplicate — but before the menu stage (170), which must re-run because
 * event listeners don't survive `cloneNode`, and the insert stage (200),
 * which places the clone in the document.
 */
export const STATE_RESTORED = STATE_ASSISTIVEMML + 2;

/**
 * Sources that read or write cross-expression document state (equation
 * labels/numbers) can't be reused out of context. `(?![a-zA-Z])` is the TeX
 * control-word boundary: `\ref1` is `\ref` applied to `1`, but a JS `\b`
 * wouldn't match between `f` and `1`.
 */
const uncacheablePattern = /\\(?:label|ref|eqref|tag)(?![a-zA-Z])/;

/**
 * Sources that mutate TeX state at compile time, changing how *later*
 * expressions render. Executions are folded into `texStateHash` below.
 */
const definitionPattern =
    /\\(?:newcommand|renewcommand|providecommand|newenvironment|renewenvironment|def|gdef|edef|xdef|let|futurelet|DeclareMathOperator|DeclarePairedDelimiter|definecolor|colorlet|require)(?![a-zA-Z])/;

/** The parts of mathjax's MathItem the cache relies on. */
export interface MathItemLike {
    /** The TeX source. */
    math: string;
    display: boolean;
    start: { node: Node | null };
    typesetRoot: Element | null;
    state(value?: number): number;
    ankiCacheKey?: string | null;
    ankiFromCache?: boolean;
}

/** The parts of mathjax's MathDocument the cache relies on. */
export interface MathDocumentLike {
    math: Iterable<MathItemLike>;
    /** Contextual-menu handler state, when the menu component is loaded. */
    menu?: { settings?: unknown };
}

const cache = new LRUCache<string, Element>({ max: MAX_ENTRIES });

function djb2(input: string): number {
    let hash = 5381;
    for (let i = 0; i < input.length; i++) {
        hash = ((hash << 5) + hash + input.charCodeAt(i)) >>> 0;
    }
    return hash;
}

/**
 * Rolling signature of the definition-bearing sources compiled so far, in
 * document order. Entries rendered under one signature are never reused
 * under another, so a `\newcommand` that changes meaning between decks can't
 * serve a stale render. Consecutive identical definition blocks are folded
 * — re-running the same block is idempotent — so the common "every card of
 * the template starts with the same macro block" pattern keeps its cache
 * hits.
 */
let texStateHash = 0;
let lastDefinitionSource: string | null = null;

function noteTexStateMutations(source: string): void {
    if (!definitionPattern.test(source)) {
        return;
    }
    if (source === lastDefinitionSource) {
        return;
    }
    lastDefinitionSource = source;
    texStateHash = djb2(`${texStateHash}|${source}`);
}

/**
 * Fingerprint of the MathJax menu settings (scale, renderer, assistive
 * toggles, ...), refreshed at the start of each render pass. A settings
 * change alters the required output, so it must key the cache.
 */
let settingsKey = "";

/** Called once per render pass, before any lookups. */
export function beginRenderPass(doc: MathDocumentLike): void {
    let settings = "";
    if (doc.menu?.settings != null) {
        try {
            settings = JSON.stringify(doc.menu.settings);
        } catch {
            // non-serializable settings: fall back to a shared bucket
        }
    }
    settingsKey = settings;
}

function cacheKey(item: MathItemLike): string | null {
    if (uncacheablePattern.test(item.math)) {
        return null;
    }
    const parent = item.start.node?.parentElement;
    if (!parent) {
        return null;
    }
    // The rendered size depends on the surrounding font (CHTML scales by the
    // measured ex-height), so the same source inside an <h1> and in body
    // text must not share an entry.
    const style = getComputedStyle(parent);
    return `${
        item.display ? "D" : "I"
    } ${texStateHash} ${settingsKey} ${style.fontSize} ${style.fontFamily} ${item.math}`;
}

/**
 * On a cache hit, install a clone of the cached render and skip the
 * metrics/typeset stages. Returns whether the item was restored.
 *
 * Definition-bearing sources fold into the TeX-state signature here, after
 * this item's key is computed: a definition takes effect for *subsequent*
 * expressions (its own source is already part of its key).
 */
export function restoreFromCache(item: MathItemLike): boolean {
    if (item.state() >= STATE_TYPESET) {
        return false;
    }
    const key = cacheKey(item);
    // Stash for storeInCache: the start node is replaced by the insert
    // stage, so the key can't be recomputed later.
    item.ankiCacheKey = key;
    noteTexStateMutations(item.math);
    const cached = key && cache.get(key);
    if (!cached) {
        return false;
    }
    item.typesetRoot = cached.cloneNode(true) as Element;
    item.ankiFromCache = true;
    item.state(STATE_RESTORED);
    return true;
}

/**
 * After the typeset/assistive-mml stages (but before the menu stage — see
 * the module comment), remember freshly rendered output.
 */
export function storeInCache(item: MathItemLike): void {
    if (item.ankiFromCache || !item.ankiCacheKey) {
        return;
    }
    if (item.state() < STATE_TYPESET) {
        return;
    }
    const root = item.typesetRoot;
    if (!root || typeof root.cloneNode !== "function") {
        return;
    }
    cache.set(item.ankiCacheKey, root.cloneNode(true) as Element);
}

type RenderAction = [
    number,
    (doc: MathDocumentLike) => void,
    (math: MathItemLike, doc: MathDocumentLike) => void,
];

/**
 * `renderActions` entries for the MathJax configuration, keyed by action
 * name. Lookup runs just after the compile (20) stage; store runs after
 * typeset (150) and assistive-mml (153) but before the menu (170) and
 * insert (200) stages.
 */
export function renderCacheActions(): Record<string, RenderAction> {
    return {
        ankiCacheLookup: [
            STATE_COMPILED + 10,
            (doc) => {
                beginRenderPass(doc);
                for (const math of doc.math) {
                    restoreFromCache(math);
                }
            },
            (math, doc) => {
                beginRenderPass(doc);
                restoreFromCache(math);
            },
        ],
        ankiCacheStore: [
            STATE_CONTEXT_MENU - 10,
            (doc) => {
                for (const math of doc.math) {
                    storeInCache(math);
                }
            },
            (math) => {
                storeInCache(math);
            },
        ],
    };
}

/** Test seam: drop all cached renders and state signatures. */
export function resetRenderCache(): void {
    cache.clear();
    texStateHash = 0;
    lastDefinitionSource = null;
    settingsKey = "";
}
