// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { getPreferences } from "@generated/backend";

import { quiet } from "$lib/shell/shell";

import type { PageLoad } from "./$types";
import { shellCommand, type ShellPreferences } from "./lib";

export const load = (async () => {
    const [prefs, shell] = await Promise.all([
        getPreferences({}, quiet),
        shellCommand<ShellPreferences>("shellPreferences"),
    ]);
    return { prefs, shell };
}) satisfies PageLoad;
