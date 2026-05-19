// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { afterEach, describe, expect, test } from "vitest";

import { plainTextAgentSelectionContext, richTextAgentSelectionContext } from "./agent-selection";

function setSelection(start: Node, startOffset: number, end: Node, endOffset: number): void {
    const range = document.createRange();
    range.setStart(start, startOffset);
    range.setEnd(end, endOffset);

    const selection = document.getSelection()!;
    selection.removeAllRanges();
    selection.addRange(range);
}

describe("agent selection context", () => {
    afterEach(() => {
        document.getSelection()?.removeAllRanges();
        document.body.replaceChildren();
    });

    test("captures rich-text selected text and stored html", () => {
        const editable = document.createElement("div");
        editable.innerHTML = "Before <strong>selected</strong> after";
        document.body.append(editable);

        const selected = editable.querySelector("strong")!;
        const range = document.createRange();
        range.selectNode(selected);

        const selection = document.getSelection()!;
        selection.removeAllRanges();
        selection.addRange(range);

        expect(richTextAgentSelectionContext(editable, "Front", 0)).toEqual({
            inside: true,
            context: {
                field_name: "Front",
                field_index: 0,
                input_kind: "rich_text",
                text: "selected",
                html: "<strong>selected</strong>",
            },
        });
    });

    test("clears rich-text context for collapsed in-field selections", () => {
        const editable = document.createElement("div");
        editable.textContent = "selected";
        document.body.append(editable);

        setSelection(editable.firstChild!, 3, editable.firstChild!, 3);

        expect(richTextAgentSelectionContext(editable, "Front", 0)).toEqual({
            inside: true,
            context: null,
        });
    });

    test("ignores selections outside the rich-text field", () => {
        const editable = document.createElement("div");
        editable.textContent = "field";
        const outside = document.createElement("div");
        outside.textContent = "outside";
        document.body.append(editable, outside);

        setSelection(outside.firstChild!, 0, outside.firstChild!, "outside".length);

        expect(richTextAgentSelectionContext(editable, "Front", 0)).toEqual({
            inside: false,
            context: null,
        });
    });

    test("captures plain-text source selections without html", () => {
        expect(plainTextAgentSelectionContext("<b>source</b>", "Back", 1)).toEqual({
            field_name: "Back",
            field_index: 1,
            input_kind: "plain_text",
            text: "<b>source</b>",
            html: null,
        });
        expect(plainTextAgentSelectionContext("", "Back", 1)).toBeNull();
    });
});
