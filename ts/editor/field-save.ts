// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Readable } from "svelte/store";
import { get } from "svelte/store";

export function transformContentBeforeSave(content: string): string {
    return content.replace(/ data-editor-shrink="(true|false)"/g, "");
}

/** Read-only fields, and their content as loaded. */
export interface ReadOnlyFields {
    readOnly: boolean[];
    loaded: string[];
}

/** What to save for field `index`: a read-only field's content as loaded. */
export function fieldContentToSave(
    index: number,
    content: string,
    keep: ReadOnlyFields,
): string {
    // the hidden inputs' DOM round trip rewrites entities (&quot; to "), so
    // their store isn't what was loaded
    if (keep.readOnly[index] && keep.loaded[index] !== undefined) {
        return keep.loaded[index];
    }
    return transformContentBeforeSave(content);
}

export function saveFieldsCommand(
    noteId: number,
    fieldStores: Readable<string>[],
    keep: ReadOnlyFields = { readOnly: [], loaded: [] },
): string {
    const fields = fieldStores.map((store, index) => fieldContentToSave(index, get(store), keep));
    return `saveFields:${noteId}:${JSON.stringify(fields)}`;
}
