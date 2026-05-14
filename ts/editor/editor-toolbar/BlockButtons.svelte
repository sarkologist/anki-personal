<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import * as tr from "@generated/ftl";
    import { preventDefault } from "@tslib/events";
    import { getPlatformString, registerShortcut } from "@tslib/shortcuts";
    import { onMount } from "svelte";

    import ButtonGroup from "$lib/components/ButtonGroup.svelte";
    import ButtonGroupItem, {
        createProps,
        setSlotHostContext,
        updatePropsList,
    } from "$lib/components/ButtonGroupItem.svelte";
    import ButtonToolbar from "$lib/components/ButtonToolbar.svelte";
    import DynamicallySlottable from "$lib/components/DynamicallySlottable.svelte";
    import Icon from "$lib/components/Icon.svelte";
    import IconButton from "$lib/components/IconButton.svelte";
    import {
        indentIcon,
        justifyCenterIcon,
        justifyFullIcon,
        justifyLeftIcon,
        justifyRightIcon,
        listOptionsIcon,
        olIcon,
        outdentIcon,
        ulIcon,
    } from "$lib/components/icons";
    import Popover from "$lib/components/Popover.svelte";
    import WithFloating from "$lib/components/WithFloating.svelte";

    import { execCommandWithUndecoratedElements } from "../decorated-elements";
    import { context } from "../NoteEditor.svelte";
    import { editingInputIsRichText } from "../rich-text-input";
    import CommandIconButton from "./CommandIconButton.svelte";

    export let api = {};

    const outdentKeyCombination = "Control+Shift+,";
    async function editIndent(command: "indent" | "outdent"): Promise<void> {
        if (!$focusedInput || !editingInputIsRichText($focusedInput)) {
            return;
        }

        const input = $focusedInput;
        const element = await input.element;

        input.pushUndoSnapshot();
        execCommandWithUndecoratedElements(element, command);
        input.focus();
    }

    const indentKeyCombination = "Control+Shift+.";

    onMount(() => {
        registerShortcut((event: KeyboardEvent) => {
            preventDefault(event);
            void editIndent("indent");
        }, indentKeyCombination);
        registerShortcut((event: KeyboardEvent) => {
            preventDefault(event);
            void editIndent("outdent");
        }, outdentKeyCombination);
    });

    const { focusedInput } = context.get();

    $: disabled = !$focusedInput || !editingInputIsRichText($focusedInput);

    let showFloating = false;
    $: if (disabled) {
        showFloating = false;
    }

    const rtl = window.getComputedStyle(document.body).direction === "rtl";

    const justificationKeys = [
        "justifyLeft",
        "justifyCenter",
        "justifyRight",
        "justifyFull",
    ];

    const listKeys = ["insertUnorderedList", "insertOrderedList"];
</script>

<ButtonGroup>
    <DynamicallySlottable
        slotHost={ButtonGroupItem}
        {createProps}
        {updatePropsList}
        {setSlotHostContext}
        {api}
    >
        <ButtonGroupItem>
            <CommandIconButton
                key="insertUnorderedList"
                tooltip={tr.editingUnorderedList()}
                shortcut="Control+,"
                modeVariantKeys={listKeys}
            >
                <Icon icon={ulIcon} />
            </CommandIconButton>
        </ButtonGroupItem>

        <ButtonGroupItem>
            <CommandIconButton
                key="insertOrderedList"
                tooltip={tr.editingOrderedList()}
                shortcut="Control+."
                modeVariantKeys={listKeys}
            >
                <Icon icon={olIcon} />
            </CommandIconButton>
        </ButtonGroupItem>

        <ButtonGroupItem>
            <WithFloating
                show={showFloating}
                inline
                on:close={() => (showFloating = false)}
                let:asReference
            >
                <span class="block-buttons" use:asReference>
                    <IconButton
                        tooltip={tr.editingAlignment()}
                        {disabled}
                        on:click={() => (showFloating = !showFloating)}
                    >
                        <Icon icon={listOptionsIcon} />
                    </IconButton>
                </span>

                <Popover slot="floating" --popover-padding-inline="0">
                    <ButtonToolbar wrap={false}>
                        <ButtonGroup>
                            <DynamicallySlottable
                                slotHost={ButtonGroupItem}
                                {createProps}
                                {updatePropsList}
                                {setSlotHostContext}
                                {api}
                            >
                                <ButtonGroupItem>
                                    <CommandIconButton
                                        key="justifyLeft"
                                        tooltip={tr.editingAlignLeft()}
                                        modeVariantKeys={justificationKeys}
                                    >
                                        <Icon icon={justifyLeftIcon} />
                                    </CommandIconButton>
                                </ButtonGroupItem>

                                <ButtonGroupItem>
                                    <CommandIconButton
                                        key="justifyCenter"
                                        tooltip={tr.editingCenter()}
                                        modeVariantKeys={justificationKeys}
                                    >
                                        <Icon icon={justifyCenterIcon} />
                                    </CommandIconButton>
                                </ButtonGroupItem>

                                <ButtonGroupItem>
                                    <CommandIconButton
                                        key="justifyRight"
                                        tooltip={tr.editingAlignRight()}
                                        modeVariantKeys={justificationKeys}
                                    >
                                        <Icon icon={justifyRightIcon} />
                                    </CommandIconButton>
                                </ButtonGroupItem>

                                <ButtonGroupItem>
                                    <CommandIconButton
                                        key="justifyFull"
                                        tooltip={tr.editingJustify()}
                                        modeVariantKeys={justificationKeys}
                                    >
                                        <Icon icon={justifyFullIcon} />
                                    </CommandIconButton>
                                </ButtonGroupItem>
                            </DynamicallySlottable>
                        </ButtonGroup>

                        <ButtonGroup>
                            <IconButton
                                tooltip="{tr.editingOutdent()} ({getPlatformString(
                                    outdentKeyCombination,
                                )})"
                                {disabled}
                                flipX={rtl}
                                on:click={() => editIndent("outdent")}
                                --border-left-radius="5px"
                                --border-right-radius="0px"
                            >
                                <Icon icon={outdentIcon} />
                            </IconButton>

                            <IconButton
                                tooltip="{tr.editingIndent()} ({getPlatformString(
                                    indentKeyCombination,
                                )})"
                                {disabled}
                                flipX={rtl}
                                on:click={() => editIndent("indent")}
                                --border-right-radius="5px"
                            >
                                <Icon icon={indentIcon} />
                            </IconButton>
                        </ButtonGroup>
                    </ButtonToolbar>
                </Popover>
            </WithFloating>
        </ButtonGroupItem>
    </DynamicallySlottable>
</ButtonGroup>

<style lang="scss">
    .block-buttons {
        line-height: 1;
    }
</style>
