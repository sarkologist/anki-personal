// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { CustomStudyRequest_Cram_CramKind as CramKind } from "@generated/anki/scheduler_pb";
import { expect, test } from "vitest";

import { buildRequest, spinFor } from "./lib";

const noTags = { kind: CramKind.DUE, include: [], exclude: [] };

test("limits start from the deck's extensions and may go down", () => {
    expect(spinFor("new", { extendNew: 5, extendReview: 9 })).toEqual({ min: -99999, max: 99999, value: 5 });
    expect(spinFor("review", { extendNew: 5, extendReview: 9 }).value).toBe(9);
    expect(spinFor("forgot", { extendNew: 0, extendReview: 0 }).max).toBe(30);
});

test("each choice sets its own field", () => {
    expect(buildRequest(3n, "new", -2, noTags).value).toEqual({ case: "newLimitDelta", value: -2 });
    expect(buildRequest(3n, "ahead", 4, noTags).value).toEqual({ case: "reviewAheadDays", value: 4 });
    // out of range or not a number: clamped, as Qt's spin box
    expect(buildRequest(3n, "forgot", 99, noTags).value).toEqual({ case: "forgotDays", value: 30 });
    expect(buildRequest(3n, "preview", NaN, noTags).value).toEqual({ case: "previewDays", value: 1 });
});

test("cramming takes the card kind, limit and tags", () => {
    const request = buildRequest(3n, "cram", 50, { kind: CramKind.NEW, include: ["a"], exclude: ["b"] });
    expect(request.deckId).toBe(3n);
    expect(request.value.case).toBe("cram");
    const cram = request.value.value as { kind: CramKind; cardLimit: number; tagsToInclude: string[] };
    expect([cram.kind, cram.cardLimit, cram.tagsToInclude]).toEqual([CramKind.NEW, 50, ["a"]]);
});
