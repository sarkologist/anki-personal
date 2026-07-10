// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { SelectionLocation } from "$lib/domlib/location";
import { restoreSelection, saveSelection } from "$lib/domlib/location";
import { fragmentToString } from "@tslib/dom";

interface Snapshot {
    html: string;
    selection: SelectionLocation | null;
}

type NormalizeSnapshot = (fragment: DocumentFragment) => void;
type CancelIdleCallback = () => void;

const observerConfig: MutationObserverInit = {
    childList: true,
    subtree: true,
    attributes: true,
    characterData: true,
};

const debounceMs = 300;
const maxHistory = 200;

function requestIdle(callback: () => void): CancelIdleCallback {
    const idleWindow = window as Window & {
        requestIdleCallback?: (
            callback: () => void,
            options?: { timeout: number },
        ) => number;
        cancelIdleCallback?: (handle: number) => void;
    };

    if (idleWindow.requestIdleCallback && idleWindow.cancelIdleCallback) {
        const handle = idleWindow.requestIdleCallback(callback, { timeout: 1000 });
        return () => idleWindow.cancelIdleCallback!(handle);
    }

    const handle = setTimeout(callback);
    return () => clearTimeout(handle);
}

/**
 * Per-field undo/redo stack for a contenteditable element.
 *
 * Captures snapshots (HTML + selection) on a debounce after any mutation to
 * the element, so that typing, toolbar actions (which manipulate the DOM
 * directly and bypass the browser's contenteditable undo), pastes, image
 * inserts and MathJax frame edits all participate in the same undo history.
 *
 * Toolbar code can call {@link flush} to commit the current state as a
 * standalone undo step before performing its own mutation.
 */
export class FieldUndo {
    private past: Snapshot[] = [];
    private future: Snapshot[] = [];
    private last: Snapshot;
    /**
     * The base element's raw (decorated) innerHTML at the time of the last
     * snapshot, or null when it is unknown. Lets {@link commit} skip the
     * expensive clone/normalize snapshot when nothing serializable changed.
     */
    private lastRawHtml: string | null = null;
    private debounceHandle: ReturnType<typeof setTimeout> | null = null;
    private cancelIdleCommit: CancelIdleCallback | null = null;
    private readonly observer: MutationObserver;

    constructor(
        private readonly base: HTMLElement,
        private readonly normalizeSnapshot?: NormalizeSnapshot,
        /**
         * Optional cheaper source for the normalized snapshot HTML, e.g. an
         * already-normalized fragment maintained elsewhere. Receives the base
         * element's current raw innerHTML so it can check that its fragment
         * is not stale. Returning null falls back to cloning and normalizing
         * the base element.
         */
        private readonly snapshotSource?: (rawHtml: string) => string | null,
    ) {
        this.lastRawHtml = base.innerHTML;
        this.last = this.snapshot(this.lastRawHtml);
        this.observer = new MutationObserver(() => this.onMutation());
        this.observer.observe(base, observerConfig);
    }

    private snapshotHtml(rawHtml: string): string {
        const sourced = this.snapshotSource?.(rawHtml);
        if (typeof sourced === "string") {
            return sourced;
        }

        if (!this.normalizeSnapshot) {
            return rawHtml;
        }

        const range = document.createRange();
        range.selectNodeContents(this.base);
        const fragment = range.cloneContents();
        this.normalizeSnapshot(fragment);
        return fragmentToString(fragment);
    }

    private snapshot(rawHtml: string): Snapshot {
        return {
            html: this.snapshotHtml(rawHtml),
            selection: saveSelection(this.base),
        };
    }

    private onMutation(): void {
        this.cancelScheduledIdleCommit();
        if (this.debounceHandle != null) {
            clearTimeout(this.debounceHandle);
        }
        this.debounceHandle = setTimeout(() => {
            this.debounceHandle = null;
            this.scheduleIdleCommit();
        }, debounceMs);
    }

    private scheduleIdleCommit(): void {
        if (this.cancelIdleCommit) {
            return;
        }

        this.cancelIdleCommit = requestIdle(() => {
            this.cancelIdleCommit = null;
            this.commit();
        });
    }

    private cancelScheduledIdleCommit(): void {
        this.cancelIdleCommit?.();
        this.cancelIdleCommit = null;
    }

    private commit(): void {
        const rawHtml = this.base.innerHTML;
        if (rawHtml === this.lastRawHtml) {
            return;
        }
        const current = this.snapshot(rawHtml);
        this.lastRawHtml = rawHtml;
        if (current.html === this.last.html) {
            return;
        }
        this.past.push(this.last);
        if (this.past.length > maxHistory) {
            this.past.shift();
        }
        this.last = current;
        this.future.length = 0;
    }

    private restore(snap: Snapshot): void {
        this.base.innerHTML = snap.html;
        /* Decoration mutates the DOM after this, so the raw HTML that
         * corresponds to this snapshot isn't knowable here. */
        this.lastRawHtml = null;
        this.last = snap;
        if (snap.selection) {
            try {
                restoreSelection(this.base, snap.selection);
            } catch {
                // Selection coordinates couldn't be resolved; leave caret as-is.
            }
        }
    }

    /**
     * Force a commit of the current DOM state right now. Call this before
     * a programmatic mutation (e.g. a toolbar action) so that any pending
     * typing batch is closed off as its own undo step.
     */
    flush(): void {
        if (this.debounceHandle != null) {
            clearTimeout(this.debounceHandle);
            this.debounceHandle = null;
        }
        this.cancelScheduledIdleCommit();
        this.commit();
    }

    undo(): boolean {
        this.flush();
        const snap = this.past.pop();
        if (!snap) {
            return false;
        }
        this.future.push(this.last);
        this.restore(snap);
        return true;
    }

    redo(): boolean {
        this.flush();
        const snap = this.future.pop();
        if (!snap) {
            return false;
        }
        this.past.push(this.last);
        this.restore(snap);
        return true;
    }

    /** Discard all history and re-baseline on the current DOM state. */
    reset(): void {
        if (this.debounceHandle != null) {
            clearTimeout(this.debounceHandle);
            this.debounceHandle = null;
        }
        this.cancelScheduledIdleCommit();
        this.past.length = 0;
        this.future.length = 0;
        this.lastRawHtml = this.base.innerHTML;
        this.last = this.snapshot(this.lastRawHtml);
    }

    destroy(): void {
        if (this.debounceHandle != null) {
            clearTimeout(this.debounceHandle);
            this.debounceHandle = null;
        }
        this.cancelScheduledIdleCommit();
        this.observer.disconnect();
    }
}
