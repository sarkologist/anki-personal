// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/**
 * A minimal Emacs/readline-style kill ring shared across the web-based editor
 * surfaces (rich text fields and the CodeMirror HTML editor run in the same
 * page). Each kill replaces the buffer; yank inserts the last kill.
 */
let killText = "";

export function setKillText(text: string): void {
    killText = text;
}

export function getKillText(): string {
    return killText;
}
