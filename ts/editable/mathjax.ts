// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/* eslint
@typescript-eslint/no-explicit-any: "off",
 */

import "mathjax/es5/tex-svg-full";

import mathIcon from "@mdi/svg/svg/math-integral-box.svg?src";

import { revealMathjaxClozeAnswers } from "./mathjax-cloze";

const parser = new DOMParser();

function getCSS(fontSize: number): string {
    /* `color` is set for Maths, `fill` for the empty icon. Both use
     * `currentColor` so the rendered glyphs inherit colour from whichever
     * ancestor (e.g. a `<span style="color: red">` from the text-colour
     * button) is in effect on the host of the inline SVG. Per-glyph fills
     * emitted by `\color{...}` in the LaTeX source still override this. */
    return `svg { color: currentColor; fill: currentColor; font-size: ${fontSize}px; }`;
}

function getStyle(css: string): HTMLStyleElement {
    const style = document.createElement("style");
    style.appendChild(document.createTextNode(css));
    return style;
}

function getEmptyIcon(style: HTMLStyleElement): [string, string] {
    const icon = parser.parseFromString(mathIcon, "image/svg+xml");
    const svg = icon.children[0];
    svg.insertBefore(style, svg.children[0]);

    return [svg.outerHTML, "MathJax"];
}

export function convertMathjax(
    input: string,
    fontSize: number,
): [string, string] {
    input = revealClozeAnswers(input);
    // The SVG is rendered inline inside a shadow root on <anki-mathjax>, so
    // the notetype CSS is injected there (not into this string). Inheritable
    // properties — `color` (for `currentColor`), `font-family`, custom
    // properties — cross the shadow boundary naturally; opaque selectors in
    // the notetype CSS are scoped to the shadow root and can't bleed into
    // the editor's own DOM.
    const style = getStyle(getCSS(fontSize));

    if (input.trim().length === 0) {
        return getEmptyIcon(style);
    }

    let output: Element;
    try {
        output = globalThis.MathJax.tex2svg(input);
    } catch (e) {
        return ["Mathjax Error", String(e)];
    }

    const svg = output.children[0] as SVGElement;

    if ((svg as any).viewBox.baseVal.height === 16) {
        return getEmptyIcon(style);
    }

    let title = "";

    if (svg.innerHTML.includes("data-mjx-error")) {
        svg.querySelector("rect")?.setAttribute("fill", "yellow");
        svg.querySelector("text")?.setAttribute("color", "red");
        title = svg.querySelector("title")?.innerHTML ?? "";
    } else {
        svg.insertBefore(style, svg.children[0]);
    }

    return [svg.outerHTML, title];
}

/**
 * Escape characters which are technically legal in Mathjax, but confuse HTML.
 */
export function escapeSomeEntities(value: string): string {
    return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function unescapeSomeEntities(value: string): string {
    return value.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}

function revealClozeAnswers(input: string): string {
    return revealMathjaxClozeAnswers(input);
}
