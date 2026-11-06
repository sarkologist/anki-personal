// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { EmptyCardsReport } from "@generated/anki/card_rendering_pb";

export interface ReportSection {
    heading: string;
    lines: { nid: string; text: string; allEmpty: boolean }[];
}

/** The backend's HTML report (`empty_cards_report`) as data, so its
notetype and card names are never treated as HTML. */
export function parseReport(html: string): ReportSection[] {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const sections: ReportSection[] = [];
    for (const child of doc.body.children) {
        if (child.tagName === "DIV") {
            sections.push({ heading: child.textContent ?? "", lines: [] });
        } else if (child.tagName === "OL" && sections.length) {
            for (const li of child.querySelectorAll("li")) {
                const text = li.textContent ?? "";
                const match = /^\[anki:nid:(\d+)\]\s*(.*)$/s.exec(text);
                sections.at(-1)!.lines.push({
                    nid: match?.[1] ?? "",
                    text: match?.[2] ?? text,
                    allEmpty: li.classList.contains("allempty"),
                });
            }
        }
    }
    return sections;
}

/** The cards to delete (EmptyCardsDialog._delete_cards): keeping notes
leaves the first card of a note that would lose all of them. */
export function cardsToDelete(report: EmptyCardsReport, keepNotes: boolean): bigint[] {
    return report.notes.flatMap((note) =>
        keepNotes && note.willDeleteNote ? note.cardIds.slice(1) : note.cardIds
    );
}
