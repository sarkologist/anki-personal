// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The text a read-only field shows: the field's HTML decoded to plain text
 * (markup is dropped, never rendered), and pretty-printed when it is JSON.
 */
export function readOnlyText(html: string): string {
    // An inert document: nothing in it loads or runs.
    const body = new DOMParser().parseFromString(html, "text/html").body;
    // textContent drops line breaks; mark them first (innerText needs layout).
    for (const br of body.querySelectorAll("br")) {
        br.replaceWith("\n");
    }
    for (const block of body.querySelectorAll("div, p, li")) {
        if (block.previousSibling) {
            block.before("\n");
        }
    }
    const text = body.textContent ?? "";
    try {
        return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
        return text;
    }
}
