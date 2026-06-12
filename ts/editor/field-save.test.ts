// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { writable } from "svelte/store";
import { expect, test } from "vitest";

import { saveFieldsCommand } from "./field-save";

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
