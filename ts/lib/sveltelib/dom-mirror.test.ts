// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { writable } from "svelte/store";
import { afterEach, describe, expect, test, vi } from "vitest";

import useDOMMirror from "./dom-mirror";

function fragmentFromHtml(html: string): DocumentFragment {
    return document.createRange().createContextualFragment(html);
}

function fragmentToHtml(fragment: DocumentFragment): string {
    const wrapper = document.createElement("div");
    wrapper.append(fragment.cloneNode(true));
    return wrapper.innerHTML;
}

async function flushMutationObserver(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
}

describe("useDOMMirror", () => {
    afterEach(() => {
        vi.useRealTimers();
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
