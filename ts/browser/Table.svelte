<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The browser's table (Qt's `QTableView` over `DataModel`): only the rows in
view are drawn, and they're fetched from the shell in blocks.
-->
<script lang="ts">
    import { onDestroy, onMount, tick } from "svelte";

    import { call, type Row, type TableInfo } from "./bridge";
    import {
        blocksToFetch,
        clickRow,
        contains,
        moveCurrent,
        type Range,
        type SelectionState,
    } from "./lib";

    export let table: TableInfo;
    export let selection: SelectionState;
    export let rowHeight: number;
    export let widths: Record<string, number>;
    export let onSelect: (state: SelectionState) => void;
    export let onSort: (key: string) => void;
    export let onPopup: (menu: string, x: number, y: number) => void;
    export let onWidths: (widths: Record<string, number>) => void;
    /** The shell's list is newer than ours. */
    export let onStale: () => void;

    const BLOCK = 100;
    const OVERSCAN = 20;
    const DEFAULT_WIDTH = 140;
    const MIN_WIDTH = 50;

    let scroller: HTMLDivElement;
    let scrollTop = 0;
    let viewHeight = 400;
    let viewWidth = 600;
    let rows = new Map<number, Row>();
    /** Rows drawn from before a refresh, until they're fetched again. */
    let stale = new Set<number>();
    let fetching = new Set<number>();
    let gen = -1;

    $: if (table.gen !== gen) {
        gen = table.gen;
        rows = new Map();
        stale = new Set();
        fetching = new Set();
    }

    $: headerHeight = 24;
    $: first = Math.max(0, Math.floor(scrollTop / rowHeight) - OVERSCAN);
    $: last = Math.min(
        table.count,
        Math.ceil((scrollTop + viewHeight) / rowHeight) + OVERSCAN,
    );
    $: visible = range(first, last);
    $: fetchVisible(first, last, table.gen, rows, stale);

    function range(start: number, end: number): number[] {
        return Array.from({ length: Math.max(0, end - start) }, (_, i) => start + i);
    }

    function width(key: string): number {
        return widths[key] ?? DEFAULT_WIDTH;
    }

    // the last column takes what's left, as Qt's stretched last section
    $: columnWidths = table.columns.map((c, i) => {
        const w = width(c.key);
        if (i < table.columns.length - 1) {
            return w;
        }
        const others = table.columns.slice(0, -1).reduce((n, c) => n + width(c.key), 0);
        return Math.max(w, viewWidth - others);
    });
    $: totalWidth = columnWidths.reduce((a, b) => a + b, 0);
    $: template = columnWidths.map((w) => `${w}px`).join(" ");

    async function fetchVisible(
        start: number,
        end: number,
        forGen: number,
        have: Map<number, Row>,
        old: Set<number>,
    ): Promise<void> {
        const blocks = blocksToFetch(
            start,
            end,
            table.count,
            (i) => (have.has(i) && !old.has(i)) || fetching.has(i),
            BLOCK,
        );
        for (const [s, e] of blocks) {
            for (let i = s; i < e; i++) {
                fetching.add(i);
            }
            const out = await call<{
                gen: number;
                start: number;
                rows?: Row[];
                stale?: boolean;
            } | null>(
                `browserRows:${JSON.stringify({ gen: forGen, start: s, end: e })}`,
            );
            for (let i = s; i < e; i++) {
                fetching.delete(i);
            }
            if (out?.stale && out.gen > table.gen) {
                onStale();
                return;
            }
            if (!out || out.gen !== table.gen || !out.rows) {
                return;
            }
            out.rows.forEach((row, i) => {
                rows.set(out.start + i, row);
                stale.delete(out.start + i);
            });
            rows = rows;
        }
    }

    /** The shell's rows changed (an edit, an operation): fetch the ones in
        view again, showing the old ones meanwhile. */
    export function refresh(): void {
        stale = new Set(rows.keys());
    }

    export function scrollToRow(row: number | null): void {
        if (row === null || !scroller) {
            return;
        }
        const top = row * rowHeight;
        const bottom = top + rowHeight;
        const viewTop = scroller.scrollTop;
        const viewBottom = viewTop + scroller.clientHeight - headerHeight;
        if (top < viewTop) {
            scroller.scrollTop = top;
        } else if (bottom > viewBottom) {
            scroller.scrollTop = bottom - (scroller.clientHeight - headerHeight);
        }
    }

    export function focus(): void {
        scroller?.focus();
    }

    function select(state: SelectionState): void {
        onSelect(state);
        tick().then(() => scrollToRow(state.current));
    }

    function onRowDown(event: MouseEvent, row: number): void {
        if (event.button === 2) {
            if (!contains(selection.ranges, row)) {
                select(clickRow(selection, row, {}));
            }
            return;
        }
        if (event.button !== 0) {
            return;
        }
        scroller.focus();
        select(
            clickRow(selection, row, { shift: event.shiftKey, meta: event.metaKey }),
        );
    }

    function onContextMenu(event: MouseEvent): void {
        event.preventDefault();
        onPopup("actions", event.clientX, event.clientY);
    }

    function onKey(event: KeyboardEvent): void {
        if (event.ctrlKey || event.altKey) {
            return;
        }
        const page = Math.max(
            1,
            Math.floor((scroller.clientHeight - headerHeight) / rowHeight) - 1,
        );
        const moves: Record<string, number> = {
            ArrowDown: 1,
            ArrowUp: -1,
            PageDown: page,
            PageUp: -page,
            Home: -Infinity,
            End: Infinity,
        };
        if (event.key in moves && !event.metaKey) {
            event.preventDefault();
            select(
                moveCurrent(selection, moves[event.key], event.shiftKey, table.count),
            );
        } else if (event.metaKey && event.key === "a" && !event.shiftKey) {
            event.preventDefault();
            const all: Range[] = table.count ? [[0, table.count]] : [];
            select({
                ranges: all,
                current: selection.current,
                anchor: selection.anchor,
            });
        }
    }

    // column resizing
    let resizing: { key: string; startX: number; startWidth: number } | null = null;

    function startResize(event: PointerEvent, key: string, index: number): void {
        event.preventDefault();
        event.stopPropagation();
        (event.target as HTMLElement).setPointerCapture(event.pointerId);
        resizing = { key, startX: event.clientX, startWidth: columnWidths[index] };
    }

    function moveResize(event: PointerEvent): void {
        if (!resizing) {
            return;
        }
        const w = Math.max(
            MIN_WIDTH,
            resizing.startWidth + event.clientX - resizing.startX,
        );
        widths = { ...widths, [resizing.key]: w };
    }

    function endResize(): void {
        if (resizing) {
            onWidths({ [resizing.key]: widths[resizing.key] ?? DEFAULT_WIDTH });
            resizing = null;
        }
    }

    function onHeaderClick(key: string): void {
        if (!resizing) {
            onSort(key);
        }
    }

    function onHeaderMenu(event: MouseEvent): void {
        event.preventDefault();
        onPopup("columns", event.clientX, event.clientY);
    }

    let observer: ResizeObserver | undefined;
    onMount(() => {
        observer = new ResizeObserver(() => {
            viewHeight = scroller.clientHeight;
            viewWidth = scroller.clientWidth;
        });
        observer.observe(scroller);
    });
    onDestroy(() => observer?.disconnect());

    const colorClass = (row: Row | undefined): string =>
        row?.color ? `color-${row.color}` : "";
