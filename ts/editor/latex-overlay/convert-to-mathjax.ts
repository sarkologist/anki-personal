// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { Mathjax } from "../../editable/mathjax-element.svelte";

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

    return text;
}

export function legacyLatexToMathjaxElement(
    source: string,
    isDisplay: boolean,
): HTMLElement {
    const element = document.createElement(Mathjax.tagName);

    if (isDisplay) {
        element.setAttribute("block", "true");
    }

    element.textContent = normalizeLegacyLatexSource(source);
    return element;
}
