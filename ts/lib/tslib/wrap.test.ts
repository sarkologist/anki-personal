// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { wrapInternal } from "./wrap";

const hairlineSpace = "\u200a";

function insertHtmlAtSelection(_command: string, _showUI?: boolean, value?: string): boolean {
    const selection = document.getSelection()!;
    const range = selection.getRangeAt(0);
    const fragment = range.createContextualFragment(value ?? "");
    const lastChild = fragment.lastChild;

    range.deleteContents();
    range.insertNode(fragment);

    if (lastChild) {
        range.setStartAfter(lastChild);
        range.collapse(true);
        selection.removeAllRanges();
        selection.addRange(range);
    }

    return true;
}

function undecorateMathjax(fragment: DocumentFragment): void {
    for (const element of fragment.querySelectorAll("anki-mathjax")) {
        if (element.parentElement?.tagName === "ANKI-FRAME") {
            element.parentElement.replaceWith(element);
        }

        element.innerHTML = (element as HTMLElement).dataset.mathjax ?? "";
        delete (element as HTMLElement).dataset.mathjax;
        element.removeAttribute("contenteditable");
        element.removeAttribute("decorated");
    }
}

describe("wrapInternal", () => {
    let execCommand: typeof document.execCommand | undefined;

    beforeEach(() => {
        execCommand = document.execCommand;
        document.execCommand = vi.fn(insertHtmlAtSelection);
    });

    afterEach(() => {
        document.execCommand = execCommand!;
    });

    test("wraps a whole frame when the selection is inside a frame handle", () => {
        const base = document.createElement("div");
        base.innerHTML = `test&nbsp;<anki-frame data-frames="anki-mathjax">`
            + `<frame-start data-frames="anki-mathjax">${hairlineSpace}</frame-start>`
            + `<anki-mathjax contenteditable="false" decorated="true" data-mathjax="1">`
            + `<img data-anki="mathjax">`
            + `</anki-mathjax>`
            + `<frame-end data-frames="anki-mathjax">${hairlineSpace}</frame-end>`
            + `</anki-frame>.`;
        document.body.append(base);

        const range = new Range();
        range.setStart(base.querySelector("frame-start")!.firstChild!, 1);
        range.collapse(true);

        const selection = document.getSelection()!;
        selection.removeAllRanges();
        selection.addRange(range);

        wrapInternal(base, "{{c1::", "}}", false, undecorateMathjax);

        expect(base.innerHTML).toBe(
            "test&nbsp;{{c1::<anki-mathjax>1</anki-mathjax>}}.",
        );
    });
});
