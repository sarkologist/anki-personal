// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { revealMathjaxClozeAnswers } from "./mathjax-cloze";

describe("revealMathjaxClozeAnswers", () => {
    test("reveals an escaped literal right brace inside a cloze", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::\{1\}}}`)).toBe(
            String.raw`[\{1\}]`,
        );
    });

    test("reveals balanced TeX groups inside a cloze", () => {
        expect(
            revealMathjaxClozeAnswers(String.raw`{{c1::\sqrt{\frac{a}{b}}}}`),
        ).toBe(String.raw`[\sqrt{\frac{a}{b}}]`);
    });

    test("reveals entity-escaped TeX group closes", () => {
        expect(
            revealMathjaxClozeAnswers(String.raw`{{c1::\sqrt{\frac{a}{b&#125;&#125;}}`),
        ).toBe(String.raw`[\sqrt{\frac{a}{b}}]`);
    });

    test("decodes entity-escaped literal right braces", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::\{1\&#125;}}`)).toBe(
            String.raw`[\{1\}]`,
        );
    });

    test("omits hints", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::x::hint}}`)).toBe("[x]");
    });

    test("ignores hint-like text inside TeX groups", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::\text{a::b}}}`)).toBe(
            String.raw`[\text{a::b}]`,
        );
    });

    test("leaves malformed clozes unchanged", () => {
        const input = String.raw`{{c1::\{1\}}`;
        expect(revealMathjaxClozeAnswers(input)).toBe(input);
    });
});
