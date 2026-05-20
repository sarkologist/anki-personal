<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script context="module" lang="ts">
    import { registerPackage } from "@tslib/runtime-require";

    import lifecycleHooks from "$lib/sveltelib/lifecycle-hooks";

    import type { AgentSelectedTextContext } from "../agent-selection";
    import type { CodeMirrorAPI } from "../CodeMirror.svelte";
    import type { EditingInputAPI, FocusableInputAPI } from "../EditingArea.svelte";

    export interface PlainTextInputAPI extends EditingInputAPI {
        name: "plain-text";
        moveCaretToEnd(): void;
        setStoredContentWithUndo(storedHtml: string): Promise<void>;
        syncFromStoredContent(): void;
        toggle(): boolean;
        codeMirror: CodeMirrorAPI;
    }

    export const parsingInstructions: string[] = [];
    export const closeHTMLTags = writable(true);

    const [lifecycle, instances, setupLifecycleHooks] =
        lifecycleHooks<PlainTextInputAPI>();

    registerPackage("anki/PlainTextInput", {
        lifecycle,
        instances,
    });
</script>

<script lang="ts">
    import { singleCallback } from "@tslib/typing";
    import { onMount, tick } from "svelte";
    import { writable } from "svelte/store";

    import { pageTheme } from "$lib/sveltelib/theme";

    import { plainTextAgentSelectionContext } from "../agent-selection";
    import { baseOptions, gutterOptions, htmlanki } from "../code-mirror";
    import CodeMirror from "../CodeMirror.svelte";
    import { context as editingAreaContext } from "../EditingArea.svelte";
    import { Flag } from "../helpers";
    import { context as noteEditorContext } from "../NoteEditor.svelte";
    import removeProhibitedTags from "./remove-prohibited";
    import { storedToUndecorated, undecoratedToStored } from "./transform";

    export let hidden = false;
    export let fieldCollapsed = false;
    export const focusFlag = new Flag();
    export let fieldIndex: number;
    export let fieldName: string;
    export let onAgentSelectedTextContext: (
        context: AgentSelectedTextContext | null,
    ) => void = () => {};

    $: configuration = {
        mode: htmlanki,
        ...baseOptions,
        ...gutterOptions,
        ...{ autoCloseTags: $closeHTMLTags },
    };

    const { focusedInput } = noteEditorContext.get();
    const { editingInputs, content } = editingAreaContext.get();

    function storedToCode(storedHtml: string): string {
        return removeProhibitedTags(storedToUndecorated(storedHtml));
    }

    let latestStoredContent = $content;
    let settingCodeFromContent = false;
    let settingContentFromCode = false;
    const code = writable(storedToCode(latestStoredContent));

    let codeMirror = {} as CodeMirrorAPI;

    async function focus(): Promise<void> {
        const editor = await codeMirror.editor;
        editor.focus();
    }

    async function moveCaretToEnd(): Promise<void> {
        const editor = await codeMirror.editor;
        editor.setCursor(editor.lineCount(), 0);
    }

    async function setStoredContentWithUndo(storedHtml: string): Promise<void> {
        latestStoredContent = storedHtml;
        await codeMirror.replaceValueWithUndo(storedToCode(storedHtml));
    }

    function inputIsVisible(): boolean {
        return !(hidden || fieldCollapsed);
    }

    function syncCodeFromStoredContent(): void {
        settingCodeFromContent = true;
        try {
            code.set(storedToCode(latestStoredContent));
        } finally {
            settingCodeFromContent = false;
        }
    }

    function syncFromStoredContent(): void {
        syncCodeFromStoredContent();
    }

    async function refocus(): Promise<void> {
        const editor = (await codeMirror.editor) as any;
        editor.display.input.blur();

        focus();
        moveCaretToEnd();
    }

    function toggle(): boolean {
        hidden = !hidden;
        return hidden;
    }

    async function getInputAPI(target: EventTarget): Promise<FocusableInputAPI | null> {
        const editor = (await codeMirror.editor) as any;

        if (target === editor.display.input.textarea) {
            return api;
        }

        return null;
    }

    export const api: PlainTextInputAPI = {
        name: "plain-text",
        focus,
        focusable: !hidden,
        moveCaretToEnd,
        setStoredContentWithUndo,
        syncFromStoredContent,
        refocus,
        toggle,
        getInputAPI,
        codeMirror,
    };

    /**
     * Communicate to editing area that input is not focusable
     */
    function pushUpdate(isFocusable: boolean): void {
        api.focusable = isFocusable;
        $editingInputs = $editingInputs;
    }

    let inputWasVisible = inputIsVisible();
    $: {
        const visible = inputIsVisible();
        if (!inputWasVisible && visible) {
            syncCodeFromStoredContent();
        }
        inputWasVisible = visible;
    }

    async function refresh(): Promise<void> {
        const editor = await codeMirror.editor;
        editor.refresh();
    }

    $: {
        pushUpdate(!(hidden || fieldCollapsed));
        tick().then(() => {
            refresh();
            if (focusFlag.checkAndReset()) {
                refocus();
            }
        });
    }

    function onChange({ detail: html }: CustomEvent<string>): void {
        code.set(removeProhibitedTags(html));
    }

    onMount(() => {
        $editingInputs.push(api);
        $editingInputs = $editingInputs;

        let destroyed = false;
        let cleanupSelection = (): void => {};

        codeMirror.editor.then((editor) => {
            if (destroyed) {
                return;
            }

            const updateSelectionContext = (): void => {
                onAgentSelectedTextContext(
                    plainTextAgentSelectionContext(
                        editor.getSelection(),
                        fieldName,
                        fieldIndex,
                    ),
                );
            };

            editor.on("cursorActivity", updateSelectionContext);
            cleanupSelection = () =>
                editor.off("cursorActivity", updateSelectionContext);
        });

        let codeSubscriptionStarted = false;
        const cleanupStores = singleCallback(
            content.subscribe((html: string): void => {
                if (settingContentFromCode) {
                    return;
                }

                /* We call `removeProhibitedTags` here, because content might
                 * have been changed outside the editor, and we need to parse
                 * it to get the "neutral" value. Otherwise, there might be
                 * conflicts with other editing inputs */
                latestStoredContent = html;
                if (inputIsVisible()) {
                    syncCodeFromStoredContent();
                }
            }),
            code.subscribe((html: string): void => {
                if (!codeSubscriptionStarted) {
                    codeSubscriptionStarted = true;
                    return;
                }

                if (settingCodeFromContent) {
                    return;
                }

                const storedHtml = undecoratedToStored(html);
                latestStoredContent = storedHtml;
                settingContentFromCode = true;
                try {
                    content.set(storedHtml);
                } finally {
                    settingContentFromCode = false;
                }
            }),
        );

        return (): void => {
            destroyed = true;
            cleanupStores();
            cleanupSelection();
        };
    });

    setupLifecycleHooks(api);
</script>

<div
    class="plain-text-input"
    class:light-theme={!$pageTheme.isDark}
    on:focusin={() => ($focusedInput = api)}
    {hidden}
>
    <CodeMirror
        {configuration}
        {code}
        {hidden}
        bind:api={codeMirror}
        on:change={onChange}
    />
</div>

<style lang="scss">
    .plain-text-input {
        height: 100%;

        :global(.CodeMirror) {
            height: 100%;
            background: var(--canvas-code);
            padding-inline: 4px;
        }

        :global(.CodeMirror-lines) {
            padding: 8px 0;
        }

        :global(.CodeMirror-gutters) {
            background: var(--canvas-code);
        }
    }
</style>
