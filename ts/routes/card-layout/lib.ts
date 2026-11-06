// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { Notetype } from "@generated/anki/notetypes_pb";
import { Notetype_Config_Kind, Notetype_Template, Notetype_Template_Config } from "@generated/anki/notetypes_pb";

export function isCloze(notetype: Notetype): boolean {
    return notetype.config?.kind === Notetype_Config_Kind.CLOZE;
}

/** `CardLayout._newCardName`: "Card N" for the first N not taken. */
export function newCardName(templates: Notetype_Template[], cardName: (n: number) => string): string {
    const taken = new Set(templates.map((t) => t.name));
    let n = templates.length + 1;
    while (taken.has(cardName(n))) {
        n++;
    }
    return cardName(n);
}

/** `CardLayout.onAddCard`: a new card type, a copy of `current`'s sides
(it has no ord yet: the backend adds it). */
export function newTemplate(name: string, current: Notetype_Template): Notetype_Template {
    return new Notetype_Template({
        name,
        config: new Notetype_Template_Config({
            qFormat: current.config?.qFormat ?? "",
            aFormat: current.config?.aFormat ?? "",
        }),
    });
}

/** `CardLayout._flipQA`: the front from `src`'s answer (after the answer
line), the back its front; null if the back has no answer line. */
export function flipped(src: Notetype_Template_Config): { qFormat: string; aFormat: string } | null {
    const match = /^([\s\S]+)<hr id=answer>([\s\S]+)$/.exec(src.aFormat);
    if (!match) {
        return null;
    }
    return {
        qFormat: match[2].trim(),
        aFormat: `{{FrontSide}}\n\n<hr id=answer>\n\n${src.qFormat}`,
    };
}

/** Move the template at `from` to the 1-based `position`; the new index. */
export function reposition(templates: Notetype_Template[], from: number, position: number): number {
    const to = Math.min(templates.length, Math.max(1, Math.round(position))) - 1;
    const [moved] = templates.splice(from, 1);
    templates.splice(to, 0, moved);
    return to;
}

/** The cards to preview for a cloze notetype: the note's cloze numbers, plus
the card it was opened on (CardLayout.setup_preview). */
export function clozeCards(numbers: number[], ord: number): number[] {
    return [...new Set([...numbers, ord + 1])].sort((a, b) => a - b);
}

export function toBase64(bytes: Uint8Array): string {
    let binary = "";
    for (const b of bytes) {
        binary += String.fromCharCode(b);
    }
    return btoa(binary);
}

export function fromBase64(text: string): Uint8Array {
    return Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
}
