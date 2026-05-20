// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { on } from "@tslib/events";
import { noop } from "@tslib/functional";
import type { Writable } from "svelte/store";
import { writable } from "svelte/store";

import storeSubscribe from "./store-subscribe";

const config = {
    childList: true,
    subtree: true,
    attributes: true,
    characterData: true,
};

export type MirrorAction = (
    element: HTMLElement,
    params: { store: Writable<DocumentFragment> },
) => { destroy(): void };

interface DOMMirrorAPI {
    mirror: MirrorAction;
    preventResubscription(): () => void;
    flush(): void;
}

type CancelIdleCallback = () => void;

function cloneNode(node: Node): DocumentFragment {
    /**
     * Creates a deep clone
     * This seems to be less buggy than node.cloneNode(true)
     */
    const range = document.createRange();

    range.selectNodeContents(node);
    return range.cloneContents();
}

function requestIdle(callback: () => void): CancelIdleCallback {
    const idleWindow = window as Window & {
        requestIdleCallback?: (
            callback: () => void,
            options?: { timeout: number },
        ) => number;
        cancelIdleCallback?: (handle: number) => void;
    };

    if (idleWindow.requestIdleCallback && idleWindow.cancelIdleCallback) {
        const handle = idleWindow.requestIdleCallback(callback, { timeout: 500 });
        return () => idleWindow.cancelIdleCallback!(handle);
    }

    const handle = setTimeout(callback);
    return () => clearTimeout(handle);
}

/**
 * Allows you to keep an element's inner HTML bidirectionally
 * in sync with a store containing a DocumentFragment.
 * While the element has focus, this connection is tethered.
 * In practice, this will sync changes from PlainTextInput to RichTextInput.
 */
function useDOMMirror(): DOMMirrorAPI {
    const allowResubscription = writable(true);
    let flushPendingMirror = noop;

    function preventResubscription() {
        allowResubscription.set(false);

        return () => {
            allowResubscription.set(true);
        };
    }

    function mirror(
        element: HTMLElement,
        { store }: { store: Writable<DocumentFragment> },
    ): { destroy(): void } {
        let cancelPendingSave: CancelIdleCallback | null = null;

        function cancelScheduledSave(): void {
            cancelPendingSave?.();
            cancelPendingSave = null;
        }

        function saveHTMLToStore(): void {
            cancelScheduledSave();
            store.set(cloneNode(element));
        }

        function scheduleSaveHTMLToStore(): void {
            if (cancelPendingSave) {
                return;
            }

            cancelPendingSave = requestIdle(() => {
                cancelPendingSave = null;
                saveHTMLToStore();
            });
        }

        flushPendingMirror = saveHTMLToStore;

        const observer = new MutationObserver(scheduleSaveHTMLToStore);
        observer.observe(element, config);

        function mirrorToElement(node: Node): void {
            cancelScheduledSave();
            observer.disconnect();
            // element.replaceChildren(...node.childNodes); // TODO use once available
            while (element.firstChild) {
                element.firstChild.remove();
            }

            while (node.firstChild) {
                element.appendChild(node.firstChild);
            }
            observer.observe(element, config);
        }

        function mirrorFromFragment(fragment: DocumentFragment): void {
            mirrorToElement(cloneNode(fragment));
        }

        const { subscribe, unsubscribe } = storeSubscribe(
            store,
            mirrorFromFragment,
            false,
        );

        /* do not update when focused as it will reset caret */
        const removeFocus = on(element, "focus", unsubscribe);
        let removeBlur: (() => void) | undefined;

        const unsubResubscription = allowResubscription.subscribe(
            (allow: boolean): void => {
                if (allow) {
                    if (!removeBlur) {
                        removeBlur = on(element, "blur", subscribe);
                    }

                    const root = element.getRootNode() as Document | ShadowRoot;

                    if (root.activeElement !== element) {
                        subscribe();
                    }
                } else if (removeBlur) {
                    removeBlur();
                    removeBlur = undefined;
                }
            },
        );

        return {
            destroy() {
                cancelScheduledSave();
                observer.disconnect();

                removeFocus();
                removeBlur?.();

                unsubscribe();
                unsubResubscription();
                flushPendingMirror = noop;
            },
        };
    }

    return {
        mirror,
        preventResubscription,
        flush: () => flushPendingMirror(),
    };
}

export default useDOMMirror;
