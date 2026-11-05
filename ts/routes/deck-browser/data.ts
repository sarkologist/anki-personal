// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { DeckTreeNode } from "@generated/anki/decks_pb";
import { deckTree, getCurrentDeck, studiedToday } from "@generated/backend";

import { quiet } from "$lib/shell/shell";

export interface DeckBrowserData {
    tree: DeckTreeNode;
    currentDeckId: bigint;
    currentDeckName: string;
    studiedToday: string;
}

export async function loadDeckBrowser(): Promise<DeckBrowserData> {
    const now = BigInt(Math.floor(Date.now() / 1000));
    // the tree first: with a timestamp, it unburies cards if the day rolled over
    const tree = await deckTree({ now }, quiet);
    const [current, studied] = await Promise.all([
        getCurrentDeck({}, quiet),
        studiedToday({}, quiet),
    ]);
    return {
        tree,
        currentDeckId: current.id,
        currentDeckName: current.name,
        studiedToday: studied.val,
    };
}
