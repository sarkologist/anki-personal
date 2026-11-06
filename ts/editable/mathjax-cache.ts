// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { LRUCache } from "lru-cache";

/**
 * A rendered MathJax SVG is a pure function of its source, the inherited font
 * size, whether it is block (display) or inline math, and the notetype template
 * version. Caching the result lets us skip the
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
/**
 * The template version only ever increases, so once a newer one is seen the
 * older buckets can never be hit again; keeping them would grow the cache by up
 * to a full bucket of SVGs per notetype switch.
 */
let currentTemplateScriptVersion = -Infinity;

function bucketFor(
    fontSize: number,
    block: boolean,
    templateScriptVersion: number,
): Bucket | null {
    if (templateScriptVersion < currentTemplateScriptVersion) {
        // A render that started before a notetype switch: don't cache it.
        return null;
    }
    if (templateScriptVersion > currentTemplateScriptVersion) {
        currentTemplateScriptVersion = templateScriptVersion;
        buckets.clear();
    }
    const key = `${fontSize}:${block}`;
    let bucket = buckets.get(key);
    if (!bucket) {
        bucket = new LRUCache<string, MathjaxConversion>({ max: MAX_ENTRIES_PER_BUCKET });
        buckets.set(key, bucket);
    }
    return bucket;
}

/**
 * Return the cached conversion for `mathjax` at the given font size, math style
 * (block vs inline) and template version, computing and caching it with
 * `compute` on a miss.
 */
export function getCachedMathjaxConversion(
    mathjax: string,
    fontSize: number,
    block: boolean,
    templateScriptVersion: number,
    compute: () => MathjaxConversion,
): MathjaxConversion {
    const bucket = bucketFor(fontSize, block, templateScriptVersion);
    const cached = bucket?.get(mathjax);
    if (cached) {
        return cached;
    }

    const conversion = compute();
    bucket?.set(mathjax, conversion);
    return conversion;
}

/**
 * Wait for MathJax before consulting the render cache. In particular, the
 * loading placeholder must never occupy the cache entry for the real SVG.
 */
export async function getCachedMathjaxConversionAfterLoad(
    mathjax: string,
    fontSize: number,
    block: boolean,
    templateScriptVersion: number,
    load: () => Promise<void>,
    compute: () => MathjaxConversion,
): Promise<MathjaxConversion> {
    await load();
    return getCachedMathjaxConversion(
        mathjax,
        fontSize,
        block,
        templateScriptVersion,
        compute,
    );
}

/** Test seam: drop all cached conversions. */
export function resetMathjaxCache(): void {
    buckets.clear();
    currentTemplateScriptVersion = -Infinity;
}

/** Test seam: number of live (font size, math style) buckets. */
export function mathjaxCacheBucketCount(): number {
    return buckets.size;
}
