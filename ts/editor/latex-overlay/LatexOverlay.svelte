<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { on } from "@tslib/events";
    import { promiseWithResolver } from "@tslib/promise";
    import type { Callback } from "@tslib/typing";
    import { singleCallback } from "@tslib/typing";
    import type CodeMirrorLib from "codemirror";
    import { tick } from "svelte";
    import { writable } from "svelte/store";

    import Popover from "$lib/components/Popover.svelte";
    import Shortcut from "$lib/components/Shortcut.svelte";
    import WithFloating from "$lib/components/WithFloating.svelte";
    import WithOverlay from "$lib/components/WithOverlay.svelte";
    import { placeCaretAfter } from "$lib/domlib/place-caret";
    import { isComposing } from "$lib/sveltelib/composition";

    import { LegacyLatex } from "../../editable/legacy-latex-element.svelte";
    import type { LegacyLatexKind } from "../../editable/legacy-latex-preview";
    import type { EditingInputAPI } from "../EditingArea.svelte";
    import HandleBackground from "../HandleBackground.svelte";
    import { context } from "../NoteEditor.svelte";
    import type { RichTextInputAPI } from "../rich-text-input";
    import { editingInputIsRichText } from "../rich-text-input";
    import { legacyLatexToMathjaxElement } from "./convert-to-mathjax";
    import LatexButtons from "./LatexButtons.svelte";
    import LatexEditor from "./LatexEditor.svelte";

    const { focusedInput } = context.get();

    let cleanup: Callback;
    let richTextInput: RichTextInputAPI | null = null;
    let allowPromise = Promise.resolve();
    let isClozeField: boolean = true;

    async function initialize(input: EditingInputAPI | null): Promise<void> {
        cleanup?.();

        const isRichText = input && editingInputIsRichText(input);

        if (isRichText) {
            const container = await input.element;

            cleanup = singleCallback(
                on(container, "click", showOverlayIfLatexClicked),
                on(container, "movecaretafterlatex" as any, showOnAutofocus),
                on(container, "selectlatexall" as any, showSelectAll),
            );
            isClozeField = input.isClozeField;
        }

        await allowPromise;

        if (!isRichText) {
            richTextInput = null;
            return;
        }

        richTextInput = input;
    }

    $: initialize($focusedInput);

    let activePreview: HTMLElement | null = null;
    let latexElement: HTMLElement | null = null;

    let allowResubscription: Callback;
    let unsubscribe: Callback;

    let selectAll = false;
    let position: CodeMirrorLib.Position | undefined = undefined;

    const code = writable("");

    function showOverlay(preview: HTMLElement, pos?: CodeMirrorLib.Position) {
        if ($isComposing) {
            return;
        }

        const [promise, allowResolve] = promiseWithResolver<void>();

        allowPromise = promise;
        allowResubscription = singleCallback(
            richTextInput!.preventResubscription(),
            allowResolve,
        );

        position = pos;
        activePreview = preview;
        latexElement = activePreview.closest(LegacyLatex.tagName)!;
        errorMessage = activePreview.title;

        code.set(latexElement.dataset.latex ?? "");
        unsubscribe = code.subscribe((value: string) => {
            latexElement!.dataset.latex = value;
        });
    }

    function placeHandle(after: boolean): void {
        richTextInput!.editable.focusHandler.flushCaret();

        if (after) {
            (latexElement as any).placeCaretAfter();
        } else {
            (latexElement as any).placeCaretBefore();
        }
    }

    async function resetHandle(): Promise<void> {
        selectAll = false;
        position = undefined;

        allowResubscription?.();

        if (activePreview && latexElement) {
            clear();
        }
    }

    function clear(): void {
        unsubscribe();
        activePreview = null;
        latexElement = null;
    }

    let errorMessage: string;
    let cleanupPreviewResize: Callback | null = null;

    async function updateErrorMessage(): Promise<void> {
        errorMessage = activePreview!.title;
    }

    async function updatePreviewResizeCallback(preview: HTMLElement | null) {
        cleanupPreviewResize?.();
        cleanupPreviewResize = null;

        if (!preview) {
            return;
        }

        cleanupPreviewResize = on(preview, "resize", updateErrorMessage);
    }

    $: updatePreviewResizeCallback(activePreview);

    async function showOverlayIfLatexClicked({ target }: Event): Promise<void> {
        const preview =
            target instanceof HTMLElement
                ? target.closest<HTMLElement>('[data-anki="latex"]')
                : null;

        if (preview) {
            resetHandle();
            showOverlay(preview);
        }
    }

    async function showOnAutofocus({
        detail,
    }: CustomEvent<{
        image: HTMLElement;
        position?: [number, number];
    }>): Promise<void> {
        let position: CodeMirrorLib.Position | undefined = undefined;

        if (detail.position) {
            const [line, ch] = detail.position;
            position = { line, ch };
        }

        if (detail.image.dataset.anki === "latex") {
            showOverlay(detail.image, position);
        }
    }

    async function showSelectAll({ detail }: CustomEvent<HTMLElement>): Promise<void> {
        if (detail.dataset.anki !== "latex") {
            return;
        }

        selectAll = true;
        showOverlay(detail);
    }

    let kind: LegacyLatexKind;
    $: kind = latexElement?.dataset.latexKind === "display" ? "display" : "inline";
    $: isDisplay = kind === "display";

    async function updateKind(newKind: LegacyLatexKind): Promise<void> {
        latexElement!.dataset.latexKind = newKind;
        kind = newKind;

        await tick();
    }

    async function convertToMathjax(source: string): Promise<void> {
        if (!latexElement) {
            return;
        }

        const mathjaxElement = legacyLatexToMathjaxElement(source, isDisplay);
        const replacementTarget =
            latexElement.parentElement?.tagName === "ANKI-FRAME"
                ? latexElement.parentElement
                : latexElement;

        richTextInput?.pushUndoSnapshot();
        richTextInput?.editable.focusHandler.flushCaret();
        replacementTarget.replaceWith(mathjaxElement);

        selectAll = false;
        position = undefined;
        allowResubscription?.();
        clear();

        await tick();

        placeCaretAfter(
            mathjaxElement.parentElement?.tagName === "ANKI-FRAME"
                ? mathjaxElement.parentElement
                : mathjaxElement,
        );
    }

    const acceptShortcut = "Enter";
    const newlineShortcut = "Shift+Enter";