</script>

<div
    class="scroller"
    bind:this={scroller}
    tabindex="0"
    role="grid"
    aria-rowcount={table.count}
    on:scroll={() => (scrollTop = scroller.scrollTop)}
    on:keydown={onKey}
>
    <div
        class="header"
        style="grid-template-columns: {template}; width: {totalWidth}px;"
    >
        {#each table.columns as column, i (column.key)}
            <div
                class="th"
                class:center={column.center}
                class:sorted={column.key === table.sort_column}
                title={column.tooltip}
                role="columnheader"
                tabindex="-1"
                on:click={() => onHeaderClick(column.key)}
                on:keydown={() => {}}
                on:contextmenu={onHeaderMenu}
            >
                <span class="label">{column.label}</span>
                {#if column.key === table.sort_column && column.sortable}
                    <span class="arrow">{table.sort_backwards ? "▼" : "▲"}</span>
                {/if}
                <!-- svelte-ignore a11y-no-static-element-interactions -->
                <div
                    class="resize"
                    on:pointerdown={(e) => startResize(e, column.key, i)}
                    on:pointermove={moveResize}
                    on:pointerup={endResize}
                    on:click|stopPropagation
                ></div>
            </div>
        {/each}
    </div>
    <div
        class="body"
        style="height: {table.count * rowHeight}px; width: {totalWidth}px;"
        on:contextmenu={onContextMenu}
        role="rowgroup"
    >
        {#each visible as index (index)}
            {@const row = rows.get(index)}
            <div
                class="tr {colorClass(row)}"
                class:selected={contains(selection.ranges, index)}
                class:current={selection.current === index}
                class:disabled={row?.disabled}
                style="top: {index *
                    rowHeight}px; height: {rowHeight}px; grid-template-columns: {template};"
                role="row"
                tabindex="-1"
                aria-rowindex={index + 1}
                data-row={index}
                data-id={row?.id}
                on:mousedown={(e) => onRowDown(e, index)}
            >
                {#each table.columns as column, c (column.key)}
                    {@const cell = row?.cells[c]}
                    <div
                        class="td"
                        class:center={column.center}
                        dir={cell?.rtl ? "rtl" : undefined}
                        style={column.font && row?.font
                            ? `font-family: "${row.font}"; font-size: ${row.fontSize}px;`
                            : undefined}
                        role="gridcell"
                    >
                        {cell?.text ?? ""}
                    </div>
                {/each}
            </div>
        {/each}
    </div>
</div>

<style lang="scss">
    .scroller {
        position: relative;
        height: 100%;
        overflow: auto;
        outline: none;
        user-select: none;
        -webkit-user-select: none;
        cursor: default;
    }
    .header {
        position: sticky;
        top: 0;
        z-index: 2;
        display: grid;
        height: 24px;
        background: var(--canvas-elevated, #fff);
        border-bottom: 1px solid var(--border, #ccc);
    }
    .th {
        position: relative;
        display: flex;
        align-items: center;
        padding: 0 6px;
        overflow: hidden;
        white-space: nowrap;
        border-right: 1px solid var(--border-subtle, #e4e4e4);
        font-weight: 500;
        &.center {
            justify-content: center;
        }
        .label {
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .arrow {
            margin-left: 4px;
            font-size: 9px;
            color: var(--fg-subtle, #888);
        }
    }
    .resize {
        position: absolute;
        top: 0;
        right: -3px;
        width: 7px;
        height: 100%;
        cursor: col-resize;
        z-index: 1;
    }
    .body {
        position: relative;
    }
    .tr {
        position: absolute;
        left: 0;
        right: 0;
        display: grid;
        align-items: center;
        border-bottom: 1px solid var(--border-subtle, #eee);
        box-sizing: border-box;
    }
    .td {
        padding: 0 6px;
        overflow: hidden;
        white-space: nowrap;
        text-overflow: ellipsis;
        &.center {
            text-align: center;
        }
    }
    .color-1 {
        background: color-mix(in srgb, var(--state-marked) 25%, transparent);
    }
    .color-2 {
        background: color-mix(
            in srgb,
            var(--state-suspended, #ffef9f) 45%,
            transparent
        );
    }
    .color-3 {
        background: color-mix(in srgb, var(--flag-1) 30%, transparent);
    }
    .color-4 {
        background: color-mix(in srgb, var(--flag-2) 30%, transparent);
    }
    .color-5 {
        background: color-mix(in srgb, var(--flag-3) 30%, transparent);
    }
    .color-6 {
        background: color-mix(in srgb, var(--flag-4) 30%, transparent);
    }
    .color-7 {
        background: color-mix(in srgb, var(--flag-5) 30%, transparent);
    }
    .color-8 {
        background: color-mix(in srgb, var(--flag-6) 30%, transparent);
    }
    .color-9 {
        background: color-mix(in srgb, var(--flag-7) 30%, transparent);
    }
    .color-10 {
        background: color-mix(in srgb, var(--state-buried) 25%, transparent);
    }
    .tr.selected {
        background: var(--selected-bg, rgba(214, 214, 214, 0.7));
    }
    .scroller:focus .tr.selected {
        background: var(--highlight-bg, rgba(147, 197, 253, 0.5));
    }
    .tr.current {
        box-shadow: inset 0 0 0 1px var(--border, #999);
    }
    .tr.disabled {
        color: var(--fg-subtle, #888);
    }
</style>
