// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { updateAllState } from "$lib/components/WithState.svelte";
import { execCommand } from "$lib/domlib";

import { filterHTML } from "../html-filter";
import { decoratedElements, decorateUndecoratedElements } from "./decorated-elements";

function markdownMathBlock(element: Element): boolean | null {
    const { classList } = element;

    if (!classList.contains("math")) {
        return null;
    }

    if (
        classList.contains("math-block")
        || classList.contains("math-display")
        || classList.contains("display")
    ) {
        return true;
    }

    if (classList.contains("math-inline") || classList.contains("inline")) {
        return false;
    }

    return null;
}

function trimLineBreaks(source: string): string {
    return source.replace(/^\n+/u, "").replace(/\n+$/u, "");
}

function stripMathDelimiters(source: string, block: boolean): string {
    const trimmed = source.trim();
    const [open, close] = block ? ["\\[", "\\]"] : ["\\(", "\\)"];

    if (trimmed.startsWith(open) && trimmed.endsWith(close)) {
        return trimLineBreaks(trimmed.slice(open.length, -close.length));
    }

    return trimLineBreaks(source);
}

function normalizeMarkdownMathElements(html: string): string {
    const template = document.createElement("template");
    template.innerHTML = html;

    for (const element of template.content.querySelectorAll(".math")) {
        const block = markdownMathBlock(element);
        if (block === null) {
            continue;
        }

        const mathjax = document.createElement("anki-mathjax");
        if (block) {
            mathjax.setAttribute("block", "true");
        }
        mathjax.textContent = stripMathDelimiters(element.textContent ?? "", block);
        element.replaceWith(mathjax);
    }

    return template.innerHTML;
}

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
    html = normalizeMarkdownMathElements(html);
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
