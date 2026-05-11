// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { replaceEditorMathjaxElements } from "./mathjax";

test("replaces editor mathjax tags with reviewer delimiters", () => {
    const qa = document.createElement("div");
    qa.innerHTML = `For <anki-mathjax>w=z^3</anki-mathjax> and <anki-mathjax block="true">x<br>y</anki-mathjax>`;

    replaceEditorMathjaxElements(qa);

    expect(qa.querySelector("anki-mathjax")).toBeNull();
    expect(qa.textContent).toBe("For \\(w=z^3\\) and \\[x\ny\\]");
});

test("uses decorated mathjax data when present", () => {
    const qa = document.createElement("div");
    qa.innerHTML = `<anki-mathjax data-mathjax="w=\\epsilon"><span>preview</span></anki-mathjax>`;

    replaceEditorMathjaxElements(qa);

    expect(qa.textContent).toBe("\\(w=\\epsilon\\)");
});

test("block=false is treated as inline mathjax", () => {
    const qa = document.createElement("div");
    qa.innerHTML = `<anki-mathjax block="false">a+b</anki-mathjax>`;

    replaceEditorMathjaxElements(qa);

    expect(qa.textContent).toBe("\\(a+b\\)");
});
