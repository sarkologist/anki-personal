<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { getPlatformString } from "@tslib/shortcuts";
    import { wrapInternal } from "@tslib/wrap";

    import DropdownItem from "$lib/components/DropdownItem.svelte";
    import Icon from "$lib/components/Icon.svelte";
    import IconButton from "$lib/components/IconButton.svelte";
    import { functionIcon, mathIcon } from "$lib/components/icons";
    import Popover from "$lib/components/Popover.svelte";
    import Shortcut from "$lib/components/Shortcut.svelte";
    import type { IconData } from "$lib/components/types";
    import WithFloating from "$lib/components/WithFloating.svelte";

    import { mathjaxConfig } from "../../editable/mathjax-element.svelte";
    import { undecorateFragment } from "../decorated-elements";
    import {
        convertLegacyLatexToInlineMathjax,
        flattenBlocksToNewlines,
    } from "../latex-overlay/convert-to-mathjax";
    import { context as noteEditorContext } from "../NoteEditor.svelte";
    import type { RichTextInputAPI } from "../rich-text-input";
    import { editingInputIsRichText } from "../rich-text-input";

    const noteEditor = noteEditorContext.get();
    const { focusedInput } = noteEditor;
    $: richTextAPI = $focusedInput as RichTextInputAPI;

    async function surround(
        front: string,
        back: string,
        flattenBlocks = false,
    ): Promise<void> {
        const element = await richTextAPI.element;
        const normalize = flattenBlocks
            ? (fragment: DocumentFragment): void => {
                  undecorateFragment(fragment);
                  flattenBlocksToNewlines(fragment);
              }
            : undecorateFragment;
        wrapInternal(element, front, back, false, normalize);
    }

    function onMathjaxInline(): void {
        if (mathjaxConfig.enabled) {
            surround("<anki-mathjax focusonmount>", "</anki-mathjax>");
        } else {
            surround("\\(", "\\)");
        }
    }

    function onMathjaxBlock(): void {
        if (mathjaxConfig.enabled) {
            surround('<anki-mathjax block="true" focusonmount>', "</anki-mathjax>");
        } else {
            surround("\\[", "\\]");
        }
    }

    function onMathjaxChemistry(): void {
        if (mathjaxConfig.enabled) {
            surround('<anki-mathjax focusonmount="0,4">\\ce{', "}</anki-mathjax>");
        } else {
            surround("\\(\\ce{", "}\\)");
        }
    }

    function onLatex(): void {
        surround("[latex]", "[/latex]");
    }

    function onLatexEquation(): void {
        surround(
            '<anki-latex data-latex-kind="inline" focusonmount>',
            "</anki-latex>",
            true,
        );
    }

    function onLatexMathEnv(): void {
        surround(
            '<anki-latex data-latex-kind="display" focusonmount>',
            "</anki-latex>",
            true,
        );
    }

    async function onConvertAllToMathjax(): Promise<void> {
        await noteEditor.transformFieldsWithUndo(convertLegacyLatexToInlineMathjax);
    }

    type LatexItem = [() => void | Promise<void>, string | null, string, IconData?];

    const dropdownItems: LatexItem[] = [
        [onMathjaxInline, "Control+M, M", tr.editingMathjaxInline()],
        [onMathjaxBlock, "Control+M, E", tr.editingMathjaxBlock()],
        [onMathjaxChemistry, "Control+M, C", tr.editingMathjaxChemistry()],
        [onLatex, "Control+T, T", tr.editingLatex()],
        [onLatexEquation, "Control+T, E", tr.editingLatexEquation()],
        [onLatexMathEnv, "Control+T, M", tr.editingLatexMathEnv()],
        [onConvertAllToMathjax, "Control+M, X", tr.editingConvertToMathjax(), mathIcon],
    ];

    $: disabled = !$focusedInput || !editingInputIsRichText($focusedInput);

    let showFloating = false;
    $: if (disabled) {
        showFloating = false;
    }
</script>

<WithFloating
    show={showFloating}
    closeOnInsideClick
    inline
    on:close={() => (showFloating = false)}
>
    <IconButton
        slot="reference"
        tooltip={tr.editingEquations()}
        {disabled}
        on:click={() => (showFloating = !showFloating)}
    >
        <Icon icon={functionIcon} />
    </IconButton>

    <Popover slot="floating" --popover-padding-inline="0">
        {#each dropdownItems as [callback, keyCombination, label, icon]}
            <DropdownItem on:click={() => setTimeout(callback, 100)}>
                {#if icon}
                    <Icon {icon} />
                {/if}
                <span>{label}</span>
                {#if keyCombination}
                    <span class="ms-auto ps-2 shortcut">
                        {getPlatformString(keyCombination)}
                    </span>
                {/if}
            </DropdownItem>
        {/each}
    </Popover>
</WithFloating>

{#each dropdownItems as [callback, keyCombination]}
    {#if keyCombination}
        <Shortcut {keyCombination} on:action={callback} />
    {/if}
{/each}

<style lang="scss">
    .shortcut {
        font: Verdana;
    }
</style>
