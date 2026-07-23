// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { writable } from "svelte/store";
import { beforeEach, describe, expect, test } from "vitest";

import { CodeMirror, setupCodeMirror } from "./code-mirror";

/**
 * CodeMirror measures layout via `getBoundingClientRect`/`getClientRects`,
 * which jsdom doesn't implement. The undo-history logic under test is pure
 * document manipulation, so stubbing these with empty rects is enough.
 */
beforeEach(() => {
    const rect = () => ({
        left: 0,
        right: 0,
        top: 0,
        bottom: 0,
        width: 0,
        height: 0,
        x: 0,
        y: 0,
    });
    const emptyRects = () => ({
        length: 0,
        item: () => null,
        [Symbol.iterator]: function*() {},
    });
    (Range.prototype as any).getBoundingClientRect = rect;
    (Range.prototype as any).getClientRects = emptyRects;
    (Element.prototype as any).getBoundingClientRect = rect;
    (HTMLElement.prototype as any).getClientRects = emptyRects;
    document.body.replaceChildren();
});

function makeEditor(): CodeMirror.Editor {
    const textarea = document.createElement("textarea");
    document.body.append(textarea);
    return CodeMirror.fromTextArea(textarea, {});
}

describe("setupCodeMirror undo history", () => {
    test("a store-driven populate is not an undoable step", () => {
        const code = writable("x");
        const editor = makeEditor();

        setupCodeMirror(editor, code);

        expect(editor.getValue()).toBe("x");
        expect(editor.getDoc().historySize().undo).toBe(0);
    });

    test("undoing right after open leaves the populated value intact", () => {
        const code = writable("\\alpha");
        const editor = makeEditor();
        setupCodeMirror(editor, code);

        editor.undo();

        // Without the fix, the initial setValue is undoable and this wipes the
        // editor to "" — which the overlay would mirror onto data-mathjax,
        // blanking the decorated MathJax block.
        expect(editor.getValue()).toBe("\\alpha");
    });

    test("undo reverts user edits back to — but not past — the baseline", () => {
        const code = writable("x");
        const editor = makeEditor();
        setupCodeMirror(editor, code);

        // Simulate the MathJax overlay's cloze button: select all + surround.
        editor.execCommand("selectAll");
        editor.replaceSelection(`{{c1::${editor.getSelection()}}}`);
        expect(editor.getValue()).toBe("{{c1::x}}");

        editor.undo();
        expect(editor.getValue()).toBe("x"); // cloze removed

        editor.undo();
        expect(editor.getValue()).toBe("x"); // cannot revert past the equation
    });

    test("later store-driven populates are also non-undoable baselines", () => {
        const code = writable("first");
        const editor = makeEditor();
        setupCodeMirror(editor, code);

        // An external update while the editor is unfocused re-populates it.
        code.set("second");
        expect(editor.getValue()).toBe("second");

        editor.undo();
        expect(editor.getValue()).toBe("second");
    });

    /**
     * End-to-end guard for the reported bug: the MathJax overlay mirrors the
     * editor's `code` onto the decorated element's `data-mathjax`. If undo
     * could empty the editor, it would blank the equation.
     */
    test("undo after adding a cloze does not blank the decorated block", () => {
        const mathjaxElement = document.createElement("anki-mathjax");
        mathjaxElement.dataset.mathjax = "x";

        const code = writable(mathjaxElement.dataset.mathjax ?? "");
        // overlay wiring: code -> data-mathjax
        code.subscribe((value) => {
            mathjaxElement.dataset.mathjax = value;
        });

        const editor = makeEditor();
        // overlay wiring: editor changes -> code
        editor.on("change", () => code.set(editor.getValue()));
        setupCodeMirror(editor, code);

        // add a cloze via the cloze button (select all + surround)
        editor.focus();
        editor.execCommand("selectAll");
        editor.replaceSelection(`{{c1::${editor.getSelection()}}}`);
        expect(mathjaxElement.dataset.mathjax).toBe("{{c1::x}}");

        editor.undo();
        expect(mathjaxElement.dataset.mathjax).toBe("x");

        // A further undo must not wipe the equation to empty.
        editor.undo();
        expect(mathjaxElement.dataset.mathjax).toBe("x");
    });
});
