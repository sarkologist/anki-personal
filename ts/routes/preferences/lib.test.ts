// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { minsToSecs, secsToMins } from "./lib";

test("minutes and seconds", () => {
    expect(secsToMins(1200)).toBe(20);
    expect(secsToMins(89)).toBe(1);
    expect(minsToSecs(20)).toBe(1200);
    // a cleared or negative spin box is no limit, not a negative one
    expect(minsToSecs(-3)).toBe(0);
    expect(minsToSecs(NaN)).toBe(0);
});
