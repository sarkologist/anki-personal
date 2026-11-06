// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { EmptyCardsReport, EmptyCardsReport_NoteWithEmptyCards } from "@generated/anki/card_rendering_pb";
import { expect, test } from "vitest";

import { cardsToDelete, parseReport } from "./lib";

test("the report becomes text, names included", () => {
    const html = "<div><b>Empty cards for <img src=x onerror=alert(1)>:</b></div><ol>"
        + "<li class=allempty>[anki:nid:12] 2 of 2 cards empty (Front, Back).</li>"
        + "<li class=>[anki:nid:13] 1 of 2 cards empty (Back).</li></ol>";
    expect(parseReport(html)).toEqual([{
        heading: "Empty cards for :",
        lines: [
            { nid: "12", text: "2 of 2 cards empty (Front, Back).", allEmpty: true },
            { nid: "13", text: "1 of 2 cards empty (Back).", allEmpty: false },
        ],
    }]);
});

test("keeping notes spares a note's first card", () => {
    const report = new EmptyCardsReport({
        notes: [
            new EmptyCardsReport_NoteWithEmptyCards({ noteId: 1n, cardIds: [10n, 11n], willDeleteNote: true }),
            new EmptyCardsReport_NoteWithEmptyCards({ noteId: 2n, cardIds: [20n], willDeleteNote: false }),
        ],
    });
    expect(cardsToDelete(report, true)).toEqual([11n, 20n]);
    expect(cardsToDelete(report, false)).toEqual([10n, 11n, 20n]);
});
