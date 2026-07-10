// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { on } from "@tslib/events";

import { placeCaretAfter, placeCaretBefore } from "$lib/domlib/place-caret";

import { mount, tick, unmount } from "svelte";
import { autoDecorationSuspended } from "./decorated";
import type { DecoratedElement, DecoratedElementConstructor } from "./decorated";
import { FrameElement, frameElement } from "./frame-element";
import Mathjax_svelte from "./Mathjax.svelte";

const mathjaxFramePattern =
    /<anki-frame\b(?=[^>]*\bdata-frames=(?:"anki-mathjax"|'anki-mathjax'|anki-mathjax))[^>]*>.*?<\/anki-frame>/gsu;
const mathjaxTagPattern = /<anki-mathjax\b[^>]*>.*?<\/anki-mathjax>/gsu;

const mathjaxBlockDelimiterPattern = /\\\[(.*?)\\\]/gsu;
const mathjaxInlineDelimiterPattern = /\\\((.*?)\\\)/gsu;

function trimBreaks(text: string): string {
    return text
        .replace(/<br[ ]*\/?>/gsu, "\n")
        .replace(/^\n*/, "")
        .replace(/\n*$/, "");
}

function mathjaxElementFromHtml(html: string): HTMLElement | null {
    const template = document.createElement("template");
    template.innerHTML = html;

    return template.content.querySelector<HTMLElement>("anki-mathjax");
}

function hasBlockAttribute(element: Element): boolean {
    const block = element.getAttribute("block")
        ?? element.closest("anki-frame")?.getAttribute("block");

    return typeof block === "string" && block !== "false";
}

/**
 * Slow but always-correct decorated->stored conversion of one matched frame
 * (or bare tag): parse it with a `<template>` so the browser decodes the
 * `data-mathjax` attribute, then read source + block. Used as the fallback
 * for anything the fast path below declines to handle.
 */
function slowMathjaxHtmlToStored(html: string): string {
    const element = mathjaxElementFromHtml(html);
    if (!element) {
        return html;
    }

    const source = typeof element.dataset.mathjax === "string"
        ? element.dataset.mathjax
        : element.innerHTML;
    const trimmed = trimBreaks(source);

    return hasBlockAttribute(element) ? `\\[${trimmed}\\]` : `\\(${trimmed}\\)`;
}

/**
 * Shared, lazily-created element for decoding HTML entities in a `data-mathjax`
 * value. A `<textarea>`'s RCDATA content decodes named/numeric entities
 * identically to an attribute value (verified against the parser), and is far
 * cheaper than building an element tree for every frame.
 */
let entityDecoder: HTMLTextAreaElement | null = null;

function decodeHtmlEntities(value: string): string {
    if (!value.includes("&")) {
        return value;
    }
    if (!entityDecoder) {
        entityDecoder = document.createElement("textarea");
    }
    entityDecoder.innerHTML = value;
    return entityDecoder.value;
}

/**
 * Return the attribute text of `tagName`'s opening tag (everything between the
 * tag name and its closing `>`), scanning quote-aware so a `>` inside an
 * attribute value doesn't end the tag early. `null` if the tag isn't found or
 * the opening tag is unterminated.
 */
function openTagAttributes(html: string, tagName: string): string | null {
    const marker = `<${tagName}`;
    const start = html.indexOf(marker);
    if (start < 0) {
        return null;
    }
    const attrsStart = start + marker.length;
    const doubleQuote = "\"";
    const singleQuote = "'";
    let quote = "";
    for (let i = attrsStart; i < html.length; i++) {
        const ch = html[i];
        if (quote) {
            if (ch === quote) {
                quote = "";
            }
        } else if (ch === doubleQuote || ch === singleQuote) {
            quote = ch;
        } else if (ch === ">") {
            return html.slice(attrsStart, i);
        }
    }
    return null;
}

