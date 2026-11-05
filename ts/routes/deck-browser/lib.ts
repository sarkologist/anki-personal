// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import type { DeckTreeNode } from "@generated/anki/decks_pb";

export interface DeckRow {
    id: bigint;
    /** The last component of the deck name. */
    name: string;
    fullName: string;
    level: number;
    collapsed: boolean;
    hasChildren: boolean;
    filtered: boolean;
    newCount: number;
    learnCount: number;
    reviewCount: number;
}

/** The visible rows of the deck list, as deckbrowser.py's `_render_deck_node`
    walks the tree: children of collapsed decks are skipped. The tree from the
    backend already omits an empty Default deck. */
export function flattenDeckTree(top: DeckTreeNode): DeckRow[] {
    const rows: DeckRow[] = [];
    const walk = (node: DeckTreeNode, prefix: string): void => {
        const fullName = prefix + node.name;
        rows.push({
            id: node.deckId,
            name: node.name,
            fullName,
            level: node.level,
            collapsed: node.collapsed,
            hasChildren: node.children.length > 0,
            filtered: node.filtered,
            newCount: node.newCount,
            learnCount: node.learnCount,
            reviewCount: node.reviewCount,
        });
        if (!node.collapsed) {
            for (const child of node.children) {
                walk(child, fullName + "::");
            }
        }
    };
    for (const child of top.children) {
        walk(child, "");
    }
    return rows;
}

function normalizeName(name: string): string {
    return name
        .split("::")
        .map((part) => part.trim())
        .join("::")
        .toLowerCase();
}

/** Deck names are unique ignoring case. Used to open an existing deck
    instead of adding a renamed duplicate, as `add_normal_deck_with_name`. */
export function deckNameExists(top: DeckTreeNode, name: string): boolean {
    const target = normalizeName(name);
    const walk = (node: DeckTreeNode, prefix: string): boolean => {
        const full = prefix + node.name;
        return (
            normalizeName(full) === target
            || node.children.some((child) => walk(child, full + "::"))
        );
    };
    return top.children.some((child) => walk(child, ""));
}

export function findDeckNode(node: DeckTreeNode, id: bigint): DeckTreeNode | undefined {
    if (node.deckId === id) {
        return node;
    }
    for (const child of node.children) {
        const found = findDeckNode(child, id);
        if (found) {
            return found;
        }
    }
    return undefined;
}

/** Whether dropping deck `source` on `target` (0 for the top level) would
    move it. */
export function canReparent(top: DeckTreeNode, source: bigint, target: bigint): boolean {
    const sourceNode = findDeckNode(top, source);
    if (!sourceNode || source === target || findDeckNode(sourceNode, target)) {
        return false;
    }
    const parent = target === 0n ? top : findDeckNode(top, target);
    return !!parent && !parent.children.some((child) => child.deckId === source);
}
