// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/** `chunked_list`: files are trashed 25 at a time, as Qt does. */
export function chunks<T>(items: T[], size: number): T[][] {
    const out: T[][] = [];
    for (let i = 0; i < items.length; i += size) {
        out.push(items.slice(i, i + size));
    }
    return out;
}

/** Whether "Render LaTeX" is offered: a missing file is a LaTeX image. */
export function missingLatex(missing: string[]): boolean {
    return missing.some((name) => name.startsWith("latex-"));
}
