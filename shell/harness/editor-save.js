// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Edits typed just before the editor switches notes are saved: field saves
// are debounced by 600ms, so the shell must flush them first. Run on a fresh
// note (--test-note 0). The script runs again once the note has reloaded,
// and sessionStorage tells the two runs apart.

const step = sessionStorage.getItem("editor-save-step");
if (!step) {
    sessionStorage.setItem("editor-save-step", "typed");
    const editable = document.querySelector(".rich-text-editable")?.shadowRoot?.querySelector(
        "anki-editable",
    );
    editable.focus();
    const text = editable.firstChild;
    document.getSelection().collapse(text, text.length);
    document.execCommand("insertText", false, " unsaved");
    // well within the debounce
    bridge(`openEditor:${testNote}`);
    // never resolves: the page is replaced
    return await new Promise(() => {});
}
sessionStorage.removeItem("editor-save-step");
const saved = await bridge(`testNote:${testNote}`);
return { ok: saved?.[0] === "alpha beta gamma unsaved", saved: saved?.[0] };
