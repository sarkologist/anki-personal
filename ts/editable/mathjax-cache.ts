// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { LRUCache } from "lru-cache";

/**
 * A rendered MathJax SVG is a pure function of its source, the inherited font
 * size, and the notetype template version. Caching the result lets us skip the
 * expensive `tex2svg` render when the same expression is shown again — which
 * happens far more than it looks:
 *
 * - large notes repeat expressions (the same `\Delta`, `\beta`, ... dozens of
 *   times);
 * - every undo/redo, plain<->rich-text sync, or note reload re-decorates the
 *   field, re-mounting and re-rendering **all** of its MathJax from scratch.
 *
 * The previous cap of 10 entries was smaller than the distinct-expression count
 * of any non-trivial math note, so it thrashed: duplicates missed on load and a
 * single undo re-rendered the whole field. A note with hundreds of distinct
 * expressions now fits in the cache, so re-decoration is effectively free.
 *
 * Cached values are short SVG strings, so a generous cap is cheap in memory.
 */
export const MAX_ENTRIES_PER_BUCKET = 512;

/** `[converted SVG markup, title]`, as returned by `convertMathjax`. */
export type MathjaxConversion = [string, string];

type Bucket = LRUCache<string, MathjaxConversion>;

const buckets = new Map<string, Bucket>();

function bucketFor(fontSize: number, templateScriptVersion: number): Bucket {
    const key = `${fontSize}:${templateScriptVersion}`;
    let bucket = buckets.get(key);
    if (!bucket) {
        bucket = new LRUCache<string, MathjaxConversion>({ max: MAX_ENTRIES_PER_BUCKET });
        buckets.set(key, bucket);
    }
    return bucket;
}

/**
 * Return the cached conversion for `mathjax` at the given font size / template
 * version, computing and caching it with `compute` on a miss.
 */
export function getCachedMathjaxConversion(
    mathjax: string,
    fontSize: number,
    templateScriptVersion: number,
    compute: () => MathjaxConversion,
): MathjaxConversion {
    const bucket = bucketFor(fontSize, templateScriptVersion);
    const cached = bucket.get(mathjax);
    if (cached) {
        return cached;
    }

    const conversion = compute();
    bucket.set(mathjax, conversion);
    return conversion;
}

/** Test seam: drop all cached conversions. */
export function resetMathjaxCache(): void {
    buckets.clear();
}
