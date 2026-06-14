// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { wrapInternal } from "@tslib/wrap";

import { fragmentToStored } from "../rich-text-input/transform";
import {
    convertLegacyLatexToInlineMathjax,
    flattenBlocksToNewlines,
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

function fragmentFromHtml(html: string): DocumentFragment {
    const fragment = document.createDocumentFragment();
    const container = document.createElement("div");
    container.innerHTML = html;
    while (container.firstChild) {
        fragment.appendChild(container.firstChild);
    }
    return fragment;
}

describe("flattenBlocksToNewlines", () => {
    test("collapses block and line breaks to newlines", () => {
        const fragment = fragmentFromHtml(
            "\\begin{tikzcd}&nbsp;<div>&amp; 1 \\\\</div><div>\\end{tikzcd}</div>",
        );

        flattenBlocksToNewlines(fragment);

        expect(fragment.childNodes.length).toBe(1);
        expect(fragment.textContent).toBe("\\begin{tikzcd} \n& 1 \\\\\n\\end{tikzcd}");
    });

    test("leaves single-line content untouched aside from stripping markup", () => {
        const fragment = fragmentFromHtml("<b>x</b>^2");

        flattenBlocksToNewlines(fragment);

        expect(fragment.textContent).toBe("x^2");
    });
});

describe("wrapping a multiline selection as legacy LaTeX", () => {
    let execCommand: typeof document.execCommand | undefined;
    let inserted = "";

    beforeEach(() => {
        execCommand = document.execCommand;
        inserted = "";
        document.execCommand = vi.fn((_command, _showUi, value) => {
            inserted = value ?? "";
            return true;
        });
    });

    afterEach(() => {
        document.execCommand = execCommand!;
    });

    function wrapSelectionInLatex(html: string): string {
        const base = document.createElement("div");
        base.innerHTML = html;
        document.body.append(base);

        const range = new Range();
        range.selectNodeContents(base);
        const selection = document.getSelection()!;
        selection.removeAllRanges();
        selection.addRange(range);

        wrapInternal(
            base,
            "<anki-latex data-latex-kind=\"display\">",
            "</anki-latex>",
            false,
            flattenBlocksToNewlines,
        );

        base.remove();
        return inserted;
    }

    test("keeps the source in one element, separated by newlines", () => {
        expect(
            wrapSelectionInLatex(
                "\\begin{tikzcd}<div>&amp; 1 \\\\</div><div>\\end{tikzcd}</div>",
            ),
        ).toBe(
            "<anki-latex data-latex-kind=\"display\">"
                + "\\begin{tikzcd}\n&amp; 1 \\\\\n\\end{tikzcd}"
                + "</anki-latex>",
        );
    });

    test("does not leak block elements out of the inline element", () => {
        const result = wrapSelectionInLatex("a<div>b</div><div>c</div>");

        expect(result).not.toContain("<div>");
        expect(result).toBe(
            "<anki-latex data-latex-kind=\"display\">a\nb\nc</anki-latex>",
        );
    });
});
