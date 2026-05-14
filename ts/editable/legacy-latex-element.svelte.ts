// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { on } from "@tslib/events";

import { placeCaretAfter, placeCaretBefore } from "$lib/domlib/place-caret";

import { mount, tick } from "svelte";
import { autoDecorationSuspended } from "./decorated";
import type { DecoratedElement, DecoratedElementConstructor } from "./decorated";
import { FrameElement, frameElement } from "./frame-element";
import type { LegacyLatexKind } from "./legacy-latex-preview";
import LegacyLatex_svelte from "./LegacyLatex.svelte";

const legacyLatexTagPattern = /<anki-latex\b([^>]*)>(.*?)<\/anki-latex>/gsu;
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

export const LegacyLatex: DecoratedElementConstructor = class LegacyLatex extends HTMLElement
    implements DecoratedElement
{
    static tagName = "anki-latex";

    static toStored(undecorated: string): string {
        return undecorated.replace(
            legacyLatexTagPattern,
            (_match: string, attributes: string, text: string) => {
                const [open, close] = delimiterFor(kindFromAttributes(attributes));
                return `${open}${text}${close}`;
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
