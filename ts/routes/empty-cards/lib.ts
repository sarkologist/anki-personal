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
    return report.notes.flatMap((note) => keepNotes && note.willDeleteNote ? note.cardIds.slice(1) : note.cardIds);
}

/** The report shown, narrowed to the cards a fresh report still has: a
field filled in since then makes its card not empty any more. */
export function stillEmpty(shown: EmptyCardsReport, fresh: EmptyCardsReport): EmptyCardsReport {
    const now = new Map(fresh.notes.map((n) => [n.noteId, n]));
    const out = shown.clone();
    out.notes = shown.notes.flatMap((note) => {
        const current = now.get(note.noteId);
        if (!current) {
            return [];
        }
        const ids = new Set(current.cardIds);
        const copy = note.clone();
        copy.cardIds = note.cardIds.filter((id) => ids.has(id));
        copy.willDeleteNote = current.willDeleteNote && copy.cardIds.length === current.cardIds.length;
        return copy.cardIds.length ? [copy] : [];
    });
    return out;
}
