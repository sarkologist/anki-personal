// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { elementIsBlock, nodeIsElement, nodeIsText } from "@tslib/dom";

import { updateAllState } from "$lib/components/WithState.svelte";
import { execCommand } from "$lib/domlib";

import { filterHTML } from "../html-filter";
import { decoratedElements, decorateUndecoratedElements } from "./decorated-elements";

const HEADING_SELECTOR = "h1, h2, h3, h4, h5, h6";

/**
 * A heading is only expected to hold phrasing content, so one whose meaningful
 * children are *all* block-level is invalid wrapping (e.g. `<h1><div>…</div></h1>`)
 * rather than a real heading. Whitespace-only text nodes are ignored; any other
 * inline content means the heading still carries heading text and is left alone
 * so it isn't destroyed.
 */
function headingWrapsOnlyBlocks(heading: Element): boolean {
    let sawBlock = false;

    for (const node of heading.childNodes) {
        if (nodeIsElement(node) && elementIsBlock(node)) {
            sawBlock = true;
        } else if (!(nodeIsText(node) && node.data.trim() === "")) {
            return false;
        }
    }

    return sawBlock;
}

function collectHeadingsWrappingBlocks(root: ParentNode): Set<Element> {
    const headings = new Set<Element>();

    for (const heading of root.querySelectorAll(HEADING_SELECTOR)) {
        if (headingWrapsOnlyBlocks(heading)) {
            headings.add(heading);
        }
    }

    return headings;
}

/**
 * `execCommand("insertHTML")` can wrap pasted block content inside a heading
 * when the paste target sits in a heading context, producing invalid markup
 * such as `<h1><div>…</div></h1>`. Anything inside then inherits the heading's
 * font size — most visibly, decorated MathJax is re-decorated at the heading
 * size and swells onto its own line when text with math is copied and pasted.
 *
 * Unwrap the offending headings introduced by the paste; `preexisting` holds the
 * ones that were already like this beforehand, so an unrelated paste doesn't
 * silently rewrite other parts of the field. Moving the children back out
 * re-runs the decorated elements' `connectedCallback` in the corrected context,
 * so oversized MathJax re-renders at the surrounding size.
 */
function unwrapHeadingsWrappingBlocks(
    root: ParentNode,
    preexisting: Set<Element> = new Set(),
): void {
    for (const heading of root.querySelectorAll(HEADING_SELECTOR)) {
        if (!preexisting.has(heading) && headingWrapsOnlyBlocks(heading)) {
            heading.replaceWith(...heading.childNodes);
        }
    }
}

export const __testing = { collectHeadingsWrappingBlocks, unwrapHeadingsWrappingBlocks };

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
    if (internal) {
        // An internal paste carries fully decorated MathJax — the `<anki-frame>`
        // wrapper, frame handles and rendered `<svg>`. Handing that to
        // `execCommand("insertHTML")` can split the surrounding block, stranding
        // the math (and the text after it) as a bare node on its own line.
        // Collapse it back to plain `<anki-mathjax>` first so it inserts cleanly
        // and re-decorates in context.
        html = decoratedElements.toStored(html);
    }
    html = decoratedElements.toUndecorated(html);

    if (html !== "") {
        const editable = activeRichTextEditable();
        const preexisting = editable
            ? collectHeadingsWrappingBlocks(editable)
            : undefined;

        execCommand("inserthtml", false, html);

        if (editable) {
            unwrapHeadingsWrappingBlocks(editable, preexisting);
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
