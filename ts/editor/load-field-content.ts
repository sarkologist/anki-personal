// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Writable } from "svelte/store";

interface SyncableRichTextInput {
    api: {
        syncFromStoredContent(): void;
    };
}

export function loadFieldContent(
    fieldStores: Writable<string>[],
    richTextInputs: (SyncableRichTextInput | undefined)[],
    fields: [string, string][],
): void {
    for (const [index, [, fieldContent]] of fields.entries()) {
        fieldStores[index].set(fieldContent);
        richTextInputs[index]?.api.syncFromStoredContent();
    }
}
