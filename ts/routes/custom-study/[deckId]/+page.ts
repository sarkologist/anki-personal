// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { customStudyDefaults } from "@generated/backend";

import { quiet } from "$lib/shell/shell";

import type { PageLoad } from "./$types";

export const load = (async ({ params }) => {
    const deckId = BigInt(params.deckId);
    return { deckId, defaults: await customStudyDefaults({ deckId }, quiet) };
}) satisfies PageLoad;
