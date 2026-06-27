// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { beforeEach, describe, expect, test } from "vitest";

import {
    getCachedMathjaxConversion,
    type MathjaxConversion,
    MAX_ENTRIES_PER_BUCKET,
    resetMathjaxCache,
} from "./mathjax-cache";

beforeEach(() => {
    resetMathjaxCache();
});

function conversion(text: string): MathjaxConversion {
    return [`<svg>${text}</svg>`, ""];
}

describe("mathjax render cache", () => {
    test("computes once per distinct expression, hits on repeat", () => {
        let computes = 0;
        const render = (src: string) =>
            getCachedMathjaxConversion(src, 20, 0, () => {
                computes++;
                return conversion(src);
            });

        expect(render("\\Delta")).toEqual(conversion("\\Delta"));
        expect(render("\\Delta")).toEqual(conversion("\\Delta"));
        expect(render("\\beta")).toEqual(conversion("\\beta"));
        expect(computes).toBe(2);
    });

    test("separates entries by font size and template version", () => {
        let computes = 0;
        const compute = () => {
            computes++;
            return conversion("x");
        };

        getCachedMathjaxConversion("x", 20, 0, compute);
        getCachedMathjaxConversion("x", 30, 0, compute); // different font size
        getCachedMathjaxConversion("x", 20, 1, compute); // different version
        getCachedMathjaxConversion("x", 20, 0, compute); // hit
        expect(computes).toBe(3);
    });

    test("retains a full bucket of distinct expressions and stays bounded", () => {
        // The whole point of the change is that the cap comfortably exceeds the
        // distinct-expression count of a large note.
        expect(MAX_ENTRIES_PER_BUCKET).toBeGreaterThanOrEqual(256);

        let computes = 0;
        const render = (src: string) =>
            getCachedMathjaxConversion(src, 20, 0, () => {
                computes++;
                return conversion(src);
            });

        const first = Array.from({ length: MAX_ENTRIES_PER_BUCKET }, (_, i) => `a${i}`);
        first.forEach(render);
        expect(computes).toBe(MAX_ENTRIES_PER_BUCKET);

        // A second pass over the same set re-renders nothing: the cap holds an
        // entire large note's worth of distinct expressions.
        computes = 0;
        first.forEach(render);
        expect(computes).toBe(0);

        // The cache is bounded: inserting another full bucket of distinct
        // expressions evicts the originals rather than growing without limit.
        const second = Array.from({ length: MAX_ENTRIES_PER_BUCKET }, (_, i) => `b${i}`);
        second.forEach(render);
        computes = 0;
        first.forEach(render);
        expect(computes).toBe(MAX_ENTRIES_PER_BUCKET); // all originals were evicted
    });
});

describe("mathjax cache on a large note (load + re-decorate)", () => {
    // Every <anki-mathjax> source in document order, with duplicates — what the
    // editor renders when it decorates the field.
    const note = readFileSync(
        join(__dirname, "../editor/perf-fixtures/large-cloze-note.html"),
        "utf8",
    );
    const sources = [...note.matchAll(/<anki-mathjax\b[^>]*>([\s\S]*?)<\/anki-mathjax>/g)]
        .map((m) => m[1]);
    const distinct = new Set(sources).size;

    function renderAll(countCompute: () => void): void {
        for (const src of sources) {
            getCachedMathjaxConversion(src, 20, 0, () => {
                countCompute();
                return conversion(src);
            });
        }
    }

    test("load renders each distinct expression once; re-decoration is free", () => {
        expect(distinct).toBeLessThan(sources.length); // the note has duplicates

        let loadComputes = 0;
        renderAll(() => loadComputes++);
        expect(loadComputes).toBe(distinct);

        // Simulate an undo / plain<->rich sync / reload: the whole field
        // re-decorates and re-renders. With a cache large enough to hold the
        // note, this costs zero tex2svg renders.
        let redecorateComputes = 0;
        renderAll(() => redecorateComputes++);
        expect(redecorateComputes).toBe(0);
    });
});
