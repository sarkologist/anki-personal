// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { readFileSync } from "fs";
import { describe, expect, test } from "vitest";

describe("editable-base MathJax sizing", () => {
    test("lets MathJax render wider than the editor", () => {
        const editableCss = readFileSync("editable/editable-base.scss", "utf8");
        const selectorsWithNaturalWidth = Array.from(
            editableCss.matchAll(/([^{}]+)\{([^{}]*)\}/gu),
        )
            .filter(([, _selectors, body]) => /max-width:\s*none/u.test(body))
            .flatMap(([, selectors]) => selectors.split(",").map((selector) => selector.trim()));

        expect(selectorsWithNaturalWidth).toEqual(
            expect.arrayContaining([
                "anki-frame[data-frames=\"anki-mathjax\"]",
                "anki-mathjax",
                ".mathjax",
            ]),
        );
    });
});
