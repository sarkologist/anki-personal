// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { PageLoad } from "./$types";
import { loadOverview } from "./data";

export const load = (async () => {
    return { initial: await loadOverview() };
}) satisfies PageLoad;
