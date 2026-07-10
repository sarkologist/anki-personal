// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { execCommand } from "$lib/domlib";
import { beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("$lib/components/WithState.svelte", () => ({
    updateAllState: vi.fn(),
}));

vi.mock("$lib/domlib", () => ({
    execCommand: vi.fn(),
}));

import { __testing, pasteHTML } from "./old-editor-adapter";

const { unwrapHeadingsWrappingBlocks, collectHeadingsWrappingBlocks } = __testing;

describe("pasteHTML", () => {
    beforeEach(() => {
        vi.mocked(execCommand).mockClear();
    });

    test("converts pasted MathJax delimiters into editable MathJax elements", () => {
        pasteHTML(
            [
                "<div>Let<br>",
                "\\[\n",
                "x^2+y^2=z^2\n",
                "\\]",
                "and \\(z\\).</div>",
            ].join(""),
            false,
            true,
        );

        expect(execCommand).toHaveBeenCalledWith(
            "inserthtml",
            false,
            [
                "<div>Let<br>",
                "<anki-mathjax block=\"true\">x^2+y^2=z^2</anki-mathjax>",
                "and <anki-mathjax>z</anki-mathjax>.</div>",
            ].join(""),
        );
    });

    test("converts pasted Markdown math elements into editable MathJax elements", () => {
        pasteHTML(
            [
                "<div class=\"math math-block\">x&lt;y</div>",
                "<span class=\"math math-inline\">\\(z\\)</span>",
            ].join(""),
            false,
            false,
        );

        expect(execCommand).toHaveBeenCalledWith(
            "inserthtml",
            false,
            [
                "<anki-mathjax block=\"true\">x&lt;y</anki-mathjax>",
                "<anki-mathjax>z</anki-mathjax>",
            ].join(""),
        );
    });
});

describe("unwrapHeadingsWrappingBlocks", () => {
    function normalize(html: string): string {
        const root = document.createElement("div");
        root.innerHTML = html;
        unwrapHeadingsWrappingBlocks(root);
        return root.innerHTML;
    }

    test("unwraps a heading that wraps a block element (paste corruption)", () => {
        // execCommand("insertHTML") nests the pasted paragraph inside a
        // duplicate heading; the inline MathJax inside would then inherit the
        // heading font size and drop onto its own line.
        expect(
            normalize(
                "<h1>Title</h1><h1><div>text <anki-mathjax>x</anki-mathjax></div></h1>",
            ),
        ).toBe("<h1>Title</h1><div>text <anki-mathjax>x</anki-mathjax></div>");
    });

    test("unwraps a heading wrapping a block-attributed element", () => {
        // A frame/element that is block only via its `block` attribute (not its
        // tag name) must still count as block, so `elementIsBlock` is used.
        const root = document.createElement("div");
        root.innerHTML = "<h2><anki-frame block=\"true\">x</anki-frame></h2>";

        unwrapHeadingsWrappingBlocks(root);

        expect(root.querySelector("h2")).toBeNull();
        expect(root.querySelector("anki-frame")?.parentElement).toBe(root);
    });

    test("unwraps nested block-wrapping headings", () => {
        expect(normalize("<h1><div><h2><div>x</div></h2></div></h1>")).toBe(
            "<div><div>x</div></div>",
        );
    });

    test("leaves a heading with only inline content untouched", () => {
        const html = "<h2>Intro <anki-mathjax>x</anki-mathjax> and <b>bold</b></h2>";
        expect(normalize(html)).toBe(html);
    });

    test("leaves a mixed heading (inline text + block) untouched to avoid data loss", () => {
        const html = "<h1>Title<div>body</div></h1>";
        expect(normalize(html)).toBe(html);
    });

    test("leaves a heading that legitimately follows a block untouched", () => {
        const html = "<div>para</div><h2>Heading</h2><div>more</div>";
        expect(normalize(html)).toBe(html);
    });

    test("does not rewrite a pre-existing offender the paste did not introduce", () => {
        const root = document.createElement("div");
        root.innerHTML = "<h1><div>old</div></h1><h2><div>pasted</div></h2>";

        // Snapshot offenders before the "paste"; the <h1> is pre-existing, so
        // only the freshly introduced <h2> should be unwrapped.
        const preexisting = collectHeadingsWrappingBlocks(root);
        root.querySelector("h2")!.remove();
        root.insertAdjacentHTML("beforeend", "<h2><div>pasted</div></h2>");
        unwrapHeadingsWrappingBlocks(root, preexisting);

        expect(root.innerHTML).toBe("<h1><div>old</div></h1><div>pasted</div>");
    });
});
