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
    private debounceHandle: ReturnType<typeof setTimeout> | null = null;
    private cancelIdleCommit: CancelIdleCallback | null = null;
    private readonly observer: MutationObserver;

    constructor(
        private readonly base: HTMLElement,
        private readonly normalizeSnapshot?: NormalizeSnapshot,
    ) {
        this.last = this.snapshot();
        this.observer = new MutationObserver(() => this.onMutation());
        this.observer.observe(base, observerConfig);
    }

    private snapshotHtml(): string {
        if (!this.normalizeSnapshot) {
            return this.base.innerHTML;
        }

        const range = document.createRange();
        range.selectNodeContents(this.base);
        const fragment = range.cloneContents();
        this.normalizeSnapshot(fragment);
        return fragmentToString(fragment);
    }

    private snapshot(): Snapshot {
        return {
            html: this.snapshotHtml(),
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
        const current = this.snapshot();
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
        this.last = this.snapshot();
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
