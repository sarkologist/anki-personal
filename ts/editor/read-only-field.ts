// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * The text a read-only field shows: the field's HTML decoded to plain text
 * (markup is dropped, never rendered), and pretty-printed when it is JSON.
 */
export function readOnlyText(html: string): string {
    // An inert document: nothing in it loads or runs.
    const text = new DOMParser().parseFromString(html, "text/html").body.textContent ?? "";
    try {
        return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
        return text;
    }
}
