// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, expect, test, vi } from "vitest";

import { containsMathjax, replaceEditorMathjaxElements } from "./mathjax";

const mathjaxWindow = window as Window & {
    MathJax?: { startup?: { promise?: Promise<void> } };
};

afterEach(() => {
    delete mathjaxWindow.MathJax;
    document.head.replaceChildren();
});

test.each([
    ["plain text", false],
    ["\\(x + y\\)", true],
    ["before \\[\nx + y\n\\] after", true],
    ["<anki-mathjax>x + y</anki-mathjax>", true],
    ["an unmatched \\( delimiter", false],
])("detects MathJax in %j", (html, expected) => {
    expect(containsMathjax(html)).toBe(expected);
});

test("lazy loader adopts an existing MathJax startup promise", async () => {
    const startupPromise = Promise.resolve();
    mathjaxWindow.MathJax = { startup: { promise: startupPromise } };
    vi.resetModules();

    const { lazyLoadMathJax } = await import("./mathjax");

    expect(lazyLoadMathJax()).toBe(startupPromise);
    expect(document.head.querySelector("script")).toBeNull();
});

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
