<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { onDestroy, tick } from "svelte";

    import { CooldownTimer } from "./cooldown-timer";
    import type { LegacyLatexKind } from "./legacy-latex-preview";
    import { requestLegacyLatexPreview } from "./legacy-latex-preview";

    export let latex: string;
    export let kind: LegacyLatexKind;

    let host: HTMLElement;
    let dataUrl: string | null = null;
    let alt = "";
    let errorText = "";
    let loading = false;
    let placeholder = "LaTeX";
    let renderSerial = 0;

    const debouncer = new CooldownTimer(500);

    function notifyResize(): void {
        setTimeout(() => host?.dispatchEvent(new Event("resize")));
    }

    async function renderPreview(
        source: string,
        sourceKind: LegacyLatexKind,
    ): Promise<void> {
        const serial = ++renderSerial;

        dataUrl = null;
        alt = "";
        errorText = "";

        if (source.trim().length === 0) {
            loading = false;
            await tick();
            notifyResize();
            return;
        }

        loading = true;
        const result = await requestLegacyLatexPreview(sourceKind, source);

        if (serial !== renderSerial) {
            return;
        }

        loading = false;

        if (result.ok) {
            dataUrl = result.dataUrl;
            alt = result.alt;
            errorText = "";
        } else {
            dataUrl = null;
            alt = "";
            errorText = result.errorText;
        }

        await tick();
        notifyResize();
    }

    $: debouncer.schedule(() => renderPreview(latex, kind));
    $: title = errorText || latex;
    $: {
        if (loading) {
            placeholder = "LaTeX...";
        } else if (errorText) {
            placeholder = "LaTeX error";
        } else {
            placeholder = "LaTeX";
        }
    }

    export function moveCaretAfter(position?: [number, number]): void {
        host.dispatchEvent(
            new CustomEvent("movecaretafterlatex", {
                detail: { image: host, position },
                bubbles: true,
                composed: true,
            }),
        );
    }

    export function selectAll(): void {
        host.dispatchEvent(
            new CustomEvent("selectlatexall", {
                detail: host,
                bubbles: true,
                composed: true,
            }),
        );
    }

    onDestroy(() => {
        renderSerial++;
    });
</script>

<span
    bind:this={host}
    class="legacy-latex"
    class:block={kind === "display"}
    class:error={!!errorText}
    data-anki="latex"
    role="img"
    aria-label="LaTeX"
    {title}
    on:dragstart|preventDefault
>
    {#if dataUrl}
        <img
            class="legacy-latex-preview"
            class:block={kind === "display"}
            src={dataUrl}
            {alt}
            draggable="false"
            on:load={notifyResize}
        />
    {:else}
        <span class="legacy-latex-placeholder">{placeholder}</span>
    {/if}
</span>

<style lang="scss">
    :global(anki-latex) {
        white-space: pre;
    }

    .legacy-latex {
        display: inline-block;
        vertical-align: middle;
        line-height: normal;
    }

    .legacy-latex.block {
        display: block;
        width: fit-content;
        margin: 1rem auto;
        text-align: center;
    }

    .legacy-latex-preview {
        max-width: 100%;
        vertical-align: middle;
    }

    .legacy-latex-preview.block {
        display: block;
    }

    .legacy-latex-placeholder {
        display: inline-block;
        padding: 0.05rem 0.35rem;
        border: 1px solid var(--border);
        border-radius: 4px;
        background: var(--canvas-code);
        color: var(--fg-subtle);
        font-size: 0.85em;
        line-height: 1.4;
    }

    .legacy-latex.error .legacy-latex-placeholder {
        color: var(--danger);
        border-color: var(--danger);
    }
</style>
