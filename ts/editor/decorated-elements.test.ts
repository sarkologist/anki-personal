// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import type * as Svelte from "svelte";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("svelte", async () => {
    const actual = await vi.importActual<typeof Svelte>("svelte");

    return {
        ...actual,
        mount: vi.fn(() => ({
            moveCaretAfter: vi.fn(),
            selectAll: vi.fn(),
        })),
        tick: vi.fn(() => Promise.resolve()),
    };
});

import { withAutoDecorationSuspended } from "../editable/decorated";
import { execCommandWithUndecoratedElements, undecorateFragment } from "./decorated-elements";
import { fragmentToStored } from "./rich-text-input/transform";

const hairlineSpace = "\u200a";

function inlineMathjax(source: string): string {
    return [
        "<anki-frame data-frames=\"anki-mathjax\" block=\"false\">",
        `<frame-start data-frames="anki-mathjax">${hairlineSpace}</frame-start>`,
        `<anki-mathjax contenteditable="false" decorated="true" data-mathjax="${source}">`,
        "<span data-anki=\"mathjax\" class=\"mathjax empty\"></span>",
        "</anki-mathjax>",
        `<frame-end data-frames="anki-mathjax">${hairlineSpace}</frame-end>`,
        "</anki-frame>",
    ].join("");
}

function blockMathjax(source: string): string {
    return [
        "<anki-frame data-frames=\"anki-mathjax\" block=\"true\">",
        `<frame-start data-frames="anki-mathjax">${hairlineSpace}</frame-start>`,
        `<anki-mathjax block="true" contenteditable="false" decorated="true" data-mathjax="${source}">`,
        "<span data-anki=\"mathjax\" class=\"mathjax block empty\"></span>",
        "</anki-mathjax>",
        `<frame-end data-frames="anki-mathjax">${hairlineSpace}</frame-end>`,
        "</anki-frame>",
    ].join("");
}

function inlineLegacyLatex(source: string): string {
    return [
        "<anki-frame data-frames=\"anki-latex\" block=\"false\">",
        `<frame-start data-frames="anki-latex">${hairlineSpace}</frame-start>`,
        `<anki-latex data-latex-kind="inline" data-latex="${source}" decorated>`,
        "<span class=\"legacy-latex-placeholder\">LaTeX</span>",
        "</anki-latex>",
        `<frame-end data-frames="anki-latex">${hairlineSpace}</frame-end>`,
        "</anki-frame>",
    ].join("");
}

function cloneContents(element: HTMLElement): DocumentFragment {
    const range = document.createRange();
    range.selectNodeContents(element);
    return range.cloneContents();
}

function storedAfterEditorNormalization(element: HTMLElement): string {
    const fragment = cloneContents(element);
    undecorateFragment(fragment);
    return fragmentToStored(fragment);
}

function makeSvg(): Element {
    const wrapper = document.createElement("span");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    const g = document.createElementNS("http://www.w3.org/2000/svg", "g");

    Object.defineProperty(svg, "viewBox", {
        value: { baseVal: { height: 20 } },
    });
    svg.append(g);
    wrapper.append(svg);

    return wrapper;
}

describe("decorated editor elements", () => {
    let execCommand: typeof document.execCommand | undefined;

    beforeEach(() => {
        execCommand = document.execCommand;
        vi.stubGlobal(
            "ResizeObserver",
            class {
                observe = vi.fn();
                disconnect = vi.fn();
            },
        );
        vi.stubGlobal("MathJax", {
            tex2svg: vi.fn(makeSvg),
        });
    });

    afterEach(() => {
        document.execCommand = execCommand!;
        document.body.replaceChildren();
        vi.unstubAllGlobals();
    });

    test("stores decorated MathJax previews from their source", () => {
        const fragment = document.createRange().createContextualFragment(
            [
                "before ",
                inlineMathjax("\\omega"),
                " and ",
                blockMathjax("x+y"),
                " after",
            ].join(""),
        );

        undecorateFragment(fragment);

        expect(fragment.querySelector("[data-anki=\"mathjax\"]")).toBeNull();
        expect(fragmentToStored(fragment)).toBe(
            "before \\(\\omega\\) and \\[x+y\\] after",
        );
    });

    test("runs indent with decorated elements exposed as source", () => {
        const base = document.createElement("div");
        withAutoDecorationSuspended(() => {
            base.innerHTML = [
                "A ",
                inlineMathjax("\\omega"),
                " B ",
                blockMathjax("x+y"),
                " C ",
                inlineLegacyLatex("z^2"),
                " D",
            ].join("");
            document.body.append(base);
        });

        const range = document.createRange();
        range.selectNodeContents(base);
        const selection = document.getSelection()!;
        selection.removeAllRanges();
        selection.addRange(range);

        document.execCommand = vi.fn((command: string) => {
            expect(command).toBe("indent");
            expect(
                [...base.querySelectorAll("anki-mathjax")].map(
                    (element) => element.innerHTML,
                ),
            ).toEqual(["\\omega", "x+y"]);
            expect(base.querySelector("[data-anki=\"mathjax\"]")).toBeNull();
            expect(base.querySelector("anki-latex")!.innerHTML).toBe("z^2");

            const blockquote = document.createElement("blockquote");
            blockquote.style.margin = "0 0 0 40px";
            blockquote.style.border = "none";
            blockquote.style.padding = "0px";
            selection.getRangeAt(0).surroundContents(blockquote);

            return true;
        });

        execCommandWithUndecoratedElements(base, "indent");

        expect(document.execCommand).toHaveBeenCalledOnce();
        expect(base.querySelector("anki-mathjax[decorated]")).not.toBeNull();

        const stored = storedAfterEditorNormalization(base);
        expect(stored).toContain("\\(\\omega\\)");
        expect(stored).toContain("\\[x+y\\]");
        expect(stored).toContain("[$]z^2[/$]");
        expect(stored).not.toContain("data-anki=\"mathjax\"");
        expect(stored).not.toContain("mathjax empty");
    });
});
