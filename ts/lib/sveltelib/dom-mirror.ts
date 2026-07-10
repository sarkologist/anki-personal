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
    syncFromStore(): void;
    /**
     * Whether the store already reflects the element's given raw innerHTML,
     * i.e. a flush would be a no-op.
     */
    isClean(elementHTML: string): boolean;
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

function nodeContentsHTML(node: Node): string {
    const wrapper = document.createElement("div");
    wrapper.append(cloneNode(node));
    return wrapper.innerHTML;
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
    let syncFromStore = noop;
    let isClean: (elementHTML: string) => boolean = () => false;

    function preventResubscription() {
        allowResubscription.set(false);

        return () => {
            flushPendingMirror();
            allowResubscription.set(true);
        };
    }

    function mirror(
        element: HTMLElement,
        { store }: { store: Writable<DocumentFragment> },
    ): { destroy(): void } {
        let cancelPendingSave: CancelIdleCallback | null = null;
        /**
         * The fragment the store last received from this element (or retained,
         * when the new one was equal). Used by {@link mirrorFromFragment} to
         * recognize its own echo by identity, without serializing anything.
         */
        let lastMirroredFragment: DocumentFragment | null = null;
        let lastSavedHTML: string | null = null;

        function cancelScheduledSave(): void {
            cancelPendingSave?.();
            cancelPendingSave = null;
        }

        function saveHTMLToStore(): void {
            cancelScheduledSave();
            /* Mutations that leave the serialized content unchanged (e.g.
             * shadow-DOM bookkeeping right after decoration) don't need the
             * expensive clone/normalize pipeline below. */
            const currentHTML = element.innerHTML;
            if (currentHTML === lastSavedHTML) {
                return;
            }
            const fragment = cloneNode(element);
            /* Assign before set: subscribers run synchronously inside it. */
            lastMirroredFragment = fragment;
            store.set(fragment);
            /* The store keeps its previous fragment when the new one is
             * equal; record whichever object it retained. */
            const unsubscribe = store.subscribe((retained) => {
                lastMirroredFragment = retained;
            });
            unsubscribe();
            /* Only after a successful set: if it threw, the store never
             * received this state and the next flush must retry. */
            lastSavedHTML = currentHTML;
        }

        function storeFragment(): DocumentFragment | undefined {
            let current: DocumentFragment | undefined;
            const unsubscribe = store.subscribe((fragment) => {
                current = fragment;
            });
            unsubscribe();
            return current;
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
        const isCleanForElement = (elementHTML: string): boolean =>
            /* The raw-HTML check alone is not enough: while the element is
             * focused the store can be updated externally (e.g. from the
             * plain-text input) without the element changing, so also require
             * that the store still holds the exact fragment this mirror last
             * recorded. */
            lastSavedHTML !== null
            && elementHTML === lastSavedHTML
            && lastMirroredFragment !== null
            && storeFragment() === lastMirroredFragment;
        isClean = isCleanForElement;

        const observer = new MutationObserver(scheduleSaveHTMLToStore);
        observer.observe(element, config);

        function mirrorToElement(node: Node): void {
            cancelScheduledSave();
            if (nodeContentsHTML(node) === element.innerHTML) {
                return;
            }

            /* The element is about to be rewritten from the store, so the
             * saved-HTML shortcut no longer reflects the store's value. */
            lastSavedHTML = null;
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
            if (fragment === lastMirroredFragment) {
                return;
            }

            lastMirroredFragment = null;
            mirrorToElement(cloneNode(fragment));
        }

        function syncElementFromStore(): void {
            const unsubscribe = store.subscribe(mirrorFromFragment);
            unsubscribe();
        }

        syncFromStore = syncElementFromStore;

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
                if (isClean === isCleanForElement) {
                    isClean = () => false;
                }
                if (syncFromStore === syncElementFromStore) {
                    syncFromStore = noop;
                }
            },
        };
    }

    return {
        mirror,
        preventResubscription,
        flush: () => flushPendingMirror(),
        syncFromStore: () => syncFromStore(),
        isClean: (elementHTML) => isClean(elementHTML),
    };
}

export default useDOMMirror;