</script>

<div class="latex-overlay">
    {#if activePreview && latexElement}
        <WithOverlay
            reference={activePreview}
            padding={isDisplay ? 10 : 3}
            keepOnKeyup
            let:position={positionOverlay}
        >
            <WithFloating
                reference={activePreview}
                offset={20}
                keepOnKeyup
                portalTarget={document.body}
                on:close={resetHandle}
            >
                <Popover slot="floating" let:position={positionFloating}>
                    <LatexEditor
                        {acceptShortcut}
                        {newlineShortcut}
                        {code}
                        {selectAll}
                        {position}
                        on:moveoutstart={() => {
                            placeHandle(false);
                            resetHandle();
                        }}
                        on:moveoutend={() => {
                            placeHandle(true);
                            resetHandle();
                        }}
                        on:close={() => {
                            placeHandle(true);
                            resetHandle();
                        }}
                        let:editor={latexEditor}
                    >
                        <Shortcut
                            keyCombination={acceptShortcut}
                            on:action={() => {
                                placeHandle(true);
                                resetHandle();
                            }}
                        />

                        <LatexButtons
                            {isDisplay}
                            {isClozeField}
                            on:setinline={async () => {
                                await updateKind("inline");
                                positionOverlay();
                                positionFloating();
                            }}
                            on:setdisplay={async () => {
                                await updateKind("display");
                                positionOverlay();
                                positionFloating();
                            }}
                            on:delete={async () => {
                                if (activePreview) {
                                    placeCaretAfter(activePreview);
                                    latexElement?.remove();
                                    clear();
                                }
                            }}
                            on:convert={async () => {
                                const editor = await latexEditor.editor;
                                await convertToMathjax(editor.getValue());
                            }}
                            on:surround={async ({ detail }) => {
                                const editor = await latexEditor.editor;
                                const { prefix, suffix } = detail;

                                editor.replaceSelection(
                                    prefix + editor.getSelection() + suffix,
                                );
                            }}
                        />
                    </LatexEditor>
                </Popover>
            </WithFloating>

            <svelte:fragment slot="overlay">
                <HandleBackground
                    tooltip={errorMessage}
                    --handle-background-color="var(--code-bg)"
                />
            </svelte:fragment>
        </WithOverlay>
    {/if}
</div>
