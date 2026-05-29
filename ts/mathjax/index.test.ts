// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import "./index";

test("loads the html package for cloze classes inside MathJax", () => {
    expect(window.MathJax.tex.packages["[+]"]).toContain("html");
    expect(window.MathJax.loader.load).toContain("[tex]/html");
});
