// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { fragmentToStored } from "../rich-text-input/transform";
import {
    convertLegacyLatexToInlineMathjax,
    legacyLatexToMathjaxElement,
    normalizeLegacyLatexSource,
} from "./convert-to-mathjax";

function makeSvg(source: string): Element {
    const wrapper = document.createElement("span");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");

    if (source.includes("tikzpicture") || source.includes("invalid")) {
        const error = document.createElementNS("http://www.w3.org/2000/svg", "g");
        error.setAttribute("data-mjx-error", "true");
        svg.append(error);
    }

    wrapper.append(svg);
    return wrapper;
}

function storedAfterConversion(source: string, isDisplay: boolean): string | null {
    const fragment = document.createDocumentFragment();
    const element = legacyLatexToMathjaxElement(source, isDisplay);
    if (!element) {
        return null;
    }
    fragment.append(element);
    return fragmentToStored(fragment);
}

describe("legacyLatexToMathjaxElement", () => {
    beforeEach(() => {
        vi.stubGlobal("MathJax", {
            tex2svg: vi.fn(makeSvg),
        });
    });

    afterEach(() => {
        vi.unstubAllGlobals();
    });

    test("converts inline legacy LaTeX to inline MathJax", () => {
        expect(storedAfterConversion("x^2", false)).toBe("\\(x^2\\)");
    });

    test("converts display legacy LaTeX to inline MathJax", () => {
        expect(storedAfterConversion("x+y", true)).toBe("\\(x+y\\)");
    });

    test("leaves invalid MathJax as legacy LaTeX", () => {
        expect(storedAfterConversion("\\begin{tikzpicture}\\end{tikzpicture}", false))
            .toBeNull();
    });

    test("normalizes entities, formatting tags, and legacy line breaks", () => {
        expect(
            normalizeLegacyLatexSource("<b>x</b>&amp;y<br>z<div>w</div>"),
        ).toBe("x&y\nz\nw");
    });

    test("normalizes non-breaking spaces to ordinary spaces", () => {
        expect(normalizeLegacyLatexSource("x&nbsp;y\u00a0z&amp;nbsp;w")).toBe(
            "x y z w",
        );
    });

    test("converts legacy delimiters in stored field HTML", () => {
        expect(
            convertLegacyLatexToInlineMathjax(
                "a [$]x[/$] b [$$]y[/$$] c [latex]z[/latex]",
            ),
        ).toBe("a \\(x\\) b \\(y\\) c \\(z\\)");
    });

    test("converts editor legacy latex tags in stored field HTML", () => {
        expect(
            convertLegacyLatexToInlineMathjax(
                "<anki-latex data-latex-kind=\"display\">x+y</anki-latex>",
            ),
        ).toBe("\\(x+y\\)");
    });

    test("escapes normalized MathJax source for stored field HTML", () => {
        expect(convertLegacyLatexToInlineMathjax("[$]<b>x</b>&amp;y[/$]")).toBe(
            "\\(x&amp;y\\)",
        );
    });

    test("preserves invalid legacy LaTeX while converting valid snippets", () => {
        expect(
            convertLegacyLatexToInlineMathjax(
                "[$]x[/$] [latex]\\begin{tikzpicture}\\end{tikzpicture}[/latex]",
            ),
        ).toBe("\\(x\\) [latex]\\begin{tikzpicture}\\end{tikzpicture}[/latex]");
    });

    test("preserves snippets when MathJax throws", () => {
        vi.stubGlobal("MathJax", {
            tex2svg: vi.fn(() => {
                throw new Error("bad math");
            }),
        });

        expect(convertLegacyLatexToInlineMathjax("[$]x[/$]")).toBe("[$]x[/$]");
    });

    test("supports whole-note conversion by transforming each stored field", () => {
        const fields = [
            "front [$]x[/$]",
            "back [latex]invalid[/latex]",
            "extra",
        ];

        expect(fields.map(convertLegacyLatexToInlineMathjax)).toEqual([
            "front \\(x\\)",
            "back [latex]invalid[/latex]",
            "extra",
        ]);
    });
});
