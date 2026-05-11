// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

function trimBreaks(text: string): string {
    return text.replace(/^\n*/, "").replace(/\n*$/, "");
}

function mathjaxText(element: HTMLElement): string {
    if (typeof element.dataset.mathjax === "string") {
        return trimBreaks(element.dataset.mathjax);
    }

    const clone = element.cloneNode(true) as HTMLElement;
    for (const br of clone.querySelectorAll("br")) {
        br.replaceWith("\n");
    }

    return trimBreaks(clone.textContent ?? "");
}

function delimiterFor(element: HTMLElement): ["\\(" | "\\[", "\\)" | "\\]"] {
    const block = element.getAttribute("block");
    return typeof block === "string" && block !== "false" ? ["\\[", "\\]"] : ["\\(", "\\)"];
}

export function replaceEditorMathjaxElements(parent: ParentNode): void {
    for (const element of parent.querySelectorAll<HTMLElement>("anki-mathjax")) {
        const [open, close] = delimiterFor(element);
        element.replaceWith(document.createTextNode(`${open}${mathjaxText(element)}${close}`));
    }
}
