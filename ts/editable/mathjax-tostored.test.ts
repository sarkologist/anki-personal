// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { __testing, Mathjax } from "./mathjax-element.svelte";

/**
 * Frozen copy of the ORIGINAL decorated->stored conversion (per-frame
 * `<template>` parse). This is the correctness oracle: the optimized
 * `Mathjax.toStored` must be byte-identical to running this over the same
 * regexes.
 */
const mathjaxFramePattern =
    /<anki-frame\b(?=[^>]*\bdata-frames=(?:"anki-mathjax"|'anki-mathjax'|anki-mathjax))[^>]*>.*?<\/anki-frame>/gsu;
const mathjaxTagPattern = /<anki-mathjax\b[^>]*>.*?<\/anki-mathjax>/gsu;

function trimBreaks(text: string): string {
    return text.replace(/<br[ ]*\/?>/gsu, "\n").replace(/^\n*/, "").replace(/\n*$/, "");
}

function oracleOne(html: string): string {
    const template = document.createElement("template");
    template.innerHTML = html;
    const element = template.content.querySelector<HTMLElement>("anki-mathjax");
    if (!element) {
        return html;
    }
    const source = typeof element.dataset.mathjax === "string"
        ? element.dataset.mathjax
        : element.innerHTML;
    const trimmed = trimBreaks(source);
    const block = element.getAttribute("block")
        ?? element.closest("anki-frame")?.getAttribute("block");
    const isBlock = typeof block === "string" && block !== "false";
    return isBlock ? `\\[${trimmed}\\]` : `\\(${trimmed}\\)`;
}

function oracleToStored(undecorated: string): string {
    return undecorated
        .replace(mathjaxFramePattern, oracleOne)
        .replace(mathjaxTagPattern, oracleOne);
}

const hairline = " ";

/** Build the decorated HTML the browser holds, faithfully, via the DOM. */
function decoratedFrame(
    source: string,
    { block, frame = true, tagBlock }: {
        block?: boolean;
        frame?: boolean;
        tagBlock?: boolean;
    } = {},
): string {
    const container = document.createElement("div");
    const mathjax = document.createElement("anki-mathjax");
    mathjax.dataset.mathjax = source;
    mathjax.setAttribute("contenteditable", "false");
    mathjax.setAttribute("decorated", "true");
    if (tagBlock !== undefined) {
        mathjax.setAttribute("block", String(tagBlock));
    }
    mathjax.innerHTML = "<span data-anki=\"mathjax\" class=\"mathjax\"></span>";

    if (frame) {
        const frameEl = document.createElement("anki-frame");
        frameEl.setAttribute("data-frames", "anki-mathjax");
        frameEl.setAttribute("block", String(block ?? false));
        const start = document.createElement("frame-start");
        start.setAttribute("data-frames", "anki-mathjax");
        start.textContent = hairline;
        const end = document.createElement("frame-end");
        end.setAttribute("data-frames", "anki-mathjax");
        end.textContent = hairline;
        frameEl.append(start, mathjax, end);
        container.append(frameEl);
    } else {
        if (block !== undefined) {
            mathjax.setAttribute("block", String(block));
        }
        container.append(mathjax);
    }
    return container.innerHTML;
}

