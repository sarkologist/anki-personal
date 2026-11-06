// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { expect, test } from "vitest";

import {
    blocksToFetch,
    clickRow,
    contains,
    count,
    flattenSidebar,
    invert,
    moveCurrent,
    normalizeRanges,
    SelectionSender,
    type SelectionState,
    type SidebarItem,
} from "./lib";

const sel = (ranges: [number, number][], current: number | null, anchor = current): SelectionState => ({
    ranges,
    current,
    anchor,
});

test("ranges merge and sort", () => {
    expect(normalizeRanges([[5, 7], [0, 2], [2, 3], [6, 9], [4, 4]])).toEqual([[0, 3], [5, 9]]);
    expect(count([[0, 3], [5, 9]])).toBe(7);
    expect(contains([[0, 3], [5, 9]], 4)).toBe(false);
    expect(contains([[0, 3], [5, 9]], 8)).toBe(true);
});

test("a plain click selects one row", () => {
    expect(clickRow(sel([[0, 5]], 2), 7, {})).toEqual(sel([[7, 8]], 7));
});

test("shift extends from the anchor, cmd toggles", () => {
    const start = sel([[3, 4]], 3);
    expect(clickRow(start, 6, { shift: true })).toEqual(sel([[3, 7]], 6, 3));
    expect(clickRow(start, 1, { shift: true })).toEqual(sel([[1, 4]], 1, 3));
    const toggled = clickRow(start, 6, { meta: true });
    expect(toggled).toEqual(sel([[3, 4], [6, 7]], 6));
    expect(clickRow(toggled, 3, { meta: true })).toEqual(sel([[6, 7]], 3));
});

test("arrow keys move and extend within the table", () => {
    expect(moveCurrent(sel([[3, 4]], 3), 1, false, 10)).toEqual(sel([[4, 5]], 4));
    expect(moveCurrent(sel([[9, 10]], 9), 1, false, 10)).toEqual(sel([[9, 10]], 9));
    expect(moveCurrent(sel([[3, 4]], 3), 2, true, 10)).toEqual(sel([[3, 6]], 5, 3));
    expect(moveCurrent(sel([], null), 1, false, 10)).toEqual(sel([[0, 1]], 0));
    expect(moveCurrent(sel([], null), 1, false, 0)).toEqual(sel([], null));
    // Home/End
    expect(moveCurrent(sel([[3, 4]], 3), -Infinity, false, 10)).toEqual(sel([[0, 1]], 0));
    expect(moveCurrent(sel([[3, 4]], 3), Infinity, false, 10)).toEqual(sel([[9, 10]], 9));
});

test("inverting a selection", () => {
    expect(invert([[2, 4], [6, 7]], 8)).toEqual([[0, 2], [4, 6], [7, 8]]);
    expect(invert([], 3)).toEqual([[0, 3]]);
});

test("rows are fetched in blocks, only the ones missing", () => {
    const have = new Set([0, 1, 2, 3]);
    expect(blocksToFetch(0, 30, 250, (i) => have.has(i), 100)).toEqual([[0, 100]]);
    expect(blocksToFetch(95, 130, 250, () => false, 100)).toEqual([[0, 100], [100, 200]]);
    expect(blocksToFetch(0, 10, 4, (i) => have.has(i), 100)).toEqual([]);
    expect(blocksToFetch(240, 260, 250, () => false, 100)).toEqual([[200, 250]]);
});

const item = (name: string, children: SidebarItem[] = [], expanded = true): SidebarItem => ({
    name,
    kind: "tag",
    search: `tag:${name}`,
    expanded,
    children,
});

test("the sidebar shows expanded children only", () => {
    const tree = [item("a", [item("a1"), item("a2", [item("a2x")], false)]), item("b")];
    expect(flattenSidebar(tree, "").map((r) => [r.item.name, r.depth])).toEqual([
        ["a", 0],
        ["a1", 1],
        ["a2", 1],
        ["b", 0],
    ]);
});

test("the sidebar filter shows matches with their parents, expanded", () => {
    const tree = [item("a", [item("a1"), item("a2", [item("deep")], false)]), item("b")];
    expect(flattenSidebar(tree, "DEE").map((r) => r.item.name)).toEqual(["a", "a2", "deep"]);
    expect(flattenSidebar(tree, "zzz")).toEqual([]);
});

test("selections go one at a time, the newest waiting, with their own list", async () => {
    const sent: [number | null, number][] = [];
    const replies: (() => void)[] = [];
    const sender = new SelectionSender((state, gen) => {
        sent.push([state.current, gen]);
        return new Promise<void>((resolve) => replies.push(resolve));
    });
    const at = (row: number) => sel([[row, row + 1]], row);
    sender.select(at(1), 5);
    sender.select(at(2), 5);
    sender.select(at(3), 5);
    expect(sent).toEqual([[1, 5]]);
    let acted = false;
    const action = sender.sent().then(() => (acted = true));
    replies.shift()!();
    await Promise.resolve();
    expect(sent).toEqual([[1, 5], [3, 5]]);
    expect(acted).toBe(false);
    replies.shift()!();
    await action;
    expect(acted).toBe(true);
    // a new list drops what was waiting
    sender.select(at(4), 5);
    sender.select(at(5), 5);
    sender.reset();
    replies.shift()!();
    await sender.sent();
    expect(sent).toEqual([[1, 5], [3, 5], [4, 5]]);
});
