<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The deck list, as qt/aqt/deckbrowser.py drew it.
-->
<script lang="ts">
    import { SetDeckCollapsedRequest_Scope } from "@generated/anki/decks_pb";
    import {
        addDeck,
        newDeck,
        removeDecks,
        renameDeck,
        reparentDecks,
        setCurrentDeck,
        setDeckCollapsed,
    } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";
    import { onDestroy } from "svelte";

    import Icon from "$lib/components/Icon.svelte";
    import { cogIcon } from "$lib/components/icons";
    import BottomBar from "$lib/shell/BottomBar.svelte";
    import Dialog from "$lib/shell/Dialog.svelte";
    import { errorMessage, provideShellGlobal, quiet } from "$lib/shell/shell";

    import Toolbar from "../toolbar/Toolbar.svelte";
    import { type DeckBrowserData, loadDeckBrowser } from "./data";
    import {
        canReparent,
        type DeckRow,
        deckNameExists,
        findDeckNode,
        flattenDeckTree,
    } from "./lib";

    export let initial: DeckBrowserData;

    let { tree, currentDeckId, currentDeckName, studiedToday } = initial;
    $: rows = flattenDeckTree(tree);

    let error = "";
    let notice = "";
    /** Add-ons' HTML under the list (`deck_browser_will_render_content`). */
    let addonStats = "";

    function loadAddonStats(): void {
        bridgeCommand<string | null>("addonContent", (html) => {
            addonStats = html ?? "";
        });
    }
    loadAddonStats();
    let noticeTimer: ReturnType<typeof setTimeout> | undefined;

    async function refresh(): Promise<void> {
        try {
            ({ tree, currentDeckId, currentDeckName, studiedToday } =
                await loadDeckBrowser());
            error = "";
            loadAddonStats();
        } catch (err) {
            error = errorMessage(err);
        }
    }

    provideShellGlobal("shellRefresh", () => void refresh());

    /** Run a collection change, then reload the list. */
    async function run(op: () => Promise<unknown>): Promise<void> {
        try {
            await op();
        } catch (err) {
            error = errorMessage(err);
            return;
        }
        await refresh();
    }

    function showNotice(text: string): void {
        notice = text;
        clearTimeout(noticeTimer);
        noticeTimer = setTimeout(() => (notice = ""), 3000);
    }

    onDestroy(() => clearTimeout(noticeTimer));

    function openDeck(event: MouseEvent, row: DeckRow): void {
        if (event.shiftKey) {
            // select without opening, as deckbrowser.py's "select:"
            run(() => setCurrentDeck({ did: row.id }, quiet));
        } else {
            bridgeCommand(`open:${row.id}`);
        }
    }

    function toggleCollapsed(row: DeckRow): void {
        const node = findDeckNode(tree, row.id);
        if (!node) {
            return;
        }
        node.collapsed = !node.collapsed;
        tree = tree;
        run(() =>
            setDeckCollapsed(
                {
                    deckId: row.id,
                    collapsed: node.collapsed,
                    scope: SetDeckCollapsedRequest_Scope.REVIEWER,
                },
                quiet,
            ),
        );
    }

    // Options menu

    let menu: { row: DeckRow; x: number; y: number } | null = null;

    function openMenu(event: MouseEvent, row: DeckRow): void {
        event.stopPropagation();
        const rect = (event.currentTarget as HTMLElement).getBoundingClientRect();
        menu = { row, x: rect.left, y: rect.bottom };
    }

    function closeMenu(): void {
        menu = null;
    }

    function menuAction(action: (row: DeckRow) => void): () => void {
        return () => {
            const row = menu?.row;
            menu = null;
            if (row) {
                action(row);
            }
        };
    }

    function placeMenu(node: HTMLElement): void {
        // keep the menu inside the window
        const rect = node.getBoundingClientRect();
        if (rect.right > window.innerWidth) {
            node.style.left = `${Math.max(0, window.innerWidth - rect.width - 4)}px`;
        }
        if (rect.bottom > window.innerHeight) {
            node.style.top = `${Math.max(0, rect.top - rect.height - 24)}px`;
        }
    }

    // Dialogs

    type DialogState =
        | { kind: "rename"; row: DeckRow }
        | { kind: "delete"; row: DeckRow }
        | { kind: "create" };

    let dialog: DialogState | null = null;
    let dialogText = "";

    function closeDialog(): void {
        dialog = null;
    }

    function startRename(row: DeckRow): void {
        dialogText = row.fullName;
        dialog = { kind: "rename", row };
    }

    function finishRename(row: DeckRow): void {
        const newName = dialogText.trim();
        dialog = null;
        if (newName && newName !== row.fullName) {
            run(() => renameDeck({ deckId: row.id, newName }, quiet));
        }
    }

    function startDelete(row: DeckRow): void {
        dialog = { kind: "delete", row };
    }

    function finishDelete(row: DeckRow): void {
        dialog = null;
        run(async () => {
            const { count } = await removeDecks({ dids: [row.id] }, quiet);
            showNotice(
                tr.browsingCardsDeletedWithDeckname({ count, deckName: row.fullName }),
            );
        });
    }

    function startCreate(): void {
        dialogText = currentDeckName;
        dialog = { kind: "create" };
    }

    function finishCreate(): void {
        const name = dialogText.trim();
        dialog = null;
        // as add_normal_deck_with_name, an existing deck is left as it is
        if (!name || deckNameExists(tree, name)) {
            return;
        }
        run(async () => {
            const deck = await newDeck({}, quiet);
            deck.name = name;
            await addDeck(deck, quiet);
        });
    }

    function autofocus(input: HTMLInputElement): void {
        input.focus();
        input.select();
    }

    // Drag a deck onto another to reparent it, or onto the header to move it
    // to the top level.

    let dragging: bigint | null = null;
    let dropTarget: bigint | null = null;

    function draggableDeck(node: HTMLElement, id: bigint) {
        node.draggable = true;
        const onStart = (event: DragEvent) => {
            dragging = id;
            menu = null;
            if (event.dataTransfer) {
                event.dataTransfer.effectAllowed = "move";
                event.dataTransfer.setData("text/plain", String(id));
            }
        };
        const onEnd = () => {
            dragging = null;
            dropTarget = null;
        };
        node.addEventListener("dragstart", onStart);
        node.addEventListener("dragend", onEnd);
        return {
            update(newId: bigint) {
                id = newId;
            },
            destroy() {
                node.removeEventListener("dragstart", onStart);
                node.removeEventListener("dragend", onEnd);
            },
        };
    }

    function dropZone(node: HTMLElement, target: bigint) {
        const onOver = (event: DragEvent) => {
            if (dragging !== null && canReparent(tree, dragging, target)) {
                event.preventDefault();
                dropTarget = target;
            }
        };
        const onLeave = (event: DragEvent) => {
            if (dropTarget === target && !node.contains(event.relatedTarget as Node)) {
                dropTarget = null;
            }
        };
        const onDrop = (event: DragEvent) => {
            event.preventDefault();
            const source = dragging;
            dragging = null;
            dropTarget = null;
            if (source !== null && canReparent(tree, source, target)) {
                run(() =>
                    reparentDecks({ deckIds: [source], newParent: target }, quiet),
                );
            }
        };
        node.addEventListener("dragover", onOver);
        node.addEventListener("dragleave", onLeave);
        node.addEventListener("drop", onDrop);
        return {
            update(newTarget: bigint) {
                target = newTarget;
            },
            destroy() {
                node.removeEventListener("dragover", onOver);
                node.removeEventListener("dragleave", onLeave);
                node.removeEventListener("drop", onDrop);
            },
        };
    }
