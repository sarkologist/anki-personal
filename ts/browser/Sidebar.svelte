<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The browser's sidebar (`SidebarTreeView`): saved searches, today, flags,
card states, decks, note types and tags. A click searches for the item;
Cmd adds it, Shift ORs it, Option negates it.
-->
<script lang="ts">
    import { call } from "./bridge";
    import { flattenSidebar, type SidebarItem, type SidebarRow } from "./lib";

    export let items: SidebarItem[];
    export let filter = "";
    export let filterLabel: string;
    export let onPopup: (item: SidebarItem, x: number, y: number) => void;

    let filterInput: HTMLInputElement;
    let list: HTMLDivElement;
    let current = -1;

    $: rows = flattenSidebar(items, filter);
    $: if (current >= rows.length) {
        current = rows.length - 1;
    }

    export function focusFilter(): void {
        filterInput?.focus();
        filterInput?.select();
    }

    export function focusTree(): void {
        if (current < 0 && rows.length) {
            current = 0;
        }
        list?.focus();
    }

    function toggle(row: SidebarRow): void {
        if (!row.item.children.length || filter.trim()) {
            return;
        }
        row.item.expanded = !row.item.expanded;
        items = items;
        call(
            `browserSidebarExpand:${JSON.stringify({
                item: wire(row.item),
                expanded: row.item.expanded,
            })}`,
        );
    }

    /** What the shell needs of an item (not its children). */
    function wire(item: SidebarItem) {
        return {
            kind: item.kind,
            search: item.search,
            fullName: item.fullName ?? "",
            id: item.id ?? null,
            name: item.name,
        };
    }

    function search(row: SidebarRow, event: MouseEvent | KeyboardEvent): void {
        if (!row.item.search) {
            toggle(row);
            return;
        }
        call(
            `browserSidebarClick:${JSON.stringify({
                search: row.item.search,
                mods: { cmd: event.metaKey, shift: event.shiftKey, alt: event.altKey },
            })}`,
        );
    }

    function onClick(event: MouseEvent, index: number): void {
        current = index;
        search(rows[index], event);
    }

    function onMenu(event: MouseEvent, index: number): void {
        event.preventDefault();
        current = index;
        onPopup(
            wire(rows[index].item) as unknown as SidebarItem,
            event.clientX,
            event.clientY,
        );
    }

    function onKey(event: KeyboardEvent): void {
        const row = rows[current];
        if (event.key === "ArrowDown") {
            current = Math.min(rows.length - 1, current + 1);
        } else if (event.key === "ArrowUp") {
            current = Math.max(0, current - 1);
        } else if (event.key === "ArrowRight" && row && !row.open) {
            toggle(row);
        } else if (event.key === "ArrowLeft" && row && row.open) {
            toggle(row);
        } else if (event.key === "Enter" && row) {
            search(row, event);
        } else {
            return;
        }
        event.preventDefault();
        list.querySelector(`[data-index="${current}"]`)?.scrollIntoView({
            block: "nearest",
        });
    }

    function onFilterKey(event: KeyboardEvent): void {
        if (event.key === "Enter" || event.key === "ArrowDown") {
            event.preventDefault();
            focusTree();
        } else if (event.key === "Escape" && filter) {
            event.preventDefault();
            event.stopPropagation();
            filter = "";
        }
    }

    const root = (kind: string) => kind.endsWith("Root");
</script>

<div class="sidebar">
    <input
        class="filter"
        type="search"
        placeholder={filterLabel}
        bind:value={filter}
        bind:this={filterInput}
        on:keydown={onFilterKey}
        spellcheck="false"
        autocomplete="off"
    />
    <div class="tree" tabindex="0" role="tree" bind:this={list} on:keydown={onKey}>
        {#each rows as row, i (row.item.kind + ":" + (row.item.fullName || row.item.search || row.item.name) + ":" + i)}
            <div
                class="item"
                class:root={root(row.item.kind)}
                class:current={i === current}
                style="padding-left: {6 + row.depth * 14}px"
                role="treeitem"
                aria-selected={i === current}
                aria-expanded={row.item.children.length ? row.open : undefined}
                tabindex="-1"
                data-index={i}
                title={row.item.search}
                on:click={(e) => onClick(e, i)}
                on:keydown={() => {}}
                on:contextmenu={(e) => onMenu(e, i)}
            >
                <!-- svelte-ignore a11y-click-events-have-key-events a11y-no-static-element-interactions -->
                <span
                    class="twisty"
                    class:hidden={!row.item.children.length}
                    on:click|stopPropagation={() => toggle(row)}
                >
                    {row.open ? "▾" : "▸"}
                </span>
                {#if row.item.color}
                    <span class="dot" style="background: var({row.item.color})"></span>
                {/if}
                <span class="name">{row.item.name}</span>
            </div>
        {/each}
    </div>
</div>

<style lang="scss">
    .sidebar {
        display: flex;
        flex-direction: column;
        height: 100%;
        min-width: 0;
    }
    .filter {
        margin: 6px;
        box-sizing: border-box;
    }
    .tree {
        flex: 1;
        overflow: auto;
        outline: none;
        user-select: none;
        -webkit-user-select: none;
        cursor: default;
        padding-bottom: 8px;
    }
    .item {
        display: flex;
        align-items: center;
        gap: 4px;
        height: 22px;
        white-space: nowrap;
        &.root {
            font-weight: 600;
            margin-top: 4px;
        }
        &.current {
            background: var(--selected-bg, rgba(214, 214, 214, 0.7));
        }
    }
    .tree:focus .item.current {
        background: var(--highlight-bg, rgba(147, 197, 253, 0.5));
    }
    .twisty {
        width: 12px;
        font-size: 10px;
        color: var(--fg-subtle, #888);
        text-align: center;
        flex: none;
        &.hidden {
            visibility: hidden;
        }
    }
    .dot {
        width: 9px;
        height: 9px;
        border-radius: 50%;
        flex: none;
    }
    .name {
        overflow: hidden;
        text-overflow: ellipsis;
    }
</style>
