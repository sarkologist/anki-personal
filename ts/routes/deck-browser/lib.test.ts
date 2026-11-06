// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { DeckTreeNode } from "@generated/anki/decks_pb";
import { expect, test } from "vitest";

import { canReparent, deckNameExists, findDeckNode, flattenDeckTree } from "./lib";

function node(
    deckId: number,
    name: string,
    level: number,
    children: DeckTreeNode[] = [],
    extra: Partial<DeckTreeNode> = {},
): DeckTreeNode {
    return new DeckTreeNode({ deckId: BigInt(deckId), name, level, children, ...extra });
}

// top
// ├── A (collapsed)
// │   └── A::x
// └── B
//     └── B::y
//         └── B::y::z
const tree = node(0, "", 0, [
    node(1, "A", 1, [node(2, "x", 2)], { collapsed: true }),
    node(3, "B", 1, [node(4, "y", 2, [node(5, "z", 3)])], {
        newCount: 1,
        learnCount: 2,
        reviewCount: 3,
        filtered: true,
    }),
]);

test("flattening skips the children of collapsed decks", () => {
    expect(flattenDeckTree(tree).map((r) => r.fullName)).toEqual([
        "A",
        "B",
        "B::y",
        "B::y::z",
    ]);
});

test("rows carry level, child flag and counts", () => {
    const rows = flattenDeckTree(tree);
    expect(rows[0]).toMatchObject({ id: 1n, level: 1, hasChildren: true, collapsed: true });
    expect(rows[1]).toMatchObject({
        name: "B",
        newCount: 1,
        learnCount: 2,
        reviewCount: 3,
        filtered: true,
    });
    expect(rows[3]).toMatchObject({ name: "z", level: 3, hasChildren: false });
});

test("deck names are matched case-insensitively, including collapsed children", () => {
    expect(deckNameExists(tree, "a::X")).toBe(true);
    expect(deckNameExists(tree, " b::y ")).toBe(true);
    expect(deckNameExists(tree, "B::q")).toBe(false);
});

test("finds nodes anywhere in the tree", () => {
    expect(findDeckNode(tree, 2n)?.name).toBe("x");
    expect(findDeckNode(tree, 99n)).toBeUndefined();
});

test("a deck can't be dropped on itself, its descendants or its current parent", () => {
    expect(canReparent(tree, 3n, 3n)).toBe(false);
    expect(canReparent(tree, 3n, 5n)).toBe(false);
    expect(canReparent(tree, 5n, 4n)).toBe(false);
    expect(canReparent(tree, 1n, 0n)).toBe(false);
});

test("a deck can be dropped on an unrelated deck or the top level", () => {
    expect(canReparent(tree, 2n, 3n)).toBe(true);
    expect(canReparent(tree, 5n, 0n)).toBe(true);
    expect(canReparent(tree, 5n, 1n)).toBe(true);
});
