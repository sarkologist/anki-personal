// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import "mathjax/es5/tex-svg-full";

import { describe, expect, test } from "vitest";

import { revealMathjaxClozeAnswers } from "./mathjax-cloze";

function rendersWithoutError(tex: string): boolean {
    const out = (globalThis as any).MathJax.tex2svg(tex) as Element;
    return !out.innerHTML.includes("data-mjx-error");
}

describe("revealed mathjax clozes render", () => {
    // Regression for "Bracket argument to \\ must be a dimension": a cloze on
    // the line after a `\\` used to reveal as `\\[…]`, which MathJax read as an
    // optional row-spacing argument.
    test("cloze on the 2nd line of a \\substack", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`S=\sum_{\substack{x\bmod c\\{{c1::x\bar x\equiv 1\,(c)}}}}e`,
        );
        expect(revealed).not.toContain(String.raw`\\[`);
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze as a subscript (would mis-parse as subscript of `[`)", () => {
        const revealed = revealMathjaxClozeAnswers(String.raw`a_{{c1::x}}`);
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze inside a \\frac numerator", () => {
        const revealed = revealMathjaxClozeAnswers(String.raw`\frac{{{c1::a}}}{b}`);
        expect(rendersWithoutError(revealed)).toBe(true);
    });
});
