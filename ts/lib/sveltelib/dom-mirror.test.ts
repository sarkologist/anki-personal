// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { writable } from "svelte/store";
import { afterEach, describe, expect, test, vi } from "vitest";

import useDOMMirror from "./dom-mirror";
import { nodeStore } from "./node-store";

function fragmentFromHtml(html: string): DocumentFragment {
    return document.createRange().createContextualFragment(html);
}

function fragmentToHtml(fragment: DocumentFragment): string {
    const wrapper = document.createElement("div");
    wrapper.append(fragment.cloneNode(true));
    return wrapper.innerHTML;
}

function undecorateMathjax(fragment: DocumentFragment): void {
    for (const element of fragment.querySelectorAll<HTMLElement>("anki-mathjax")) {
        element.innerHTML = element.dataset.mathjax ?? "";
        delete element.dataset.mathjax;
        element.removeAttribute("contenteditable");
        element.removeAttribute("decorated");
    }
}

async function flushMutationObserver(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
}

describe("useDOMMirror", () => {
    afterEach(() => {
        vi.useRealTimers();
        document.body.replaceChildren();
    });

    test("defers element mutations before writing to the store", async () => {
        vi.useFakeTimers();
        const element = document.createElement("div");
        const store = writable(fragmentFromHtml(""));
        const values: string[] = [];
        const mirror = useDOMMirror();
        const action = mirror.mirror(element, { store });

        store.subscribe((fragment) => values.push(fragmentToHtml(fragment)));
        element.textContent = "changed";
        await flushMutationObserver();

        expect(values.at(-1)).toBe("");

        vi.runOnlyPendingTimers();
        expect(values.at(-1)).toBe("changed");

        action.destroy();
    });

    test("flushes a pending element mutation immediately", async () => {
        vi.useFakeTimers();
        const element = document.createElement("div");
        const store = writable(fragmentFromHtml(""));
        const values: string[] = [];
        const mirror = useDOMMirror();
        const action = mirror.mirror(element, { store });

        store.subscribe((fragment) => values.push(fragmentToHtml(fragment)));
        element.innerHTML = "<b>changed</b>";
        await flushMutationObserver();

        mirror.flush();

        expect(values.at(-1)).toBe("<b>changed</b>");

        vi.runOnlyPendingTimers();
        expect(values.filter((value) => value === "<b>changed</b>")).toHaveLength(
            1,
        );

        action.destroy();
    });

    test("flushes suspended local mutations before resubscribing on blur", async () => {
        vi.useFakeTimers();
        const element = document.createElement("div");
        element.tabIndex = 0;
        document.body.append(element);
        const store = nodeStore<DocumentFragment>(
            fragmentFromHtml("stale"),
            undecorateMathjax,
        );
        const values: string[] = [];
        const mirror = useDOMMirror();
        const action = mirror.mirror(element, { store });

        store.subscribe((fragment) => values.push(fragmentToHtml(fragment)));
        expect(element.innerHTML).toBe("stale");

        element.focus();
        const allowResubscription = mirror.preventResubscription();
        element.innerHTML =
            "<anki-mathjax data-mathjax=\"x^2\" decorated><span data-anki=\"mathjax\"></span></anki-mathjax>";
        const mathjaxElement = element.querySelector("anki-mathjax");
        await flushMutationObserver();

        element.blur();
        allowResubscription();

        const stored = values.at(-1);
        expect(stored).toBe("<anki-mathjax>x^2</anki-mathjax>");
        expect(stored).not.toBe("stale");
        expect(element.innerHTML).toContain("data-mathjax=\"x^2\"");
        expect(element.querySelector("anki-mathjax")).toBe(mathjaxElement);

        vi.runOnlyPendingTimers();
        expect(values.at(-1)).toBe(stored);

        action.destroy();
    });

    test("external store updates cancel stale pending element saves", async () => {
        vi.useFakeTimers();
        const element = document.createElement("div");
        const store = writable(fragmentFromHtml(""));
        const values: string[] = [];
        const mirror = useDOMMirror();
        const action = mirror.mirror(element, { store });

        store.subscribe((fragment) => values.push(fragmentToHtml(fragment)));
        element.textContent = "local";
        await flushMutationObserver();

        store.set(fragmentFromHtml("<i>remote</i>"));
        vi.runOnlyPendingTimers();

        expect(element.innerHTML).toBe("<i>remote</i>");
        expect(values.at(-1)).toBe("<i>remote</i>");
        expect(values).not.toContain("local");

        action.destroy();
    });

    test("destroy cancels a pending element save", async () => {
        vi.useFakeTimers();
        const element = document.createElement("div");
        const store = writable(fragmentFromHtml(""));
        const values: string[] = [];
        const mirror = useDOMMirror();
        const action = mirror.mirror(element, { store });

        store.subscribe((fragment) => values.push(fragmentToHtml(fragment)));
        element.textContent = "changed";
        await flushMutationObserver();

        action.destroy();
        vi.runOnlyPendingTimers();

        expect(values.at(-1)).toBe("");
    });
});
