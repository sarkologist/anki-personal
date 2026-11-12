<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The browser's own part of its window (`browser.py`'s search bar, sidebar
and table); the editor beside it is the stock editor page.
-->
<script lang="ts">
    import { onDestroy, onMount } from "svelte";

    import { call, type Init, type Message, type SelectionInfo } from "./bridge";
    import { invert, SelectionSender, type SelectionState } from "./lib";
    import SearchBar from "./SearchBar.svelte";
    import Sidebar from "./Sidebar.svelte";
    import Table from "./Table.svelte";

    export let init: Init;

    let table = init.table;
    let selection = fromInfo(init.selection);
    let sidebarItems = init.sidebar;
    let history = init.history;
    let addonMenus = init.addonMenus;
    let searchText = init.prompt;
    let sidebarWidth = init.sidebarWidth || 220;
    let cardWidths = { ...init.cardWidths };
    let noteWidths = { ...init.noteWidths };
    const labels = init.labels;

    let tableView: Table;
    let sidebarView: Sidebar;
    let searchBar: SearchBar;

    $: widths = table.notes_mode ? noteWidths : cardWidths;

    function fromInfo(info: SelectionInfo): SelectionState {
        return { ranges: info.ranges, current: info.current, anchor: info.current };
    }

    const sender = new SelectionSender((state, gen) =>
        call(
            `browserSelect:${JSON.stringify({
                gen,
                ranges: state.ranges,
                current: state.current,
            })}`,
        ),
    );

    function onSelect(state: SelectionState): void {
        selection = state;
        sender.select(state, table.gen);
    }

    const selectionSent = () => sender.sent();

    /** An action on the selection, once the shell has it. */
    async function act(action: string): Promise<void> {
        await selectionSent();
        call(`browserAction:${action}`);
    }

    function receive(msg: Message): void {
        switch (msg.type) {
            case "table":
                // pushes from different threads may arrive out of order
                if (msg.table.gen < table.gen) {
                    break;
                }
                // a selection made in the old list means nothing in this one
                sender.reset();
                table = msg.table;
                selection = fromInfo(msg.selection);
                searchText = msg.prompt;
                setTimeout(() => tableView?.scrollToRow(selection.current), 0);
                break;
            case "selection":
                if (msg.selection.gen === table.gen) {
                    selection = fromInfo(msg.selection);
                    setTimeout(() => tableView?.scrollToRow(selection.current), 0);
                }
                break;
            case "refresh":
                tableView?.refresh();
                break;
            case "sidebar":
                sidebarItems = msg.sidebar;
                break;
            case "addonMenus":
                addonMenus = msg.menus;
                break;
            case "key":
                pageAction(msg.action);
                break;
        }
    }

    /** The shell's list changed under us (a push we missed): get it. */
    async function resync(): Promise<void> {
        const msg = await call<Message | null>("browserTable");
        if (msg) {
            receive(msg);
        }
    }

    async function search(): Promise<void> {
        const out = await call<{ ok?: boolean; history?: string[]; error?: string }>(
            `browserSearch:${searchText}`,
        );
        if (out?.history) {
            history = out.history;
        }
    }

    async function popup(
        menu: string,
        x: number,
        y: number,
        item?: unknown,
    ): Promise<void> {
        // its items act on the selection
        await selectionSent();
        // the frame is at the window's left edge, below nothing
        const frame = window.frameElement?.getBoundingClientRect();
        call(
            `browserPopup:${JSON.stringify({
                menu,
                x: x + (frame?.left ?? 0),
                y: y + (frame?.top ?? 0),
                item: item ?? null,
            })}`,
        );
    }

    function popupBelow(menu: string, event: MouseEvent): void {
        const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
        popup(menu, rect.left, rect.bottom);
    }

    /** The actions the page does itself; the rest go to the shell. */
    function pageAction(action: string): boolean {
        switch (action) {
            case "find":
                searchBar?.focus();
                searchBar?.select();
                return true;
            case "sidebarFilter":
                sidebarView?.focusFilter();
                return true;
            case "sidebar":
                sidebarView?.focusTree();
                return true;
            case "cardList":
                tableView?.focus();
                return true;
            case "nextCard":
            case "previousCard": {
                if (!table.count) {
                    return true;
                }
                const delta = action === "nextCard" ? 1 : -1;
                const from = selection.current ?? -1;
                const row = Math.max(0, Math.min(table.count - 1, from + delta));
                onSelect({ ranges: [[row, row + 1]], current: row, anchor: row });
                setTimeout(() => tableView?.scrollToRow(row), 0);
                return true;
            }
            case "selectAll":
                onSelect({
                    ranges: table.count ? [[0, table.count]] : [],
                    current: selection.current,
                    anchor: selection.anchor,
                });
                return true;
            case "invertSelection":
                onSelect({
                    ranges: invert(selection.ranges, table.count),
                    current: selection.current,
                    anchor: selection.anchor,
                });
                return true;
        }
        return false;
    }

    const typing = (target: EventTarget | null): boolean =>
        target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;

    /** Keys that edit text stay in text boxes. */
    const TEXT_KEYS = new Set([
        "selectAll",
        "invertSelection",
        "undo",
        "redo",
        "delete",
        "preview",
    ]);

    function onKey(event: KeyboardEvent): void {
        for (const k of init.shortcuts) {
            if (
                event.code === k.code &&
                event.metaKey === k.meta &&
                event.shiftKey === k.shift &&
                event.altKey === k.alt &&
                !event.ctrlKey
            ) {
                if (typing(event.target) && TEXT_KEYS.has(k.action)) {
                    return;
                }
                event.preventDefault();
                event.stopPropagation();
                if (!pageAction(k.action)) {
                    act(k.action);
                }
                return;
            }
        }
        if (event.key === "Escape" && !typing(event.target)) {
            event.preventDefault();
            act("close");
        }
    }

    function onWidths(changed: Record<string, number>): void {
        if (table.notes_mode) {
            noteWidths = { ...noteWidths, ...changed };
        } else {
            cardWidths = { ...cardWidths, ...changed };
        }
        call(`browserColumnWidths:${JSON.stringify(changed)}`);
    }

    // sidebar width
    let dragging = false;
    function startSidebarDrag(event: PointerEvent): void {
        event.preventDefault();
        (event.target as HTMLElement).setPointerCapture(event.pointerId);
        dragging = true;
    }
    function moveSidebarDrag(event: PointerEvent): void {
        if (dragging) {
            sidebarWidth = Math.max(
                140,
                Math.min(event.clientX, window.innerWidth - 240),
            );
        }
    }
    function endSidebarDrag(): void {
        if (dragging) {
            dragging = false;
            call(`browserSidebarWidth:${Math.round(sidebarWidth)}`);
        }
    }

    onMount(() => {
        window.browserReceive = receive;
        document.addEventListener("keydown", onKey, true);
        searchBar?.focus();
        setTimeout(() => tableView?.scrollToRow(selection.current), 0);
    });
    onDestroy(() => {
        document.removeEventListener("keydown", onKey, true);
        if (window.browserReceive === receive) {
            delete window.browserReceive;
        }
    });