describe("Mathjax.toStored matches the original per-frame parser", () => {
    test("fast path is actually taken for clean inline/block frames", () => {
        // guard against the equivalence tests passing only because the fast
        // path always bails to the slow one
        expect(__testing.fastMathjaxHtmlToStored(decoratedFrame("\\alpha")))
            .toBe("\\(\\alpha\\)");
        expect(__testing.fastMathjaxHtmlToStored(decoratedFrame("x^2", { block: true })))
            .toBe("\\[x^2\\]");
    });

    test.each([
        "\\Delta",
        "\\beta_{\\min}(\\Delta)",
        "a &amp; b",
        "x &lt; y",
        "\\text{if } a > b",
        "\\{ x \\}",
        "{{c1::\\alpha}}",
        "\\color{red}{x}",
        "α + β",
        "\\frac{1}{2}",
        "",
        "   ",
    ])("inline frame: %j", (src) => {
        const input = decoratedFrame(src);
        expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
    });

    test.each([true, false])("block=%s frames", (block) => {
        const input = decoratedFrame("\\sum_{i=0}^n i", { block });
        expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
    });

    test("multiline (br) source falls back but stays correct", () => {
        const container = document.createElement("div");
        const mathjax = document.createElement("anki-mathjax");
        // runtime stores innerHTML, which for multiline math contains <br>
        mathjax.dataset.mathjax = "x<br>y<br>z";
        mathjax.setAttribute("decorated", "true");
        const frameEl = document.createElement("anki-frame");
        frameEl.setAttribute("data-frames", "anki-mathjax");
        frameEl.setAttribute("block", "true");
        frameEl.append(mathjax);
        container.append(frameEl);
        const input = container.innerHTML;
        expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
    });

    test("bare tag with tag-level block", () => {
        const input = decoratedFrame("z^3", { frame: false, block: true });
        expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
    });

    test("tag-level block overrides frame block", () => {
        const input = decoratedFrame("w", { block: false, tagBlock: true });
        expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
    });

    test.each([
        // entity-encoded block values must decode like the parser does
        "<anki-mathjax data-mathjax=\"x\" block=\"&#102;alse\"></anki-mathjax>",
        "<anki-mathjax data-mathjax=\"x\" block=\"&#x66;alse\"></anki-mathjax>",
        "<anki-mathjax data-mathjax=\"x\" block=\"fals&#101;\"></anki-mathjax>",
        "<anki-mathjax data-mathjax=\"x\" block=\"true\"></anki-mathjax>",
        "<anki-mathjax data-mathjax=\"x\" block=\"\"></anki-mathjax>",
        "<anki-frame data-frames=\"anki-mathjax\" block=\"&#102;alse\">"
        + "<anki-mathjax data-mathjax=\"x\"></anki-mathjax></anki-frame>",
    ])("entity-encoded block attribute: %j", (input) => {
        expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
    });

    test("multiple frames plus surrounding markup", () => {
        const input = "Before <strong>bold</strong> "
            + decoratedFrame("\\Delta")
            + " middle &amp; text "
            + decoratedFrame("\\beta", { block: true })
            + " x < y after";
        expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
    });

    // Each iteration builds decorated HTML through jsdom and runs it past both
    // the optimized converter and the DOM oracle, so the loop is dominated by
    // jsdom parsing cost. 1500 iterations keep broad coverage while staying well
    // clear of the timeout on slower, contended CI runners; the generous explicit
    // timeout is headroom, not the expected runtime (~0.4s locally).
    test("fuzz: random sources, orders and contexts stay byte-identical", () => {
        const chars = [
            "a",
            "b",
            "\\",
            "{",
            "}",
            "^",
            "_",
            "<",
            ">",
            "&",
            "\"",
            " ",
            "\n",
            "&amp;",
            "&lt;",
            "&gt;",
            "&#125;",
            "&nbsp;",
            "α",
            "Δ",
            "{{c1::",
            "}}",
            "\\alpha",
            "\\frac",
            "[",
            "]",
            "(",
            ")",
        ];
        function randomSource(rng: () => number): string {
            const len = Math.floor(rng() * 12);
            let out = "";
            for (let i = 0; i < len; i++) {
                out += chars[Math.floor(rng() * chars.length)];
            }
            return out;
        }
        // deterministic LCG so failures reproduce
        let seed = 0x9e3779b9 >>> 0;
        const rng = () => {
            seed = (seed * 1103515245 + 12345) & 0x7fffffff;
            return seed / 0x7fffffff;
        };

        for (let i = 0; i < 1500; i++) {
            const parts: string[] = [];
            const n = 1 + Math.floor(rng() * 3);
            for (let j = 0; j < n; j++) {
                const roll = rng();
                const src = randomSource(rng);
                if (roll < 0.5) {
                    parts.push(decoratedFrame(src, { block: rng() < 0.5 }));
                } else if (roll < 0.75) {
                    parts.push(decoratedFrame(src, { frame: false, block: rng() < 0.5 }));
                } else if (roll < 0.9) {
                    parts.push(decoratedFrame(src, { block: false, tagBlock: rng() < 0.5 }));
                } else {
                    parts.push(`plain ${src} <em>x</em>`);
                }
            }
            const input = parts.join(" text & <b>more</b> ");
            expect(Mathjax.toStored(input)).toBe(oracleToStored(input));
        }
    }, 20000);
});