</script>

<svelte:window
    on:click={closeMenu}
    on:blur={closeMenu}
    on:resize={closeMenu}
    on:scroll={closeMenu}
/>

<Toolbar />

<div class="deck-browser">
    {#if error}
        <div class="error">{error}</div>
    {/if}

    <table>
        <thead>
            <tr
                class="top-level-drag-row"
                class:drag-hover={dropTarget === 0n}
                use:dropZone={0n}
            >
                <th class="name-header">{tr.decksDeck()}</th>
                <th class="count">{tr.actionsNew()}</th>
                <th class="count">{tr.decksLearnHeader()}</th>
                <th class="count">{tr.decksReviewHeader()}</th>
                <th class="optscol"></th>
            </tr>
        </thead>
        <tbody>
            {#each rows as row (row.id)}
                <tr
                    class="deck"
                    class:current={row.id === currentDeckId}
                    class:drag-hover={dropTarget === row.id}
                    use:draggableDeck={row.id}
                    use:dropZone={row.id}
                >
                    <td class="decktd">
                        <span
                            class="indent"
                            style:width="{(row.level - 1) * 1.5}em"
                        ></span>
                        {#if row.hasChildren}
                            <button
                                class="plain collapse"
                                on:click={() => toggleCollapsed(row)}
                            >
                                {row.collapsed ? "+" : "−"}
                            </button>
                        {:else}
                            <span class="collapse"></span>
                        {/if}
                        <button
                            class="plain deck-name"
                            class:filtered={row.filtered}
                            on:click={(event) => openDeck(event, row)}
                        >
                            {row.name}
                        </button>
                    </td>
                    <td class="count">
                        <span class={row.newCount ? "new-count" : "zero-count"}>
                            {row.newCount}
                        </span>
                    </td>
                    <td class="count">
                        <span class={row.learnCount ? "learn-count" : "zero-count"}>
                            {row.learnCount}
                        </span>
                    </td>
                    <td class="count">
                        <span class={row.reviewCount ? "review-count" : "zero-count"}>
                            {row.reviewCount}
                        </span>
                    </td>
                    <td class="opts">
                        <button
                            class="plain gears"
                            class:open={menu?.row.id === row.id}
                            aria-label={tr.actionsOptions()}
                            on:click={(event) => openMenu(event, row)}
                        >
                            <Icon icon={cogIcon} />
                        </button>
                    </td>
                </tr>
            {/each}
        </tbody>
    </table>

    {#if notice}
        <div class="notice">{notice}</div>
    {/if}

    <div class="studied-today">{studiedToday}</div>
    <!-- trusted: from the user's own add-ons, as in deckbrowser.py -->
    {@html addonStats}
</div>

<BottomBar>
    <button on:click={startCreate}>{tr.decksCreateDeck()}</button>
</BottomBar>

{#if menu}
    <div
        class="menu"
        role="menu"
        style:left="{menu.x}px"
        style:top="{menu.y}px"
        use:placeMenu
    >
        <button class="plain" role="menuitem" on:click={menuAction(startRename)}>
            {tr.actionsRename()}
        </button>
        <button
            class="plain"
            role="menuitem"
            on:click={menuAction((row) => bridgeCommand(`opts:${row.id}`))}
        >
            {tr.actionsOptions()}
        </button>
        <button class="plain" role="menuitem" on:click={menuAction(startDelete)}>
            {tr.actionsDelete()}
        </button>
    </div>
{/if}

{#if dialog?.kind === "rename"}
    {@const row = dialog.row}
    <Dialog
        title={tr.actionsRename()}
        choices={[{ label: tr.helpOk(), action: () => finishRename(row) }]}
        onCancel={closeDialog}
    >
        <label>
            {tr.decksNewDeckName()}
            <input type="text" bind:value={dialogText} use:autofocus />
        </label>
    </Dialog>
{:else if dialog?.kind === "create"}
    <Dialog
        title={tr.decksCreateDeck()}
        choices={[{ label: tr.helpOk(), action: finishCreate }]}
        onCancel={closeDialog}
    >
        <label>
            {tr.decksNewDeckName()}
            <input type="text" bind:value={dialogText} use:autofocus />
        </label>
    </Dialog>
{:else if dialog?.kind === "delete"}
    {@const row = dialog.row}
    <Dialog
        title={tr.decksDeleteDeck()}
        choices={[{ label: tr.actionsDelete(), action: () => finishDelete(row) }]}
        onCancel={closeDialog}
    >
        <b>{row.fullName}</b>
    </Dialog>
{/if}

<style lang="scss">
    @use "$lib/sass/elevation" as *;

    :global(body) {
        margin: 0;
        color: var(--fg);
        background: var(--canvas);
    }

    .deck-browser {
        display: flex;
        flex-direction: column;
        align-items: center;
        margin: 2em 1em 1em 1em;
        -webkit-user-select: none;
        user-select: none;
    }

    .plain,
    .plain:hover,
    .plain:active,
    .plain:focus {
        background: none;
        border: none;
        border-radius: 0;
        box-shadow: none;
        padding: 0;
        margin: 0;
        color: inherit;
        font: inherit;
        text-align: inherit;
    }

    table {
        padding: 1rem;
        border-collapse: separate;
        border-spacing: 0;
        border: 1px solid var(--border-subtle);
        border-radius: var(--border-radius-medium);
        background: var(--canvas-glass);
        @include elevation(1, $opacity-boost: -0.08);
        &:hover {
            @include elevation(2);
        }
    }

    th {
        border-bottom: 1px solid var(--border-subtle);
        padding: 3px 3px 5px 3px;
        text-align: start;
    }

    td {
        white-space: nowrap;
        padding: 4px 12px;
    }

    tr.drag-hover td,
    tr.drag-hover th {
        border-bottom: 1px solid var(--border);
    }

    tr.deck.current td,
    tr.deck:hover td {
        background: var(--border-subtle);
        &:first-child {
            border-start-start-radius: var(--border-radius-medium);
            border-end-start-radius: var(--border-radius-medium);
        }
        &:last-child {
            border-start-end-radius: var(--border-radius-medium);
            border-end-end-radius: var(--border-radius-medium);
        }
        .gears {
            visibility: visible;
        }
    }

    .decktd {
        min-width: 15em;
        max-width: calc(100vw - 300px);
        overflow: hidden;
    }

    .indent {
        display: inline-block;
    }

    .collapse {
        display: inline-block;
        width: 1em;
        color: var(--fg);
        cursor: pointer;
    }

    .deck-name {
        min-width: 5em;
        display: inline-block;
        color: var(--fg);
        cursor: pointer;
        &:hover {
            text-decoration: underline;
        }
        &.filtered {
            color: var(--fg-link);
        }
    }

    .count {
        min-width: 4em;
        text-align: end;
    }

    .optscol {
        width: 2em;
    }

    .opts {
        text-align: center;
    }

    .gears {
        visibility: hidden;
        cursor: pointer;
        opacity: 0.5;
        line-height: 0;
        &.open {
            visibility: visible;
        }
        :global(svg) {
            width: 1em;
            height: 1em;
            fill: var(--fg);
        }
    }

    .new-count {
        color: var(--state-new);
    }

    .learn-count {
        color: var(--state-learn);
    }

    .review-count {
        color: var(--state-review);
    }

    .zero-count {
        color: var(--fg-faint);
    }

    .studied-today {
        margin: 2em 0;
        text-align: center;
    }

    .notice {
        margin-top: 1em;
        color: var(--fg-subtle);
    }

    .error {
        margin-bottom: 1em;
        padding: 0.5em 1em;
        border: 1px solid var(--accent-danger);
        border-radius: var(--border-radius);
        color: var(--accent-danger);
        -webkit-user-select: text;
        user-select: text;
    }

    .menu {
        position: fixed;
        z-index: 50;
        display: flex;
        flex-direction: column;
        min-width: 8em;
        padding: 4px 0;
        background: var(--canvas-elevated);
        border: 1px solid var(--border-subtle);
        border-radius: var(--border-radius);
        @include elevation(4);

        button {
            padding: 4px 16px;
            cursor: pointer;
            &:hover {
                background: var(--border-subtle);
            }
        }
    }
</style>
