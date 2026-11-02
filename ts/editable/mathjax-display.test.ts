// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, describe, expect, test, vi } from "vitest";

import { convertMathjax } from "./mathjax";

// A minimal stand-in for MathJax's tex2svg output. jsdom doesn't implement the
// SVG DOM (`viewBox.baseVal`), so we fake just enough for convertMathjax to run
// and record the options it was called with.
function fakeMathjax() {
    const tex2svg = vi.fn((_input: string, _options?: { display: boolean }) => {
        const svg = {
            viewBox: { baseVal: { height: 20 } },
            innerHTML: "",
            children: [{}],
            insertBefore: () => {},
            outerHTML: "<svg></svg>",
            querySelector: () => null,
        };
        return { children: [svg] };
    });
    return { tex2svg };
}

afterEach(() => {
    delete (globalThis as any).MathJax;
});

describe("convertMathjax threads the block flag into MathJax's display style", () => {
    // MathJax's tex2svg defaults to `display: true`. Without passing the flag,
    // inline math (`\sum`, `\int`, …) renders in display style and swells its
    // text line — while block math must stay in display style. Regression for
    // the editor rendering all math in display style regardless of `block`.
    test("passes display: true for block math", () => {
        const mathjax = fakeMathjax();
        (globalThis as any).MathJax = mathjax;

        convertMathjax("\\sum_n a(n)", 20, true);

        expect(mathjax.tex2svg).toHaveBeenCalledWith("\\sum_n a(n)", { display: true });
    });

    test("passes display: false for inline math", () => {
        const mathjax = fakeMathjax();
        (globalThis as any).MathJax = mathjax;

        convertMathjax("\\sum_n a(n)", 20, false);

        expect(mathjax.tex2svg).toHaveBeenCalledWith("\\sum_n a(n)", { display: false });
    });
});
