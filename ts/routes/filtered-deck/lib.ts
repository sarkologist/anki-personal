// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    Deck_Filtered_SearchTerm,
    Deck_Filtered_SearchTerm_Order,
    type FilteredDeckForUpdate,
} from "@generated/anki/decks_pb";

export interface Term {
    search: string;
    limit: number;
    order: Deck_Filtered_SearchTerm_Order;
}

/** The dialog's two filters, as FilteredDeckConfigDialog loads them: the
second is shown only for an existing deck that has one, and defaults to 20
cards in the order added. */
export function loadTerms(deck: FilteredDeckForUpdate): {
    first: Term;
    second: Term;
    secondEnabled: boolean;
} {
    const terms = deck.config?.searchTerms ?? [];
    const term = (t: Deck_Filtered_SearchTerm | undefined, fallback: Term): Term =>
        t ? { search: t.search, limit: t.limit, order: t.order } : fallback;
    return {
        first: term(terms[0], {
            search: "",
            limit: 100,
            order: Deck_Filtered_SearchTerm_Order.RANDOM,
        }),
        second: term(terms[1], {
            search: "",
            limit: 20,
            order: Deck_Filtered_SearchTerm_Order.ADDED,
        }),
        secondEnabled: deck.id !== 0n && terms.length > 1,
    };
}

/** The search terms to save. */
export function saveTerms(first: Term, second: Term, secondEnabled: boolean): Deck_Filtered_SearchTerm[] {
    const terms = secondEnabled ? [first, second] : [first];
    return terms.map((t) => new Deck_Filtered_SearchTerm({ ...t, limit: Math.max(1, Math.round(t.limit || 0)) }));
}
