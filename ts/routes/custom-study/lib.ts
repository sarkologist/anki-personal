// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    CustomStudyRequest,
    CustomStudyRequest_Cram,
    CustomStudyRequest_Cram_CramKind as CramKind,
} from "@generated/anki/scheduler_pb";

/** The dialog's choices, in Qt's order. */
export type Choice = "new" | "review" | "forgot" | "ahead" | "preview" | "cram";

/** DYN_MAX_SIZE */
export const MAX_CARDS = 99999;

/** The number box's range and starting value for a choice
(CustomStudy.onRadioChange). */
export function spinFor(
    choice: Choice,
    defaults: { extendNew: number; extendReview: number },
): { min: number; max: number; value: number } {
    switch (choice) {
        case "new":
            return { min: -MAX_CARDS, max: MAX_CARDS, value: defaults.extendNew };
        case "review":
            return { min: -MAX_CARDS, max: MAX_CARDS, value: defaults.extendReview };
        case "forgot":
            return { min: 1, max: 30, value: 1 };
        case "ahead":
        case "preview":
            return { min: 1, max: MAX_CARDS, value: 1 };
        case "cram":
            return { min: 1, max: MAX_CARDS, value: 100 };
    }
}

/** The card kinds for "Study by card state or tag", in the dialog's order. */
export const CRAM_KINDS = [CramKind.NEW, CramKind.DUE, CramKind.REVIEW, CramKind.ALL];

export function buildRequest(
    deckId: bigint,
    choice: Choice,
    value: number,
    cram: { kind: CramKind; include: string[]; exclude: string[] },
): CustomStudyRequest {
    const { min, max } = spinFor(choice, { extendNew: 0, extendReview: 0 });
    const n = Math.min(max, Math.max(min, Math.round(value || 0)));
    const request = new CustomStudyRequest({ deckId });
    switch (choice) {
        case "new":
            request.value = { case: "newLimitDelta", value: n };
            break;
        case "review":
            request.value = { case: "reviewLimitDelta", value: n };
            break;
        case "forgot":
            request.value = { case: "forgotDays", value: n };
            break;
        case "ahead":
            request.value = { case: "reviewAheadDays", value: n };
            break;
        case "preview":
            request.value = { case: "previewDays", value: n };
            break;
        case "cram":
            request.value = {
                case: "cram",
                value: new CustomStudyRequest_Cram({
                    kind: cram.kind,
                    cardLimit: n,
                    tagsToInclude: cram.include,
                    tagsToExclude: cram.exclude,
                }),
            };
            break;
    }
    return request;
}
