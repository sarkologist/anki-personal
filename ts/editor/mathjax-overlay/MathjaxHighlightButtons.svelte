<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { getPlatformString } from "@tslib/shortcuts";
    import { createEventDispatcher } from "svelte";

    import ButtonGroup from "$lib/components/ButtonGroup.svelte";
    import Shortcut from "$lib/components/Shortcut.svelte";

    import { highlightClasses } from "../editor-toolbar/highlight-classes";
    import { mathjaxHighlightSurround } from "./highlight";

    const dispatch = createEventDispatcher();

    function applyHighlight(className: string): void {
        dispatch("surround", mathjaxHighlightSurround(className));
    }
</script>

<ButtonGroup>
    {#each highlightClasses as { className, label, swatch, keyCombination } (className)}
        <button
            type="button"
            class="hl-chip"
            title="{tr.editingTextHighlightColor()} {label} ({getPlatformString(
                keyCombination,
            )})"
            aria-label="{tr.editingTextHighlightColor()} {label}"
            style:background-color={swatch}
            on:mousedown|preventDefault
            on:click={() => applyHighlight(className)}
        >
            {label}
        </button>

        <Shortcut {keyCombination} on:action={() => applyHighlight(className)} />
    {/each}
</ButtonGroup>

<style lang="scss">
    .hl-chip {
        min-width: calc(var(--buttons-size) * 0.75);
        height: var(--buttons-size);
        border: 1px solid rgba(0, 0, 0, 0.25);
        border-radius: 4px;
        padding: 0;
        font-size: calc(var(--buttons-size) * 0.42);
        font-weight: 600;
        line-height: 1;
        color: #fff;
        text-shadow: 0 1px 1px rgba(0, 0, 0, 0.5);
        cursor: pointer;

        &:hover {
            outline: 2px solid var(--border-focus, #0d6efd);
            z-index: 1;
        }
    }
</style>
