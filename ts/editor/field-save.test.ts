// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { writable } from "svelte/store";
import { expect, test } from "vitest";

import { fieldContentToSave, saveFieldsCommand } from "./field-save";

test("batch save includes converted and otherwise pending fields", () => {
    const fields = [
        writable("<anki-mathjax>x</anki-mathjax>"),
        writable("pending unconverted edit"),
        writable("<img data-editor-shrink=\"true\">"),
    ];

    expect(saveFieldsCommand(123, fields)).toBe(
        "saveFields:123:[\"<anki-mathjax>x</anki-mathjax>\",\"pending unconverted edit\",\"<img>\"]",
    );
});

test("a read-only field is saved as loaded, not as the editor serializes it", () => {
    const loaded = ["a", "{&quot;k&quot;: &#x27;v&#x27;}"];
    // what the hidden input's store holds after a DOM round trip
    const fields = [writable("a edited"), writable("{\"k\": 'v'}")];
    const keep = { readOnly: [false, true], loaded };

    expect(saveFieldsCommand(1, fields, keep)).toBe(
        `saveFields:1:${JSON.stringify(["a edited", loaded[1]])}`,
    );
    expect(fieldContentToSave(1, "{\"k\": 'v'}", keep)).toBe(loaded[1]);
    expect(fieldContentToSave(0, "<img data-editor-shrink=\"true\">", keep)).toBe("<img>");
});
