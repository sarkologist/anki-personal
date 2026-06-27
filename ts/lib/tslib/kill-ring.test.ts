// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { getKillText, setKillText } from "./kill-ring";

describe("kill ring", () => {
    test("yank returns the last kill", () => {
        setKillText("hello");
        expect(getKillText()).toBe("hello");
        setKillText("world");
        expect(getKillText()).toBe("world");
    });
});
