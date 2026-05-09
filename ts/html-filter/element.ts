// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { isHTMLElement, isNightMode } from "./helpers";
import { removeNode as removeElement } from "./node";
import { filterStylingInternal, filterStylingLightMode, filterStylingNightMode } from "./styling";

interface TagsAllowed {
    [tagName: string]: FilterMethod;
}

type FilterMethod = (element: Element) => void;

function filterAttributes(
    attributePredicate: (attributeName: string) => boolean,
    element: Element,
): void {
    for (const attr of [...element.attributes]) {
        const attrName = attr.name.toUpperCase();

        if (!attributePredicate(attrName)) {
            element.removeAttributeNode(attr);
        }
    }
}

function allowNone(element: Element): void {
    filterAttributes(() => false, element);
}

const allow = (attrs: string[]): FilterMethod => (element: Element): void =>
    filterAttributes(
        (attributeName: string) => attrs.includes(attributeName),
        element,
    );

const allowClass = (attrs: string[] = []): FilterMethod => allow(["CLASS", ...attrs]);

function unwrapElement(element: Element): void {
    element.replaceWith(...element.childNodes);
}

function filterSpan(element: Element): void {
    const filterAttrs = allow(["STYLE"]);
    filterAttrs(element);

    const filterStyle = isNightMode() ? filterStylingNightMode : filterStylingLightMode;
    filterStyle(element as HTMLSpanElement);
}

const tagsAllowedBasic: TagsAllowed = {
    BR: allowNone,
    IMG: allow(["SRC", "ALT"]),
    DIV: allowNone,
    P: allowNone,
    SUB: allowNone,
    SUP: allowNone,
    TITLE: removeElement,
};

const tagsAllowedExtended: TagsAllowed = {
    ...tagsAllowedBasic,
    DIV: allowClass(),
    A: allowClass(["HREF"]),
    ASIDE: allowClass(),
    B: allowClass(),
    BLOCKQUOTE: allowClass(),
    CODE: allowClass(),
    DD: allowClass(),
    DL: allowClass(),
    DT: allowClass(),
    EM: allowClass(),
    FONT: allowClass(["COLOR"]),
    H1: allowClass(),
    H2: allowClass(),
    H3: allowClass(),
    I: allowClass(),
    LI: allowClass(),
    OL: allowClass(),
    PRE: allowClass(),
    RP: allowClass(),
    RT: allowClass(),
    RUBY: allowClass(),
    SPAN: filterSpan,
    STRONG: allowClass(),
    TABLE: allowClass(),
    TD: allowClass(["COLSPAN", "ROWSPAN"]),
    TH: allowClass(["COLSPAN", "ROWSPAN"]),
    TR: allowClass(["ROWSPAN"]),
    U: allowClass(),
    UL: allowClass(),
};

const filterElementTagsAllowed = (tagsAllowed: TagsAllowed) => (element: Element): void => {
    const tagName = element.tagName;

    if (Object.prototype.hasOwnProperty.call(tagsAllowed, tagName)) {
        tagsAllowed[tagName](element);
    } else if (element.innerHTML) {
        unwrapElement(element);
    } else {
        removeElement(element);
    }
};

export const filterElementBasic = filterElementTagsAllowed(tagsAllowedBasic);
export const filterElementExtended = filterElementTagsAllowed(tagsAllowedExtended);

export function filterElementInternal(element: Element): void {
    if (isHTMLElement(element)) {
        filterStylingInternal(element);
    }
}
