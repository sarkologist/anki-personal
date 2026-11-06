// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export type SyncKind = "none" | "normal" | "full" | "syncing";

/** As the shell's `syncState` bridge command returns it, and as it pushes
    it with `shellSetSyncState`. */
export interface SyncState {
    state: SyncKind;
    loggedIn: boolean;
}

const kinds: SyncKind[] = ["none", "normal", "full", "syncing"];

export function parseSyncState(value: unknown): SyncState {
    if (typeof value === "string") {
        try {
            value = JSON.parse(value);
        } catch {
            value = null;
        }
    }
    const obj = (value ?? {}) as Partial<Record<keyof SyncState, unknown>>;
    const state = kinds.find((k) => k === obj.state) ?? "none";
    return { state, loggedIn: obj.loggedIn === true };
}

/** toolbar.py's `updateSyncColor`; a signed-out profile shows no colour, as
    `get_sync_status` reports no changes for it. */
export function syncIndicatorClass({ state, loggedIn }: SyncState): string {
    if (!loggedIn) {
        return "";
    }
    switch (state) {
        case "normal":
            return "normal-sync";
        case "full":
            return "full-sync";
        default:
            return "";
    }
}
