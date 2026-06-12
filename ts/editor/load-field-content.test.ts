// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { get, writable } from "svelte/store";
import { afterEach, expect, test, vi } from "vitest";

import useDOMMirror from "$lib/sveltelib/dom-mirror";

import { loadFieldContent } from "./load-field-content";

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

afterEach(() => {
    vi.useRealTimers();
    document.body.replaceChildren();
});

test("note loads refresh focused rich text after a forced conversion refresh", async () => {
    vi.useFakeTimers();
    const element = document.createElement("div");
    element.tabIndex = 0;
    document.body.append(element);

    const fieldStore = writable("before");
    const nodeStore = writable(fragmentFromHtml("before"));
    let settingFieldFromNodes = false;
    const unsubscribeField = fieldStore.subscribe((html) => {
        if (!settingFieldFromNodes) {
            nodeStore.set(fragmentFromHtml(html));
        }
    });
    const unsubscribeNodes = nodeStore.subscribe((fragment) => {
        settingFieldFromNodes = true;
        try {
            fieldStore.set(fragmentToHtml(fragment));
        } finally {
            settingFieldFromNodes = false;
        }
    });

    const mirror = useDOMMirror();
    const action = mirror.mirror(element, { store: nodeStore });
    const richTextInput = {
        api: {
            syncFromStoredContent: mirror.syncFromStore,
        },
    };

    element.focus();
    fieldStore.set("<i>converted</i>");
    mirror.syncFromStore();
    expect(element.innerHTML).toBe("<i>converted</i>");

    element.textContent = "pending old note";
    await flushMutationObserver();

    loadFieldContent(
        [fieldStore],
        [richTextInput],
        [["Front", "<b>note B</b>"]],
    );
    expect(element.innerHTML).toBe("<b>note B</b>");

    loadFieldContent([fieldStore], [richTextInput], [["Front", "note C"]]);
    vi.runOnlyPendingTimers();

    expect(element.innerHTML).toBe("note C");
    expect(get(fieldStore)).toBe("note C");
    expect(document.activeElement).toBe(element);

    action.destroy();
    unsubscribeField();
    unsubscribeNodes();
});
