// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { describe, expect, test } from "vitest";

import { emacsWordNavDirection } from "./content-editable";

describe("emacsWordNavDirection", () => {
    test("maps B/F to word movement", () => {
        expect(emacsWordNavDirection("KeyB")).toBe("backward");
        expect(emacsWordNavDirection("KeyF")).toBe("forward");
    });

    test("ignores other keys", () => {
        expect(emacsWordNavDirection("KeyA")).toBeNull();
        expect(emacsWordNavDirection("KeyE")).toBeNull();
        expect(emacsWordNavDirection("ArrowLeft")).toBeNull();
    });
});
