// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { ACTIVE_CLOZE_HIGHLIGHT_CLASS, findActiveTextCloze, locateActiveCloze } from "./cloze-locator";

beforeEach(() => {
    vi.useFakeTimers();
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 500 });
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 800 });
});

afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.restoreAllMocks();
    document.body.innerHTML = "";
});

test("finds the first active text cloze", () => {
    document.body.innerHTML = `
<p>
    <span class="cloze-inactive" data-ordinal="2">inactive</span>
    <span class="cloze" data-ordinal="1" id="first">[...]</span>
    <span class="cloze" data-ordinal="1" id="second">[...]</span>
</p>`;

    expect(findActiveTextCloze()?.id).toBe("first");
});

test("ignores inactive clozes", () => {
    document.body.innerHTML = `
<p>
    <span class="cloze-inactive" data-ordinal="1" id="inactive">inactive</span>
    <span class="cloze" data-ordinal="1" id="active">[...]</span>
</p>`;

    expect(findActiveTextCloze()?.id).toBe("active");
});

test("ignores image occlusion cloze shape markers", () => {
    document.body.innerHTML = `
<div class="cloze" data-ordinal="1" data-shape="rect" id="shape"></div>
<span class="cloze" data-ordinal="1" id="text">[...]</span>`;

    expect(findActiveTextCloze()?.id).toBe("text");
});

test("does not scroll when the active cloze is already visible", () => {
    const cloze = activeCloze();
    const scrollIntoView = mockScrollIntoView(cloze);
    mockRect(cloze, { top: 100, bottom: 120, left: 100, right: 180 });

    locateActiveCloze({ highlightDurationMs: 100 });

    expect(scrollIntoView).not.toHaveBeenCalled();
});

test("scrolls when the active cloze is outside the viewport", () => {
    const cloze = activeCloze();
    const scrollIntoView = mockScrollIntoView(cloze);
    mockRect(cloze, { top: 700, bottom: 720, left: 100, right: 180 });

    locateActiveCloze({ highlightDurationMs: 100 });

    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", inline: "nearest" });
});

test("adds and removes the temporary highlight class", () => {
    const cloze = activeCloze();
    mockScrollIntoView(cloze);
    mockRect(cloze, { top: 100, bottom: 120, left: 100, right: 180 });

    locateActiveCloze({ highlightDurationMs: 100 });

    expect(cloze.classList.contains(ACTIVE_CLOZE_HIGHLIGHT_CLASS)).toBe(true);

    vi.advanceTimersByTime(100);

    expect(cloze.classList.contains(ACTIVE_CLOZE_HIGHLIGHT_CLASS)).toBe(false);
});

function activeCloze(): HTMLElement {
    document.body.innerHTML = `<span class="cloze" data-ordinal="1" id="active">[...]</span>`;
    return document.getElementById("active")!;
}

function mockScrollIntoView(element: HTMLElement): ReturnType<typeof vi.fn> {
    const scrollIntoView = vi.fn();
    element.scrollIntoView = scrollIntoView;
    return scrollIntoView;
}

function mockRect(
    element: HTMLElement,
    rect: Pick<DOMRect, "top" | "bottom" | "left" | "right">,
): void {
    vi.spyOn(element, "getBoundingClientRect").mockReturnValue({
        x: rect.left,
        y: rect.top,
        width: rect.right - rect.left,
        height: rect.bottom - rect.top,
        ...rect,
        toJSON: () => ({}),
    } as DOMRect);
}
