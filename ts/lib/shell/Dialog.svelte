<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

A small in-page modal, as the native shell's WebKit views may not show
window.alert/confirm/prompt. Enter picks the first choice, Escape cancels.
-->
<script lang="ts">
    import * as tr from "@generated/ftl";

    export let title: string;
    export let choices: { label: string; action: () => void }[];
    export let onCancel: () => void;

    const titleId = `dialog-${Math.random().toString(36).slice(2)}`;

    function onKeydown(event: KeyboardEvent): void {
        if (event.key === "Escape") {
            event.preventDefault();
            onCancel();
        }
    }
</script>

<svelte:window on:keydown={onKeydown} />

<div class="backdrop">
    <div class="dialog" role="dialog" aria-modal="true" aria-labelledby={titleId}>
        <h5 id={titleId}>{title}</h5>
        <form on:submit|preventDefault={() => choices[0]?.action()}>
            <div class="body">
                <slot />
            </div>
            <div class="buttons">
                <button type="button" on:click={onCancel}>{tr.actionsCancel()}</button>
                {#each choices as choice, index}
                    {#if index === 0}
                        <button type="submit" class="primary">{choice.label}</button>
                    {:else}
                        <button type="button" on:click={choice.action}>
                            {choice.label}
                        </button>
                    {/if}
                {/each}
            </div>
        </form>
    </div>
</div>

<style lang="scss">
    @use "$lib/sass/elevation" as *;
    @use "$lib/sass/button-mixins" as button;

    .backdrop {
        position: fixed;
        inset: 0;
        z-index: 100;
        display: flex;
        align-items: flex-start;
        justify-content: center;
        padding-top: 15vh;
        background: rgba(0, 0, 0, 0.3);
    }

    .dialog {
        min-width: 20em;
        max-width: calc(100vw - 2em);
        padding: 1em 1.25em;
        color: var(--fg);
        background: var(--canvas-elevated);
        border: 1px solid var(--border-subtle);
        border-radius: var(--border-radius-large);
        @include elevation(5);
    }

    h5 {
        margin: 0 0 0.75em;
    }

    .body {
        margin-bottom: 1em;
        overflow-wrap: anywhere;

        :global(input[type="text"]) {
            width: 100%;
            box-sizing: border-box;
            padding: 0.3em 0.5em;
        }
    }

    .buttons {
        display: flex;
        flex-wrap: wrap;
        justify-content: flex-end;
        gap: 0.5em;

        button {
            padding: 6px 12px;
        }

        .primary {
            @include button.base($primary: true, $with-disabled: false);
        }
    }
</style>
