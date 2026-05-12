// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { escapeMathjaxClozeEntities } from "./mathjax-cloze-entities";

describe("escapeMathjaxClozeEntities", () => {
    test("escapes trailing mathjax braces when clozing inside mathjax", () => {
        expect(escapeMathjaxClozeEntities("\\({{c1::\\frac{1}{2}}}\\)")).toBe(
            "\\({{c1::\\frac{1}{2&#125;}}\\)",
        );
    });

    test("escapes nested closing groups inside a mathjax cloze", () => {
        expect(
            escapeMathjaxClozeEntities("\\({{c1::\\sqrt{\\frac{a}{b}}}}\\)"),
        ).toBe("\\({{c1::\\sqrt{\\frac{a}{b&#125;&#125;}}\\)");
    });

    test("escapes nested closing groups when a cloze wraps mathjax", () => {
        expect(
            escapeMathjaxClozeEntities("{{c1::\\(\\sqrt{\\frac{a}{b}}\\)}}"),
        ).toBe("{{c1::\\(\\sqrt{\\frac{a}{b&#125;&#125;\\)}}");
    });

    test("escapes escaped literal right braces before the cloze suffix", () => {
        expect(escapeMathjaxClozeEntities("\\({{c1::\\}}}\\)")).toBe(
            "\\({{c1::\\&#125;}}\\)",
        );
    });

    test("escapes multiline block mathjax clozes", () => {
        expect(
            escapeMathjaxClozeEntities(
                "\\[{{c1::\\begin{aligned}\na &= b\\\\\n&= c\n\\end{aligned}}}\\]",
            ),
        ).toBe(
            "\\[{{c1::\\begin{aligned}\na &= b\\\\\n&= c\n\\end{aligned&#125;}}\\]",
        );
    });

    test("leaves non-clozed mathjax unchanged", () => {
        const input = "\\(\\sqrt{\\frac{a}{b}}\\)";
        expect(escapeMathjaxClozeEntities(input)).toBe(input);
    });

    test("is idempotent", () => {
        const input = "{{c1::\\(\\sqrt{\\frac{a}{b&#125;&#125;\\)}}";
        expect(escapeMathjaxClozeEntities(input)).toBe(input);
    });
});
