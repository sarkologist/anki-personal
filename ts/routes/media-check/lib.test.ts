// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { chunks, missingLatex } from "./lib";

test("chunks", () => {
    expect(chunks([1, 2, 3, 4, 5], 2)).toEqual([[1, 2], [3, 4], [5]]);
    expect(chunks([], 25)).toEqual([]);
});

test("missing LaTeX images", () => {
    expect(missingLatex(["a.png", "latex-0a1b.png"])).toBe(true);
    expect(missingLatex(["my-latex-notes.png"])).toBe(false);
});
