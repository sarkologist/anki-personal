// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { getRange, getSelection } from "@tslib/cross-browser";

import { undecorateFragment } from "./decorated-elements";
import { fragmentToStored } from "./rich-text-input/transform";

export type AgentSelectionInputKind = "rich_text" | "plain_text";

export interface AgentSelectedTextContext {
    field_name: string;
    field_index: number;
    input_kind: AgentSelectionInputKind;
    text: string;
    html: string | null;
}

export interface AgentSelectionReadResult {
    inside: boolean;
    context: AgentSelectedTextContext | null;
}

function nodeIsInside(base: Node, node: Node): boolean {
    return node === base || base.contains(node);
}

function rangeIsInside(base: Node, range: Range): boolean {
    return nodeIsInside(base, range.startContainer)
        && nodeIsInside(base, range.endContainer);
}

function richTextSelectionHtml(range: Range): string | null {
    try {
        const fragment = range.cloneContents();
        undecorateFragment(fragment);
        return fragmentToStored(fragment) || null;
    } catch {
        return null;
    }
}

export function richTextAgentSelectionContext(
    editable: HTMLElement,
    fieldName: string,
    fieldIndex: number,
): AgentSelectionReadResult {
    const selection = getSelection(editable);
    if (!selection) {
        return { inside: false, context: null };
    }

    const range = getRange(selection);
    if (!range || !rangeIsInside(editable, range)) {
        return { inside: false, context: null };
    }

    if (range.collapsed) {
        return { inside: true, context: null };
    }

    const text = range.toString();
    if (!text.trim()) {
        return { inside: true, context: null };
    }

    return {
        inside: true,
        context: {
            field_name: fieldName,
            field_index: fieldIndex,
            input_kind: "rich_text",
            text,
            html: richTextSelectionHtml(range),
        },
    };
}

export function plainTextAgentSelectionContext(
    selectedText: string,
    fieldName: string,
    fieldIndex: number,
): AgentSelectedTextContext | null {
    if (!selectedText.trim()) {
        return null;
    }

    return {
        field_name: fieldName,
        field_index: fieldIndex,
        input_kind: "plain_text",
        text: selectedText,
        html: null,
    };
}
