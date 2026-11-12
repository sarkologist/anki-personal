// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { on } from "@tslib/events";

import { placeCaretAfter, placeCaretBefore } from "$lib/domlib/place-caret";

import { mount, tick, unmount } from "svelte";
import { autoDecorationSuspended } from "./decorated";
import type { DecoratedElement, DecoratedElementConstructor } from "./decorated";
import { FrameElement, frameElement } from "./frame-element";
import type { LegacyLatexKind } from "./legacy-latex-preview";
import LegacyLatex_svelte from "./LegacyLatex.svelte";

const legacyLatexFramePattern =
    /<anki-frame\b(?=[^>]*\bdata-frames=(?:"anki-latex"|'anki-latex'|anki-latex))[^>]*>.*?<\/anki-frame>/gsu;
// Quote-aware so a `>` inside an attribute value (e.g. a serialized
// data-latex) doesn't end the opening tag early.
const legacyLatexTagPattern = /<anki-latex\b((?:[^>"']|"[^"]*"|'[^']*')*)>(.*?)<\/anki-latex>/gsu;
const dataLatexAttributePattern = /\sdata-latex\s*=/u;
const legacyLatexDisplayDelimiterPattern = /\[\$\$\](.*?)\[\/\$\$\]/gsu;
const legacyLatexInlineDelimiterPattern = /\[\$\](.*?)\[\/\$\]/gsu;

interface LegacyLatexProps {
    latex: string;
    kind: LegacyLatexKind;
}

function kindFromAttributes(attributes: string): LegacyLatexKind {
    return /\bdata-latex-kind="display"/u.test(attributes) ? "display" : "inline";
}

function kindFromElement(element: HTMLElement): LegacyLatexKind {
    return element.dataset.latexKind === "display" ? "display" : "inline";
}

function delimiterFor(kind: LegacyLatexKind): ["[$]" | "[$$]", "[/$]" | "[/$$]"] {
    return kind === "display" ? ["[$$]", "[/$$]"] : ["[$]", "[/$]"];
}

function blockFor(kind: LegacyLatexKind): boolean {
    return kind === "display";
}

function wrapStored(kind: LegacyLatexKind, text: string): string {
    const [open, close] = delimiterFor(kind);
    return `${open}${text}${close}`;
}

/**
 * Decorated (rendered) markup, as it reaches the clipboard: the source lives
 * in `data-latex` and the children are the preview, so innerHTML must not be
 * used. Parse it so the browser decodes the attribute.
 */
function decoratedHtmlToStored(html: string): string {
    const template = document.createElement("template");
    template.innerHTML = html;

    const element = template.content.querySelector<HTMLElement>("anki-latex");
    if (!element) {
        return html;
    }

    let kind = kindFromElement(element);
    if (!element.hasAttribute("data-latex-kind")) {
        const frameBlock = element.closest("anki-frame")?.getAttribute("block");
        kind = frameBlock === "true" ? "display" : "inline";
    }

    return wrapStored(kind, element.dataset.latex ?? element.innerHTML);
}

export const LegacyLatex: DecoratedElementConstructor = class LegacyLatex extends HTMLElement
    implements DecoratedElement
{
    static tagName = "anki-latex";

    static toStored(undecorated: string): string {
        if (!undecorated.includes("anki-latex")) {
            return undecorated;
        }

        return undecorated
            .replace(legacyLatexFramePattern, decoratedHtmlToStored)
            .replace(
                legacyLatexTagPattern,
                (match: string, attributes: string, text: string) => {
                    if (dataLatexAttributePattern.test(attributes)) {
                        return decoratedHtmlToStored(match);
                    }
                    return wrapStored(kindFromAttributes(attributes), text);
                },
            );
    }

    static toUndecorated(stored: string): string {
        return stored
            .replace(legacyLatexDisplayDelimiterPattern, (_match: string, text: string) => {
                return `<${LegacyLatex.tagName} data-latex-kind="display">${text}</${LegacyLatex.tagName}>`;
            })
            .replace(legacyLatexInlineDelimiterPattern, (_match: string, text: string) => {
                return `<${LegacyLatex.tagName} data-latex-kind="inline">${text}</${LegacyLatex.tagName}>`;
            });
    }

    kind: LegacyLatexKind = "inline";
    frame?: FrameElement;
    component?: Record<string, any> | null;
    props?: LegacyLatexProps;

    static get observedAttributes(): string[] {
        return ["data-latex-kind", "data-latex"];
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
            case "data-latex-kind":
                this.kind = newValue === "display" ? "display" : "inline";
                if (this.props) { this.props.kind = this.kind; }
                this.frame?.setAttribute("block", String(blockFor(this.kind)));
                break;

            case "data-latex":
                if (typeof newValue !== "string") {
                    return;
                }
                if (this.props) { this.props.latex = newValue; }
                break;
        }
    }

    decorate(): void {
        if (this.hasAttribute("decorated")) {
            this.undecorate();
        }

        this.kind = kindFromElement(this);

        if (this.parentElement?.tagName === FrameElement.tagName.toUpperCase()) {
            this.frame = this.parentElement as FrameElement;
        } else {
            frameElement(this, blockFor(this.kind));
            return;
        }

        this.dataset.latex = this.innerHTML;
        this.innerHTML = "";
        this.style.whiteSpace = "normal";

        const props = $state<LegacyLatexProps>({
            latex: this.dataset.latex,
            kind: this.kind,
        });

        const component = mount(LegacyLatex_svelte, {
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

        this.innerHTML = this.dataset.latex ?? "";
        delete this.dataset.latex;
        this.removeAttribute("style");
        this.removeAttribute("focusonmount");
        this.dataset.latexKind = this.kind;

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
