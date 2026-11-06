// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { getEmptyCards } from "@generated/backend";

import { quiet } from "$lib/shell/shell";

import type { PageLoad } from "./$types";

export const load = (async () => ({ report: await getEmptyCards({}, quiet) })) satisfies PageLoad;
