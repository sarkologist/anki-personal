// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { updateAllState } from "$lib/components/WithState.svelte";
import { execCommand } from "$lib/domlib";

import { filterHTML } from "../html-filter";
import { decoratedElements, decorateUndecoratedElements } from "./decorated-elements";

function activeRichTextEditable(): HTMLElement | null {
    const active = document.activeElement;

    if (!(active instanceof HTMLElement)) {
        return null;
    }

    if (active.matches("anki-editable")) {
        return active;
    }

    const shadowActive = active.shadowRoot?.activeElement;
    if (
        shadowActive instanceof HTMLElement
        && shadowActive.matches("anki-editable")
    ) {
        return shadowActive;
    }

    return null;
}

export function pasteHTML(
    html: string,
    internal: boolean,
    extendedMode: boolean,
): void {
    html = filterHTML(html, internal, extendedMode);
    html = decoratedElements.toUndecorated(html);

    if (html !== "") {
        execCommand("inserthtml", false, html);
        const editable = activeRichTextEditable();
        if (editable) {
            decorateUndecoratedElements(editable);
        }
        updateAllState(new Event("inserthtml"));
    }
}

export function setFormat(cmd: string, arg?: string, _nosave = false): void {
    execCommand(cmd, false, arg);
    updateAllState(new Event(cmd));
}

export function toggleEditorButton(button: HTMLButtonElement): void {
    button.classList.toggle("active");
}
