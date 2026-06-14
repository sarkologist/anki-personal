// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { Mathjax } from "../../editable/mathjax-element.svelte";

const legacyLatexTagPattern = /<anki-latex\b[^>]*>(.*?)<\/anki-latex>/gisu;
const legacyLatexPatterns = [
    /\[\$\$\](.*?)\[\/\$\$\]/gsu,
    /\[\$\](.*?)\[\/\$\]/gsu,
    /\[latex\](.*?)\[\/latex\]/gisu,
    legacyLatexTagPattern,
];

function textContentWithLegacyBreaks(node: Node): string {
    if (node.nodeType === Node.TEXT_NODE) {
        return node.textContent ?? "";
    }

    if (node.nodeType !== Node.ELEMENT_NODE) {
        return "";
    }

    const element = node as Element;
    if (element.tagName === "BR") {
        return "\n";
    }

    let text = element.tagName === "DIV" ? "\n" : "";

    for (const child of element.childNodes) {
        text += textContentWithLegacyBreaks(child);
    }

    return text;
}

export function normalizeLegacyLatexSource(source: string): string {
    const container = document.createElement("div");
    container.innerHTML = source;

    let text = "";
    for (const child of container.childNodes) {
        text += textContentWithLegacyBreaks(child);
    }

    return text.replace(/\u00a0/g, " ").replace(/&nbsp;/giu, " ");
}

/**
 * Collapse a wrapped selection down to a single multiline text node.
 *
 * `<anki-latex>` is an inline element, so the browser hoists block children
 * (e.g. the `<div>`s a multiline selection is made of) out of it when it is
 * inserted, tearing the LaTeX source apart. Converting line breaks to `\n`
 * (matching how the backend normalizes the source) and dropping the markup
 * leaves nothing for the browser to split, so multiline LaTeX stays in one
 * element. Newlines are preserved via the element's `white-space: pre`.
 */
export function flattenBlocksToNewlines(fragment: DocumentFragment): void {
    let text = "";
    for (const child of fragment.childNodes) {
        text += textContentWithLegacyBreaks(child);
    }

    text = text
        .replace(/\u00a0/g, " ")
        .replace(/^\n*/, "")
        .replace(/\n*$/, "");

    fragment.replaceChildren(document.createTextNode(text));
}

function escapeMathjaxSourceForStoredHtml(source: string): string {
    return source.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function legacyLatexIsValidInlineMathjax(source: string): boolean {
    const mathjax = (globalThis as any).MathJax;
    if (!mathjax?.tex2svg) {
        return false;
    }

    try {
        const output = mathjax.tex2svg(source) as Element;
        return !output.innerHTML.includes("data-mjx-error");
    } catch {
        return false;
    }
}

export function legacyLatexToInlineMathjax(source: string): string | null {
    const normalized = normalizeLegacyLatexSource(source);
    if (!legacyLatexIsValidInlineMathjax(normalized)) {
        return null;
    }

    return `\\(${escapeMathjaxSourceForStoredHtml(normalized)}\\)`;
}

export function convertLegacyLatexToInlineMathjax(storedHtml: string): string {
    let converted = storedHtml;

    for (const pattern of legacyLatexPatterns) {
        converted = converted.replace(pattern, (match: string, source: string) => {
            return legacyLatexToInlineMathjax(source) ?? match;
        });
    }

    return converted;
}

export function legacyLatexToMathjaxElement(
    source: string,
    _isDisplay = false,
): HTMLElement | null {
    const normalized = normalizeLegacyLatexSource(source);
    if (!legacyLatexIsValidInlineMathjax(normalized)) {
        return null;
    }

    const element = document.createElement(Mathjax.tagName);
    element.textContent = normalized;
    return element;
}
