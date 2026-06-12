// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Readable } from "svelte/store";
import { get } from "svelte/store";

export function transformContentBeforeSave(content: string): string {
    return content.replace(/ data-editor-shrink="(true|false)"/g, "");
}

export function saveFieldsCommand(
    noteId: number,
    fieldStores: Readable<string>[],
): string {
    const fields = fieldStores.map((store) => transformContentBeforeSave(get(store)));
    return `saveFields:${noteId}:${JSON.stringify(fields)}`;
}
