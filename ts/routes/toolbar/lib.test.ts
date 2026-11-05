// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { parseSyncState, syncIndicatorClass } from "./lib";

test("accepts the shell's sync state as an object or JSON", () => {
    expect(parseSyncState({ state: "full", loggedIn: true })).toEqual({
        state: "full",
        loggedIn: true,
    });
    expect(parseSyncState("{\"state\":\"syncing\",\"loggedIn\":true}")).toEqual({
        state: "syncing",
        loggedIn: true,
    });
});

test("anything unexpected means no sync indicator", () => {
    expect(parseSyncState(null)).toEqual({ state: "none", loggedIn: false });
    expect(parseSyncState("not json")).toEqual({ state: "none", loggedIn: false });
    expect(parseSyncState({ state: "weird", loggedIn: true })).toEqual({
        state: "none",
        loggedIn: true,
    });
});

test("the indicator colour follows the required sync, only when logged in", () => {
    expect(syncIndicatorClass({ state: "normal", loggedIn: true })).toBe("normal-sync");
    expect(syncIndicatorClass({ state: "full", loggedIn: true })).toBe("full-sync");
    expect(syncIndicatorClass({ state: "syncing", loggedIn: true })).toBe("");
    expect(syncIndicatorClass({ state: "full", loggedIn: false })).toBe("");
});
