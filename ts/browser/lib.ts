// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/** Rows `[start, end)`. */
export type Range = [number, number];

export interface SelectionState {
    ranges: Range[];
    current: number | null;
    /** Where a Shift+click or Shift+arrow extends from. */
    anchor: number | null;
}

export function normalizeRanges(ranges: Range[]): Range[] {
    const sorted = ranges.filter(([s, e]) => e > s).sort((a, b) => a[0] - b[0]);
    const out: Range[] = [];
    for (const [s, e] of sorted) {
        const last = out[out.length - 1];
        if (last && s <= last[1]) {
            last[1] = Math.max(last[1], e);
        } else {
            out.push([s, e]);
        }
    }
    return out;
}

export function count(ranges: Range[]): number {
    return ranges.reduce((n, [s, e]) => n + e - s, 0);
}

export function contains(ranges: Range[], row: number): boolean {
    return ranges.some(([s, e]) => row >= s && row < e);
}

function without(ranges: Range[], row: number): Range[] {
    return normalizeRanges(
        ranges.flatMap(([s, e]): Range[] => row >= s && row < e ? [[s, row], [row + 1, e]] : [[s, e]]),
    );
}

export interface Modifiers {
    shift?: boolean;
    meta?: boolean;
}

/** A click on `row`, as in Qt's table: Shift extends, Cmd toggles. */
export function clickRow(state: SelectionState, row: number, mods: Modifiers): SelectionState {
    if (mods.shift && state.anchor !== null) {
        const [s, e] = [Math.min(state.anchor, row), Math.max(state.anchor, row) + 1];
        return { ranges: [[s, e]], current: row, anchor: state.anchor };
    }
    if (mods.meta) {
        const ranges = contains(state.ranges, row)
            ? without(state.ranges, row)
            : normalizeRanges([...state.ranges, [row, row + 1]]);
        return { ranges, current: row, anchor: row };
    }
    return { ranges: [[row, row + 1]], current: row, anchor: row };
}

/** Move the current row by `delta` (±Infinity: to the ends), selecting
    it, or extending from the anchor. */
export function moveCurrent(
    state: SelectionState,
    delta: number,
    extend: boolean,
    total: number,
): SelectionState {
    if (total === 0) {
        return { ranges: [], current: null, anchor: null };
    }
    const from = state.current ?? -1;
    const row = Math.max(0, Math.min(total - 1, from === -1 && delta > 0 ? 0 : from + delta));
    return clickRow(state, row, { shift: extend });
}

export function invert(ranges: Range[], total: number): Range[] {
    const out: Range[] = [];
    let at = 0;
    for (const [s, e] of normalizeRanges(ranges)) {
        if (s > at) {
            out.push([at, s]);
        }
        at = e;
    }
    if (at < total) {
        out.push([at, total]);
    }
    return out;
}

/** The blocks (of `size` rows) to fetch so rows `start..end` are there. */
export function blocksToFetch(
    start: number,
    end: number,
    total: number,
    have: (row: number) => boolean,
    size: number,
): Range[] {
    const out: Range[] = [];
    end = Math.min(end, total);
    for (let block = Math.floor(start / size) * size; block < end; block += size) {
        const blockEnd = Math.min(block + size, total);
        for (let i = Math.max(block, start); i < Math.min(blockEnd, end); i++) {
            if (!have(i)) {
                out.push([block, blockEnd]);
                break;
            }
        }
    }
    return out;
}

export interface SidebarItem {
    name: string;
    kind: string;
    search: string;
    fullName?: string;
    id?: number;
    color?: string;
    expanded: boolean;
    children: SidebarItem[];
}

export interface SidebarRow {
    item: SidebarItem;
    depth: number;
    /** Whether its children are shown. */
    open: boolean;
}

/** The rows the sidebar shows: expanded items' children, or with a
    filter, the matching items and their parents (`SidebarSearchBar`). */
export function flattenSidebar(items: SidebarItem[], filter: string): SidebarRow[] {
    const needle = filter.trim().toLowerCase();
    const out: SidebarRow[] = [];
    const matches = (item: SidebarItem): boolean =>
        item.name.toLowerCase().includes(needle) || item.children.some(matches);
    const walk = (items: SidebarItem[], depth: number) => {
        for (const item of items) {
            if (needle && !matches(item)) {
                continue;
            }
            const open = needle
                ? item.children.some(matches)
                : item.expanded && item.children.length > 0;
            out.push({ item, depth, open });
            if (open) {
                walk(item.children, depth + 1);
            }
        }
    };
    walk(items, 0);
    return out;
}

/** Sends selections to the shell one at a time, each with the list (`gen`)
    it was made in; while one is on its way only the newest waits. Actions
    on the selection wait for `sent()`. */
export class SelectionSender {
    private sending = false;
    private queued: { state: SelectionState; gen: number } | null = null;
    private idle: (() => void)[] = [];

    constructor(private send: (state: SelectionState, gen: number) => Promise<unknown>) {}

    select(state: SelectionState, gen: number): void {
        this.queued = { state, gen };
        this.next();
    }

    /** A new list: selections made in the old one are dropped. */
    reset(): void {
        this.queued = null;
        this.next();
    }

    sent(): Promise<void> {
        return new Promise((resolve) => {
            this.idle.push(resolve);
            this.next();
        });
    }

    private next(): void {
        if (this.sending) {
            return;
        }
        if (!this.queued) {
            this.idle.splice(0).forEach((f) => f());
            return;
        }
        const { state, gen } = this.queued;
        this.queued = null;
        this.sending = true;
        this.send(state, gen).then(() => {
            this.sending = false;
            this.next();
        });
    }
}
