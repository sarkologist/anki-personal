<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script context="module" lang="ts">
    import type CodeMirrorLib from "codemirror";

    export interface CodeMirrorAPI {
        readonly editor: Promise<CodeMirrorLib.Editor>;
        replaceValueWithUndo(value: string): Promise<void>;
        setOption<T extends keyof CodeMirrorLib.EditorConfiguration>(
            key: T,
            value: CodeMirrorLib.EditorConfiguration[T],
        ): Promise<void>;
    }
</script>

<script lang="ts">
    import { directionKey } from "@tslib/context-keys";
    import { promiseWithResolver } from "@tslib/promise";
    import { createEventDispatcher, getContext, onMount } from "svelte";
    import type { Writable } from "svelte/store";

    import { pageTheme } from "$lib/sveltelib/theme";

    import {
        darkTheme,
        lightTheme,
        openCodeMirror,
        setupCodeMirror,
    } from "./code-mirror";

    export let configuration: CodeMirrorLib.EditorConfiguration;
    export let code: Writable<string>;
    export let hidden = false;

    const defaultConfiguration = {
        rtlMoveVisually: true,
        lineNumbers: false,
    };

    const [editorPromise, resolve] = promiseWithResolver<CodeMirrorLib.Editor>();

    /**
     * Convenience function for editor.setOption.
     */
    async function setOption<T extends keyof CodeMirrorLib.EditorConfiguration>(
        key: T,
        value: CodeMirrorLib.EditorConfiguration[T],
    ): Promise<void> {
        const editor = await editorPromise;
        editor.setOption(key, value);
    }

    let storeAccess: ReturnType<typeof setupCodeMirror> | null = null;

    async function replaceValueWithUndo(value: string): Promise<void> {
        const editor = await editorPromise;
        if (editor.getValue() === value) {
            return;
        }
        const wasFocused = editor.hasFocus();
        const lastLine = editor.lastLine();
        const end = {
            line: lastLine,
            ch: editor.getLine(lastLine).length,
        };

        storeAccess?.unsubscribe();
        editor.operation(() => {
            editor.replaceRange(value, { line: 0, ch: 0 }, end, "agentProposal");
        });
        if (!wasFocused) {
            storeAccess?.subscribe();
        }
    }

    const direction = getContext<Writable<"ltr" | "rtl">>(directionKey);

    let apiPartial: Partial<CodeMirrorAPI>;
    export { apiPartial as api };

    Object.assign(apiPartial, {
        editor: editorPromise,
        replaceValueWithUndo,
        setOption,
    });

    const dispatch = createEventDispatcher();

    onMount(async () => {
        const editor = await editorPromise;
        storeAccess = setupCodeMirror(editor, code);
        editor.on("change", () => dispatch("change", editor.getValue()));
        editor.on("focus", (codeMirror, event) =>
            dispatch("focus", { codeMirror, event }),
        );
        editor.on("blur", (codeMirror, event) =>
            dispatch("blur", { codeMirror, event }),
        );
        editor.on("keydown", (codeMirror, event) => {
            if (event.code === "Tab") {
                dispatch("tab", { codeMirror, event });
            }
        });
    });
</script>

<div class="code-mirror">
    <textarea
        tabindex="-1"
        hidden
        use:openCodeMirror={{
            configuration: {
                ...configuration,
                ...defaultConfiguration,
                direction: $direction,
                theme: $pageTheme.isDark ? darkTheme : lightTheme,
            },
            resolve,
            hidden,
        }}
    ></textarea>
</div>

<style lang="scss">
    .code-mirror {
        height: 100%;

        :global(.CodeMirror) {
            height: auto;
            font-family: Consolas, monospace;
        }

        :global(.CodeMirror-wrap pre) {
            word-break: break-word;
        }
    }
</style>
