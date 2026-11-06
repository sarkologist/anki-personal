// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { minsToSecs, secsToMins, toUint } from "./lib";

test("minutes and seconds", () => {
    expect(secsToMins(1200)).toBe(20);
    expect(secsToMins(89)).toBe(1);
    expect(minsToSecs(20)).toBe(1200);
    // a cleared or negative spin box is no limit, not a negative one
    expect(minsToSecs(-3)).toBe(0);
    expect(minsToSecs(NaN)).toBe(0);
});

test("number boxes are clamped as Qt's spin boxes", () => {
    // rollover 0-23: the backend stores a byte and would wrap 300 to 44
    expect(toUint(300, 0, 23, 4)).toBe(23);
    expect(toUint(-1, 0, 23, 4)).toBe(0);
    expect(toUint(2.6, 0, 23, 4)).toBe(3);
    // an emptied box keeps the saved value
    expect(toUint(null, 0, 23, 4)).toBe(4);
    expect(toUint(NaN, 5, 9999, 30)).toBe(30);
});
