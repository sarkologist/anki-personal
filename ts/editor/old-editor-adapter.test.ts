// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { execCommand } from "$lib/domlib";
import { describe, expect, test, vi } from "vitest";

vi.mock("$lib/components/WithState.svelte", () => ({
    updateAllState: vi.fn(),
}));

vi.mock("$lib/domlib", () => ({
    execCommand: vi.fn(),
}));

import { pasteHTML } from "./old-editor-adapter";

describe("pasteHTML", () => {
    test("converts pasted MathJax delimiters into editable MathJax elements", () => {
        pasteHTML(
            [
                "<div>Let<br>",
                "\\[\n",
                "x^2+y^2=z^2\n",
                "\\]",
                "and \\(z\\).</div>",
            ].join(""),
            false,
            true,
        );

        expect(execCommand).toHaveBeenCalledWith(
            "inserthtml",
            false,
            [
                "<div>Let<br>",
                "<anki-mathjax block=\"true\">x^2+y^2=z^2</anki-mathjax>",
                "and <anki-mathjax>z</anki-mathjax>.</div>",
            ].join(""),
        );
    });
});
