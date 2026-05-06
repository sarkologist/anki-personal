// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { getRange, getSelection } from "./cross-browser";

// Avoid range boundaries inside <frame-start>/<frame-end>: execCommand would
// insert the wrap text inside the handle, then the handle MutationObserver
// scrambles it.
function expandRangeOutOfFrameHandles(range: Range): boolean {
    let changed = false;
    const startHandle = nearestFrameHandle(range.startContainer);
    if (startHandle?.parentElement) {
        range.setStartBefore(startHandle.parentElement);
        changed = true;
    }
    const endHandle = nearestFrameHandle(range.endContainer);
    if (endHandle?.parentElement) {
        range.setEndAfter(endHandle.parentElement);
        changed = true;
    }
    return changed;
}

function nearestFrameHandle(node: Node): Element | null {
    let n: Node | null = node;
    while (n) {
        if (n.nodeType === Node.ELEMENT_NODE) {
            const tag = (n as Element).tagName;
            if (tag === "FRAME-START" || tag === "FRAME-END") {
                return n as Element;
            }
        }
        n = n.parentNode;
    }
    return null;
}

function wrappedExceptForWhitespace(text: string, front: string, back: string): string {
    const normalizedText = text
        .replace(/&nbsp;/g, " ")
        .replace(/&#160;/g, " ")
        .replace(/\u00A0/g, " ");

    const match = normalizedText.match(/^(\s*)([^]*?)(\s*)$/)!;
    return match[1] + front + match[2] + back + match[3];
}

function moveCursorInside(selection: Selection, postfix: string): void {
    const range = getRange(selection)!;

    range.setEnd(range.endContainer, range.endOffset - postfix.length);
    range.collapse(false);

    selection.removeAllRanges();
    selection.addRange(range);
}

export function wrapInternal(
    base: Element,
    front: string,
    back: string,
    plainText: boolean,
    normalize?: (fragment: DocumentFragment) => void,
): void {
    const selection = getSelection(base)!;
    const range = getRange(selection);

    if (!range) {
        return;
    }

    if (expandRangeOutOfFrameHandles(range)) {
        selection.removeAllRanges();
        selection.addRange(range);
    }
    const wasCollapsed = range.collapsed;
    const content = range.cloneContents();
    normalize?.(content);
    const span = document.createElement("span");
    span.appendChild(content);

    if (plainText) {
        const new_ = wrappedExceptForWhitespace(span.innerText, front, back);
        document.execCommand("inserttext", false, new_);
    } else {
        const new_ = wrappedExceptForWhitespace(span.innerHTML, front, back);
        document.execCommand("inserthtml", false, new_);
    }

    if (
        wasCollapsed
        /* ugly solution: treat <anki-mathjax> differently than other wraps */ && !front.includes(
            "<anki-mathjax",
        )
    ) {
        moveCursorInside(selection, back);
    }
}
