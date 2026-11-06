<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";
    // not $lib/components/icons, which would pull every icon into /toolbar
    import RefreshIcon from "@mdi/svg/svg/refresh.svg?component";
    import { onMount } from "svelte";

    import { provideShellGlobal } from "$lib/shell/shell";

    import { parseSyncState, type SyncState, syncIndicatorClass } from "./lib";

    /** The reviewer's style: translucent, without raised buttons. */
    export let flat = false;

    let sync: SyncState = { state: "none", loggedIn: false };

    function setSyncState(value: unknown): void {
        sync = parseSyncState(value);
    }

    provideShellGlobal("shellSetSyncState", setSyncState);
    onMount(() => bridgeCommand("syncState", setSyncState));

    const items = [
        { cmd: "decks", label: tr.actionsDecks(), key: "D" },
        { cmd: "add", label: tr.actionsAdd(), key: "A" },
        { cmd: "stats", label: tr.qtMiscStats(), key: "T" },
    ];
</script>

<div class="header" class:flat>
    <div class="toolbar">
        {#each items as item}
            <button
                class="hitem"
                tabindex="-1"
                title={tr.actionsShortcutKey({ val: item.key })}
                on:click={() => bridgeCommand(item.cmd)}
            >
                {item.label}
            </button>
        {/each}
        <button
            class="hitem {syncIndicatorClass(sync)}"
            tabindex="-1"
            title={tr.actionsShortcutKey({ val: "Y" })}
            on:click={() => bridgeCommand("sync")}
        >
            {tr.qtMiscSync()}
            <span class="spinner" class:spin={sync.state === "syncing"}>
                <RefreshIcon />
            </span>
        </button>
    </div>
</div>

<style lang="scss">
    @use "$lib/sass/elevation" as *;

    .header {
        display: flex;
        justify-content: center;
        -webkit-user-select: none;
        user-select: none;
        margin-bottom: 5px;
    }

    .toolbar {
        white-space: nowrap;
        overflow: hidden;
        border-bottom-left-radius: var(--border-radius-medium);
        border-bottom-right-radius: var(--border-radius-medium);
        transition: all var(--transition) ease-in-out;
        background: var(--canvas-elevated);
        @include elevation(1);
        &:hover {
            @include elevation(2);
        }

        .flat & {
            background: var(--canvas-glass);
            backdrop-filter: blur(var(--blur));
            @include elevation(1, $opacity-boost: -0.1);
            &:hover {
                @include elevation(1, $opacity-boost: -0.1);
            }
        }
    }

    .hitem {
        font-weight: bold;
        padding: 5px 12px;
        color: var(--fg);
        display: inline-block;
        border: 1px solid transparent;
        border-radius: 0;
        background: var(--canvas-glass);
        box-shadow: none;
        cursor: pointer;
        &:first-child {
            padding-left: 18px;
        }
        &:last-child {
            padding-right: 18px;
        }
        &:hover {
            background: var(--canvas-glass);
            border: 1px solid var(--border-subtle);
        }
        &:active {
            background: var(--canvas-inset);
        }
        &:focus {
            outline: 0;
        }

        .flat & {
            background: transparent;
            &:hover {
                background: transparent;
            }
        }
    }

    .normal-sync {
        color: var(--state-new);
    }

    .full-sync {
        color: var(--state-learn);
    }

    @keyframes spin {
        0% {
            transform: rotate(0deg);
        }
        100% {
            transform: rotate(360deg);
        }
    }

    .spinner {
        display: inline-block;
        width: 0;
        height: 16px;
        margin-bottom: -3px;
        visibility: hidden;
        :global(svg) {
            width: 16px;
            height: 16px;
            fill: currentColor;
            vertical-align: top;
        }
    }

    .spin {
        width: 16px;
        visibility: visible;
        animation: spin 2s linear infinite;
    }
</style>
