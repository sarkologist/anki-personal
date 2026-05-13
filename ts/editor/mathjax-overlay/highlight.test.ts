// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { mathjaxHighlightSurround } from "./highlight";

describe("mathjaxHighlightSurround", () => {
    test.each([
        ["hl-1", "{\\color{var(--hl-1)}"],
        ["hl-2", "{\\color{var(--hl-2)}"],
        ["hl-3", "{\\color{var(--hl-3)}"],
    ])("wraps selected text with the %s color variable", (className, prefix) => {
        expect(mathjaxHighlightSurround(className)).toEqual({
            prefix,
            suffix: "}",
        });
    });
});
