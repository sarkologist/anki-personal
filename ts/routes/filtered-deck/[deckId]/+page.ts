// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { filteredDeckOrderLabels, getOrCreateFilteredDeck } from "@generated/backend";

import { quiet } from "$lib/shell/shell";

import type { PageLoad } from "./$types";

export const load = (async ({ params }) => {
    const did = BigInt(params.deckId);
    const [deck, labels] = await Promise.all([
        getOrCreateFilteredDeck({ did }, quiet),
        filteredDeckOrderLabels({}, quiet),
    ]);
    return { deck, orderLabels: labels.vals };
}) satisfies PageLoad;