</script>

<div class="page" style="--sidebar-width: {sidebarWidth}px">
    <div class="toolbar">
        <SearchBar
            bind:this={searchBar}
            bind:value={searchText}
            {history}
            placeholder={labels.search}
            onSearch={search}
            onDown={() => tableView?.focus()}
        />
        <div class="mode" role="group" title={labels.toggleMode}>
            <button
                class:active={!table.notes_mode}
                on:click={() => table.notes_mode && act("toggleMode")}
            >
                {labels.cards}
            </button>
            <button
                class:active={table.notes_mode}
                on:click={() => !table.notes_mode && act("toggleMode")}
            >
                {labels.notes}
            </button>
        </div>
        {#each addonMenus as title}
            <button class="menu" on:click={(e) => popupBelow(`addon:${title}`, e)}>
                {title} ▾
            </button>
        {/each}
        <button class="menu" title="Actions" on:click={(e) => popupBelow("actions", e)}>
            ⋯
        </button>
    </div>
    <div class="main">
        <div class="side">
            <Sidebar
                bind:this={sidebarView}
                bind:items={sidebarItems}
                filterLabel={labels.sidebarFilter}
                onPopup={(item, x, y) => popup("sidebar", x, y, item)}
            />
        </div>
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div
            class="drag"
            on:pointerdown={startSidebarDrag}
            on:pointermove={moveSidebarDrag}
            on:pointerup={endSidebarDrag}
        ></div>
        <div class="table">
            <Table
                bind:this={tableView}
                {table}
                {selection}
                rowHeight={init.rowHeight}
                {widths}
                {onSelect}
                onSort={(key) => call(`browserSort:${key}`)}
                onPopup={(menu, x, y) => popup(menu, x, y)}
                {onWidths}
                onStale={resync}
            />
        </div>
    </div>
</div>

<style lang="scss">
    .page {
        display: flex;
        flex-direction: column;
        height: 100%;
    }
    .toolbar {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 6px 8px;
        border-bottom: 1px solid var(--border, #ccc);
    }
    .mode {
        display: flex;
        button {
            border-radius: 0;
            &:first-child {
                border-radius: 5px 0 0 5px;
            }
            &:last-child {
                border-radius: 0 5px 5px 0;
            }
            &.active {
                background: var(--button-primary-bg, #3478f6);
                color: #fff;
            }
        }
    }
    .main {
        flex: 1;
        display: grid;
        grid-template-columns: var(--sidebar-width) 4px minmax(0, 1fr);
        min-height: 0;
    }
    .side {
        min-width: 0;
        overflow: hidden;
        border-right: 1px solid var(--border-subtle, #e4e4e4);
    }
    .drag {
        cursor: col-resize;
    }
    .table {
        min-width: 0;
        min-height: 0;
    }
</style>