/** Read a double-quoted attribute's raw value from an opening-tag attr string. */
function doubleQuotedAttribute(attrs: string, name: string): string | null {
    const marker = `${name}="`;
    let from = 0;
    // find `name="` at an attribute boundary (preceded by whitespace or start)
    for (;;) {
        const at = attrs.indexOf(marker, from);
        if (at < 0) {
            return null;
        }
        if (at === 0 || /\s/u.test(attrs[at - 1])) {
            const valueStart = at + marker.length;
            const end = attrs.indexOf("\"", valueStart);
            return end < 0 ? null : attrs.slice(valueStart, end);
        }
        from = at + 1;
    }
}

/**
 * Fast decorated->stored conversion for the common case: a frame/tag whose
 * `data-mathjax` attribute holds no literal `<` (so a shared `<textarea>` can
 * decode it without RCDATA ambiguity). Returns `null` to defer to
 * {@link slowMathjaxHtmlToStored} for anything unusual, keeping the output
 * byte-identical to the original parser in every case.
 */
function fastMathjaxHtmlToStored(html: string): string | null {
    const tagAttrs = openTagAttributes(html, "anki-mathjax");
    if (tagAttrs === null) {
        return null;
    }
    const rawSource = doubleQuotedAttribute(tagAttrs, "data-mathjax");
    if (rawSource === null || rawSource.includes("<")) {
        // no data-mathjax (slow path reads innerHTML instead), or a literal
        // `<` the textarea decoder can't be trusted with — defer.
        return null;
    }

    const trimmed = trimBreaks(decodeHtmlEntities(rawSource));

    // block: tag attribute wins, else the enclosing frame's (mirrors
    // `getAttribute("block") ?? closest("anki-frame")?.getAttribute("block")`).
    let block = doubleQuotedAttribute(tagAttrs, "block");
    if (block === null) {
        const frameAttrs = openTagAttributes(html, "anki-frame");
        block = frameAttrs === null ? null : doubleQuotedAttribute(frameAttrs, "block");
    }
    // Decode before comparing: the parser-based path reads the decoded value,
    // so e.g. `block="&#102;alse"` must be treated as `"false"` (inline).
    const isBlock = block !== null && decodeHtmlEntities(block) !== "false";

    return isBlock ? `\\[${trimmed}\\]` : `\\(${trimmed}\\)`;
}

function mathjaxHtmlToStored(html: string): string {
    return fastMathjaxHtmlToStored(html) ?? slowMathjaxHtmlToStored(html);
}

export const __testing = { fastMathjaxHtmlToStored };

export const mathjaxConfig = {
    enabled: true,
    templateScriptVersion: 0,
    notetypeCss: "",
};

interface MathjaxProps {
    mathjax: string;
    block: boolean;
    fontSize: number;
}

