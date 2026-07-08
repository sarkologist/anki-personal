// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { revealMathjaxClozeAnswers } from "./mathjax-cloze";

describe("revealMathjaxClozeAnswers", () => {
    // The revealed answer is wrapped in a TeX group — `{[…]}`, not bare `[…]`.
    // The group stops the leading `[` from being read as an argument of a
    // preceding token (e.g. `\\[…]` = a row break with an optional spacing
    // argument, or `a_[x]` = subscript of `[`), which otherwise errors or
    // mis-parses. See the `\\`-adjacent regression test below.
    test("reveals an escaped literal right brace inside a cloze", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::\{1\}}}`)).toBe(
            String.raw`{[\{1\}]}`,
        );
    });

    test("reveals balanced TeX groups inside a cloze", () => {
        expect(
            revealMathjaxClozeAnswers(String.raw`{{c1::\sqrt{\frac{a}{b}}}}`),
        ).toBe(String.raw`{[\sqrt{\frac{a}{b}}]}`);
    });

    test("reveals entity-escaped TeX group closes", () => {
        expect(
            revealMathjaxClozeAnswers(String.raw`{{c1::\sqrt{\frac{a}{b&#125;&#125;}}`),
        ).toBe(String.raw`{[\sqrt{\frac{a}{b}}]}`);
    });

    test("decodes entity-escaped literal right braces", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::\{1\&#125;}}`)).toBe(
            String.raw`{[\{1\}]}`,
        );
    });

    test("omits hints", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::x::hint}}`)).toBe("{[x]}");
    });

    test("ignores hint-like text inside TeX groups", () => {
        expect(revealMathjaxClozeAnswers(String.raw`{{c1::\text{a::b}}}`)).toBe(
            String.raw`{[\text{a::b}]}`,
        );
    });

    test("wraps a subscript cloze so `[` is not taken as the subscript", () => {
        // Bare `a_[x]` makes the subscript just `[` (mis-parse); the group
        // keeps the whole `[x]` as the subscript.
        expect(revealMathjaxClozeAnswers(String.raw`a_{{c1::x}}`)).toBe(
            String.raw`a_{[x]}`,
        );
    });

    test("wraps the reveal so a cloze right after \\\\ does not error", () => {
        // A cloze on the 2nd line of a \substack sits immediately after `\\`.
        // Bare `[…]` would produce `\\[…]` ("Bracket argument to \\ must be a
        // dimension"); the group makes it `\\{[…]}`, which renders.
        expect(
            revealMathjaxClozeAnswers(
                String.raw`\sum_{\substack{x\bmod c\\{{c1::x\bar x\equiv 1\,(c)}}}}`,
            ),
        ).toBe(String.raw`\sum_{\substack{x\bmod c\\{[x\bar x\equiv 1\,(c)]}}}`);
    });

    test("leaves malformed clozes unchanged", () => {
        const input = String.raw`{{c1::\{1\}}`;
        expect(revealMathjaxClozeAnswers(input)).toBe(input);
    });
});
