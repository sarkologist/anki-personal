// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import {
    Deck_Filtered,
    Deck_Filtered_SearchTerm,
    Deck_Filtered_SearchTerm_Order as Order,
    FilteredDeckForUpdate,
} from "@generated/anki/decks_pb";
import { expect, test } from "vitest";

import { loadTerms, saveTerms } from "./lib";

const deck = (id: bigint, terms: [string, number, Order][]) =>
    new FilteredDeckForUpdate({
        id,
        config: new Deck_Filtered({
            searchTerms: terms.map(([search, limit, order]) => new Deck_Filtered_SearchTerm({ search, limit, order })),
        }),
    });

test("a new deck shows one filter, with the second's defaults behind it", () => {
    const { first, second, secondEnabled } = loadTerms(deck(0n, [["deck:x is:due", 100, Order.DUE]]));
    expect(first).toEqual({ search: "deck:x is:due", limit: 100, order: Order.DUE });
    expect(second).toEqual({ search: "", limit: 20, order: Order.ADDED });
    expect(secondEnabled).toBe(false);
});

test("an existing deck with two filters shows both", () => {
    const loaded = loadTerms(deck(5n, [["a", 10, Order.RANDOM], ["b", 30, Order.LAPSES]]));
    expect(loaded.secondEnabled).toBe(true);
    expect(loaded.second.search).toBe("b");
});

test("the second filter is saved only when enabled, and limits stay positive", () => {
    const first = { search: "a", limit: 0, order: Order.RANDOM };
    const second = { search: "b", limit: 7.4, order: Order.ADDED };
    expect(saveTerms(first, second, false).map((t) => t.search)).toEqual(["a"]);
    const both = saveTerms(first, second, true);
    expect(both.map((t) => [t.search, t.limit])).toEqual([["a", 1], ["b", 7]]);
});