export const Mathjax: DecoratedElementConstructor = class Mathjax extends HTMLElement implements DecoratedElement {
    static tagName = "anki-mathjax";

    static toStored(undecorated: string): string {
        return undecorated
            .replace(mathjaxFramePattern, mathjaxHtmlToStored)
            .replace(mathjaxTagPattern, mathjaxHtmlToStored);
    }

    static toUndecorated(stored: string): string {
        if (!mathjaxConfig.enabled) {
            return stored;
        }
        return stored
            .replace(mathjaxBlockDelimiterPattern, (_match: string, text: string) => {
                const trimmed = trimBreaks(text);
                return `<${Mathjax.tagName} block="true">${trimmed}</${Mathjax.tagName}>`;
            })
            .replace(mathjaxInlineDelimiterPattern, (_match: string, text: string) => {
                const trimmed = trimBreaks(text);
                return `<${Mathjax.tagName}>${trimmed}</${Mathjax.tagName}>`;
            });
    }

    block = false;
    frame?: FrameElement;
    component?: Record<string, any> | null;
    props?: MathjaxProps;

    static get observedAttributes(): string[] {
        return ["block", "data-mathjax"];
    }

    connectedCallback(): void {
        if (autoDecorationSuspended()) {
            return;
        }

        this.decorate();
    }

    disconnectedCallback(): void {
        this.removeEventListeners();
        this.unmountComponent();
    }

    /**
     * Svelte 5 components stay registered in the reactive graph until
     * unmounted, so skipping this leaks every decorated element (and its
     * whole frame subtree) each time a note is (re)loaded.
     */
    unmountComponent(): void {
        if (this.component) {
            unmount(this.component);
            this.component = null;
            this.props = undefined;
        }
    }

    attributeChangedCallback(name: string, old: string, newValue: string): void {
        if (newValue === old) {
            return;
        }

        switch (name) {
            case "block":
                this.block = newValue !== "false";
                if (this.props) { this.props.block = this.block; }
                this.frame?.setAttribute("block", String(this.block));
                break;

            case "data-mathjax":
                if (typeof newValue !== "string") {
                    return;
                }
                if (this.props) { this.props.mathjax = newValue; }
                break;
        }
    }

    decorate(): void {
        if (this.hasAttribute("decorated")) {
            this.undecorate();
        }

        if (this.parentElement?.tagName === FrameElement.tagName.toUpperCase()) {
            this.frame = this.parentElement as FrameElement;
        } else {
            frameElement(this, this.block);
            /* Framing will place this element inside of an anki-frame element,
             * causing the connectedCallback to be called again.
             * If we'd continue decorating at this point, we'd loose all the information */
            return;
        }

        this.dataset.mathjax = this.innerHTML;
        this.innerHTML = "";
        this.style.whiteSpace = "normal";

        const inheritedFontSize = parseFloat(getComputedStyle(this).fontSize);
        const props = $state<MathjaxProps>({
            mathjax: this.dataset.mathjax,
            block: this.block,
            fontSize: Number.isFinite(inheritedFontSize) ? inheritedFontSize : 20,
        });

        const component = mount(Mathjax_svelte, {
            target: this,
            props,
        });

        this.component = component;
        this.props = props;

        if (this.hasAttribute("focusonmount")) {
            let position: [number, number] | undefined = undefined;

            if (this.getAttribute("focusonmount")!.length > 0) {
                position = this.getAttribute("focusonmount")!
                    .split(",")
                    .map(Number) as [number, number];
            }

            tick().then(() => {
                this.component?.moveCaretAfter(position);
            });
        }

        this.setAttribute("contentEditable", "false");
        this.setAttribute("decorated", "true");
        this.removeEventListeners();
        this.addEventListeners();
    }

    undecorate(): void {
        this.unmountComponent();

        if (this.parentElement?.tagName === FrameElement.tagName.toUpperCase()) {
            this.parentElement.replaceWith(this);
        }

        this.innerHTML = this.dataset.mathjax ?? "";
        delete this.dataset.mathjax;
        this.removeAttribute("style");
        this.removeAttribute("focusonmount");

        if (this.block) {
            this.setAttribute("block", "true");
        } else {
            this.removeAttribute("block");
        }

        this.removeAttribute("contentEditable");
        this.removeAttribute("decorated");
    }

    removeMoveInStart?: () => void;
    removeMoveInEnd?: () => void;

    addEventListeners(): void {
        this.removeMoveInStart = on(
            this,
            "moveinstart" as keyof HTMLElementEventMap,
            () => this.component!.selectAll(),
        );

        this.removeMoveInEnd = on(this, "moveinend" as keyof HTMLElementEventMap, () => this.component!.selectAll());
    }

    removeEventListeners(): void {
        this.removeMoveInStart?.();
        this.removeMoveInStart = undefined;

        this.removeMoveInEnd?.();
        this.removeMoveInEnd = undefined;
    }

    placeCaretBefore(): void {
        if (this.frame) {
            placeCaretBefore(this.frame);
        }
    }

    placeCaretAfter(): void {
        if (this.frame) {
            placeCaretAfter(this.frame);
        }
    }
};
