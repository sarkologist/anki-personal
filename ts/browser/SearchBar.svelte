<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

`browser.py`'s search bar: an editable combo box of past searches. The
history list is drawn by the page: WebKit draws a `<datalist>` with its
own indicator and puts its popup in the wrong place inside a frame.
-->
<script lang="ts">
    export let value: string;
    export let history: string[];
    export let placeholder: string;
    export let onSearch: () => void;
    /** Down arrow with no history to open: on to the table. */
    export let onDown: () => void;

    let input: HTMLInputElement;
    let list: HTMLElement;
    let open = false;
    let active = -1;

    export function focus(): void {
        input?.focus();
    }

    export function select(): void {
        input?.select();
    }

    function show(): void {
        open = true;
        active = history.indexOf(value);
        setTimeout(scrollToActive, 0);
    }

    function hide(): void {
        open = false;
        active = -1;
    }

    function toggle(): void {
        if (open) {
            hide();
        } else if (history.length) {
            show();
        }
        input.focus();
    }

    function pick(entry: string): void {
        value = entry;
        hide();
        input.focus();
        onSearch();
    }

    function scrollToActive(): void {
        (list?.children[active] as HTMLElement | undefined)?.scrollIntoView({
            block: "nearest",
        });
    }

    function move(delta: number): void {
        active = Math.max(0, Math.min(history.length - 1, active + delta));
        setTimeout(scrollToActive, 0);
    }

    function onKey(event: KeyboardEvent): void {
        if (open) {
            switch (event.key) {
                case "ArrowDown":
                case "ArrowUp":
                    event.preventDefault();
                    move(event.key === "ArrowDown" ? 1 : -1);
                    return;
                case "Enter":
                    event.preventDefault();
                    if (active >= 0) {
                        pick(history[active]);
                    } else {
                        hide();
                        onSearch();
                    }
                    return;
                case "Escape":
                    event.preventDefault();
                    event.stopPropagation();
                    hide();
                    return;
            }
            return;
        }
        if (event.key === "Enter") {
            event.preventDefault();
            onSearch();
        } else if (event.key === "ArrowDown") {
            event.preventDefault();
            if (history.length) {
                show();
            } else {
                onDown();
            }
        }
    }
</script>

<div class="combo">
    <input
        class="search"
        type="search"
        {placeholder}
        bind:value
        bind:this={input}
        on:keydown={onKey}
        on:blur={hide}
        on:input={hide}
        spellcheck="false"
        autocomplete="off"
        role="combobox"
        aria-expanded={open}
        aria-controls="browser-history"
    />
    <button
        class="toggle"
        tabindex="-1"
        aria-label="History"
        disabled={!history.length}
        on:mousedown|preventDefault={toggle}
    >
        ▾
    </button>
    {#if open}
        <!-- the input keeps focus (and the list stays open) through clicks on it -->
        <ul
            id="browser-history"
            class="history"
            role="listbox"
            bind:this={list}
            on:mousedown|preventDefault
        >
            {#each history as entry, i}
                <li
                    role="option"
                    aria-selected={i === active}
                    class:active={i === active}
                    title={entry}
                    on:mousedown={() => pick(entry)}
                    on:mouseenter={() => (active = i)}
                >
                    {entry}
                </li>
            {/each}
        </ul>
    {/if}
</div>

<style lang="scss">
    .combo {
        position: relative;
        display: flex;
        flex: 1;
        min-width: 120px;
    }
    .search {
        flex: 1;
        min-width: 0;
        padding-right: 22px;
    }
    .toggle {
        position: absolute;
        right: 1px;
        top: 1px;
        bottom: 1px;
        width: 20px;
        padding: 0;
        margin: 0;
        border: none;
        background: none;
        color: var(--fg-subtle, #888);
        &:disabled {
            opacity: 0.4;
        }
    }
    .history {
        position: absolute;
        top: 100%;
        left: 0;
        right: 0;
        z-index: 10;
        max-height: 60vh;
        overflow-y: auto;
        margin: 2px 0 0;
        padding: 3px 0;
        list-style: none;
        background: var(--canvas-elevated, #fff);
        border: 1px solid var(--border, #ccc);
        border-radius: 5px;
        box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
        li {
            padding: 2px 8px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            cursor: default;
            &.active {
                background: var(--selected-bg, #cde);
            }
        }
    }
</style>
