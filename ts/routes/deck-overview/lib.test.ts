// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import { buriedCounts, descriptionDocument, formatBuried, isFinished } from "./lib";

test("buried counts are the tree's counts minus the queue's", () => {
    expect(
        buriedCounts(
            { newCount: 10, learnCount: 3, reviewCount: 20 },
            { newCount: 8, learningCount: 3, reviewCount: 25 },
        ),
    ).toEqual({ new: 2, learn: 0, review: -5 });
});

test("no tree node means nothing buried", () => {
    expect(
        buriedCounts(undefined, { newCount: 8, learningCount: 3, reviewCount: 25 }),
    ).toEqual({ new: 0, learn: 0, review: 0 });
});

test("buried differences are signed, and hidden when zero", () => {
    expect(formatBuried(2)).toBe("+2");
    expect(formatBuried(-5)).toBe("-5");
    expect(formatBuried(0)).toBe("");
});

test("finished when nothing is queued", () => {
    expect(isFinished({ newCount: 0, learningCount: 0, reviewCount: 0 })).toBe(true);
    expect(isFinished({ newCount: 0, learningCount: 1, reviewCount: 0 })).toBe(false);
});

test("the description document forbids scripts and keeps the content", () => {
    const doc = descriptionDocument("<b>hi</b><script>x()</script>", "color: red;");
    expect(doc).toContain("script-src 'none'");
    expect(doc).toContain("<b>hi</b>");
    expect(doc).toContain("color: red;");
});

test("the description style can't close its style element", () => {
    const doc = descriptionDocument("", "font-family: </style><b>x</b>");
    expect(doc).not.toContain("</style><b>");
});
