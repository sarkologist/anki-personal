// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { describe, expect, test } from "vitest";

import { fragmentToStored } from "../rich-text-input/transform";
import { legacyLatexToMathjaxElement, normalizeLegacyLatexSource } from "./convert-to-mathjax";

function storedAfterConversion(source: string, isDisplay: boolean): string {
    const fragment = document.createDocumentFragment();
    fragment.append(legacyLatexToMathjaxElement(source, isDisplay));
    return fragmentToStored(fragment);
}

describe("legacyLatexToMathjaxElement", () => {
    test("converts inline legacy LaTeX to inline MathJax", () => {
        expect(storedAfterConversion("x^2", false)).toBe("\\(x^2\\)");
    });

    test("converts display legacy LaTeX to block MathJax", () => {
        expect(storedAfterConversion("x+y", true)).toBe("\\[x+y\\]");
    });

    test("normalizes entities, formatting tags, and legacy line breaks", () => {
        expect(
            normalizeLegacyLatexSource("<b>x</b>&amp;y<br>z<div>w</div>"),
        ).toBe("x&y\nz\nw");
    });
});
