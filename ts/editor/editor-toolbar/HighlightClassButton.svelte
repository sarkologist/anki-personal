<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { getPlatformString } from "@tslib/shortcuts";
    import { singleCallback } from "@tslib/typing";
    import { onMount } from "svelte";

    import Icon from "$lib/components/Icon.svelte";
    import IconButton from "$lib/components/IconButton.svelte";
    import { chevronDown, highlightColorIcon } from "$lib/components/icons";
    import Popover from "$lib/components/Popover.svelte";
    import Shortcut from "$lib/components/Shortcut.svelte";
    import WithFloating from "$lib/components/WithFloating.svelte";
    import type { FormattingNode, MatchType } from "$lib/domlib/surround";

    import { surrounder } from "../rich-text-input";
    import { context as editorToolbarContext } from "./EditorToolbar.svelte";
    import {
        findHighlightClass,
        highlightClasses,
        isHighlightClass,
    } from "./highlight-classes";

    let currentClass = highlightClasses[0]?.className ?? "";

    function matcher(
        element: HTMLElement | SVGElement,
        match: MatchType<string>,
    ): void {
        const existing = findHighlightClass(element);
        if (!existing) {
            return;
        }

        match.setCache(existing);
        match.clear((): void => {
            element.classList.remove(existing);
            if (
                element.tagName === "SPAN"
                && element.className.length === 0
                && element.style.cssText.length === 0
            ) {
                match.remove();
            }
        });
    }

    function merger(
        before: FormattingNode<string>,
        after: FormattingNode<string>,
    ): boolean {
        return before.getCache(currentClass) === after.getCache(currentClass);
    }

    function formatter(node: FormattingNode<string>): boolean {
        const className = node.getCache(currentClass);
        if (!className) {
            return false;
        }

        const extension = node.extensions.find(
            (element: HTMLElement | SVGElement): boolean =>
                element.tagName === "SPAN",
        );

        if (extension) {
            for (const cls of [...extension.classList]) {
                if (isHighlightClass(cls) && cls !== className) {
                    extension.classList.remove(cls);
                }
            }
            extension.classList.add(className);
            return false;
        }

        const span = document.createElement("span");
        span.classList.add(className);
        node.range.toDOMRange().surroundContents(span);
        return true;
    }

    const key = "highlightClass";

    const format = {
        matcher,
        merger,
        formatter,
    };

    const namedFormat = {
        key,
        name: tr.editingTextHighlightColor(),
        show: true,
        active: true,
    };

    const { removeFormats } = editorToolbarContext.get();
    removeFormats.update((formats) => [...formats, namedFormat]);

    function applyClass(className: string): void {
        currentClass = className;
        surrounder.overwriteSurround(key);
    }

    let disabled: boolean;
    let showFloating = false;
    $: if (disabled) {
        showFloating = false;
    }

    onMount(() =>
        singleCallback(
            surrounder.active.subscribe((value) => (disabled = !value)),
            surrounder.registerFormat(key, format),
        ),
    );
</script>

<WithFloating
    show={showFloating}
    closeOnInsideClick
    inline
    on:close={() => (showFloating = false)}
>
    <IconButton
        slot="reference"
        tooltip={tr.editingTextHighlightColor()}
        {disabled}
        on:click={() => (showFloating = !showFloating)}
    >
        <Icon icon={highlightColorIcon} />
        <Icon icon={chevronDown} />
    </IconButton>

    <Popover slot="floating" --popover-padding-inline="0">
        <div class="hl-chips">
            {#each highlightClasses as { className, label, swatch, keyCombination } (className)}
                <button
                    type="button"
                    class="hl-chip"
                    title="{label} ({getPlatformString(keyCombination)})"
                    style:background-color={swatch}
                    on:mousedown|preventDefault
                    on:click={() => applyClass(className)}
                >
                    {label}
                </button>
            {/each}
        </div>
    </Popover>
</WithFloating>

{#each highlightClasses as { className, keyCombination } (className)}
    <Shortcut {keyCombination} on:action={() => applyClass(className)} />
{/each}

<style lang="scss">
    .hl-chips {
        display: flex;
        gap: 4px;
        padding: 6px;
    }

    .hl-chip {
        width: 28px;
        height: 28px;
        border: 1px solid rgba(0, 0, 0, 0.25);
        border-radius: 4px;
        font-weight: 600;
        color: #fff;
        text-shadow: 0 1px 1px rgba(0, 0, 0, 0.5);
        cursor: pointer;
        padding: 0;

        &:hover {
            outline: 2px solid var(--border-focus, #0d6efd);
        }
    }
</style>
