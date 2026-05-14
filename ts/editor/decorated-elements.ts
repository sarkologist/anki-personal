// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { BLOCK_ELEMENTS, TRANSPARENT_ELEMENTS } from "@tslib/dom";

import type { DecoratedElement } from "../editable/decorated";
import { CustomElementArray } from "../editable/decorated";
import { FrameElement } from "../editable/frame-element";
import { FrameEnd, FrameStart } from "../editable/frame-handle";
import { LegacyLatex } from "../editable/legacy-latex-element.svelte";
import { Mathjax } from "../editable/mathjax-element.svelte";
import { parsingInstructions } from "./plain-text-input";

const decoratedElements = new CustomElementArray();

export function undecorateFragment(fragment: DocumentFragment): void {
    for (const decorated of decoratedElements) {
        for (
            const element of fragment.querySelectorAll(
                decorated.tagName,
            ) as NodeListOf<DecoratedElement>
        ) {
            element.undecorate();
        }
    }
}

function registerMathjax() {
    decoratedElements.push(Mathjax);
    parsingInstructions.push("<style>anki-mathjax { white-space: pre; }</style>");
}

function registerLegacyLatex() {
    decoratedElements.push(LegacyLatex);
    parsingInstructions.push("<style>anki-latex { white-space: pre; }</style>");
}

function registerFrameElement() {
    customElements.define(FrameElement.tagName, FrameElement);
    customElements.define(FrameStart.tagName, FrameStart);
    customElements.define(FrameEnd.tagName, FrameEnd);

    /* This will ensure that they are not targeted by surrounding algorithms */
    BLOCK_ELEMENTS.push(FrameStart.tagName.toUpperCase());
    BLOCK_ELEMENTS.push(FrameEnd.tagName.toUpperCase());

    /* The frame itself is transparent to surround algorithms, so a colour
     * (or other formatting) span applied to text on either side of a framed
     * element extends across the frame instead of being split by it. The
     * decorated element inside (e.g. <anki-mathjax>) inherits the resulting
     * style via the normal CSS cascade. */
    TRANSPARENT_ELEMENTS.push(FrameElement.tagName.toUpperCase());
}

registerLegacyLatex();
registerMathjax();
registerFrameElement();

export { decoratedElements };
