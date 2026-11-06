// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { Notetype } from "@generated/anki/notetypes_pb";

import { shellCommand } from "$lib/shell/shell";

import type { PageLoad } from "./$types";
import { fromBase64 } from "./lib";

interface Init {
    notetype: string;
    ord: number;
    clozeNumbers: number[];
    hasEmptyField: boolean;
    night: boolean;
    error?: string;
}

export const load = (async () => {
    const init = await shellCommand<Init>("clayoutInit");
    if (init.error) {
        throw new Error(init.error);
    }
    return { ...init, notetype: Notetype.fromBinary(fromBase64(init.notetype)) };
}) satisfies PageLoad;
