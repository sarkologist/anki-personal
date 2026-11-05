// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Globals the native shell calls on its pages.

import { onDestroy, onMount } from "svelte";

declare global {
    interface Window {
        /** Reload the page's data, after a sync, an undo or an external change. */
        shellRefresh?: () => void;
        /** Push a new sync state (see `SyncState`) to the toolbar. */
        shellSetSyncState?: (state: unknown) => void;
    }
}

/** Backend call options for shell pages: errors are shown in the page, not
    with window.alert, which the shell's webviews may not show. */
export const quiet = { alertOnError: false };

export function errorMessage(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}

type ShellGlobal = "shellRefresh" | "shellSetSyncState";

/** Define `window[name]` while the calling component is mounted. */
export function provideShellGlobal<K extends ShellGlobal>(
    name: K,
    fn: NonNullable<Window[K]>,
): void {
    const globals = window as Partial<Record<ShellGlobal, unknown>>;
    onMount(() => {
        globals[name] = fn;
    });
    onDestroy(() => {
        if (globals[name] === fn) {
            delete globals[name];
        }
    });
}
