// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { CongratsInfoResponse } from "@generated/anki/scheduler_pb";
import { congratsInfo, deckTree, getCurrentDeck, getQueuedCards, renderMarkdown } from "@generated/backend";

import { quiet } from "$lib/shell/shell";

import { findDeckNode } from "../deck-browser/lib";
import { buriedCounts, type Counts, isFinished } from "./lib";

export interface OverviewData {
    deckId: bigint;
    name: string;
    filtered: boolean;
    /** User HTML (markdown already rendered); never put it into the page. */
    description: string;
    counts: Counts;
    buried: Counts;
    finished: boolean;
    congrats: CongratsInfoResponse;
}

export async function loadOverview(): Promise<OverviewData> {
    const now = BigInt(Math.floor(Date.now() / 1000));
    // the tree first: with a timestamp, it unburies cards if the day rolled over
    const tree = await deckTree({ now }, quiet);
    const [deck, queued, congrats] = await Promise.all([
        getCurrentDeck({}, quiet),
        getQueuedCards({ fetchLimit: 0, intradayLearningOnly: false }, quiet),
        congratsInfo({}, quiet),
    ]);

    let description = "";
    if (deck.kind.case === "normal") {
        const { description: text, markdownDescription } = deck.kind.value;
        description = text && markdownDescription
            ? (await renderMarkdown({ markdown: text, sanitize: true }, quiet)).val
            : text;
    }

    return {
        deckId: deck.id,
        name: deck.name,
        filtered: deck.kind.case === "filtered",
        description,
        counts: {
            new: queued.newCount,
            learn: queued.learningCount,
            review: queued.reviewCount,
        },
        buried: buriedCounts(findDeckNode(tree, deck.id), queued),
        finished: isFinished(queued),
        congrats,
    };
}
