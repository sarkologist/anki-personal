<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

Check Media, as qt/aqt/mediacheck.py: the check runs when the window opens,
then its report and Qt's buttons. The report is shown again after each
action.
-->
<script lang="ts">
    import type { CheckMediaResponse } from "@generated/anki/media_pb";
    import {
        addNoteTags,
        checkMedia,
        emptyTrash,
        restoreTrash,
        trashMediaFiles,
    } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";
    import { onMount } from "svelte";

    import Dialog from "$lib/shell/Dialog.svelte";
    import { errorMessage, quiet, shellCommand } from "$lib/shell/shell";

    import { chunks, missingLatex } from "./lib";

    let result: CheckMediaResponse | null = null;
    let busy = true;
    let status = "";
    let error = "";
    let latexError: { nid: string; error: string } | null = null;
    let askDelete = false;
    /** Checks run so far (for tests: each action ends with one). */
    let checks = 0;

    async function check(): Promise<void> {
        busy = true;
        try {
            result = await checkMedia({}, quiet);
            error = "";
            checks++;
        } catch (err) {
            error = errorMessage(err);
        } finally {
            busy = false;
        }
    }
    onMount(check);

    async function run(action: () => Promise<string>): Promise<void> {
        busy = true;
        try {
            status = await action();
            error = "";
            bridgeCommand("mediaChanged");
        } catch (err) {
            error = errorMessage(err);
        }
        await check();
    }

    function deleteUnused(): void {
        askDelete = false;
        const unused = result?.unused ?? [];
        run(async () => {
            for (const fnames of chunks(unused, 25)) {
                await trashMediaFiles({ fnames }, quiet);
            }
            return tr.mediaCheckDeleteUnusedComplete({ count: unused.length });
        });
    }

    function tagMissing(): void {
        const noteIds = result?.missingMediaNotes ?? [];
        run(async () => {
            const out = await addNoteTags(
                { noteIds, tags: tr.mediaCheckMissingMediaTag() },
                quiet,
            );
            return tr.browsingNotesUpdated({ count: out.count });
        });
    }

    function renderLatex(): void {
        latexError = null;
        run(async () => {
            const out = await shellCommand<{
                done?: boolean;
                nid?: number;
                error?: string;
            }>("renderAllLatex");
            if (out.done) {
                return tr.mediaCheckAllLatexRendered();
            }
            if (out.nid !== undefined) {
                latexError = { nid: String(out.nid), error: out.error ?? "" };
                return "";
            }
            throw new Error(out.error ?? "");
        });
    }

    function onEmptyTrash(): void {
        run(async () => {
            await emptyTrash({}, quiet);
            return tr.mediaCheckTrashEmptied();
        });
    }

    function onRestoreTrash(): void {
        run(async () => {
            await restoreTrash({}, quiet);
            return tr.mediaCheckTrashRestored();
        });
    }
</script>

<main data-checks={checks}>
    {#if busy && !result}
        <p class="status">{tr.mediaCheckCheckMediaAction()}…</p>
    {/if}
    {#if error}
        <div class="error" role="alert" id="error">{error}</div>
    {/if}
    {#if result}
        <pre id="report">{result.report}</pre>
    {/if}
    {#if latexError}
        <div class="error" role="alert" id="latex-error">
            <p>{tr.cardStatsNoteId()}: {latexError.nid}</p>
            <pre>{latexError.error}</pre>
        </div>
    {/if}
    <p class="status" id="status">{status}</p>
</main>

<div class="buttons">
    <button on:click={() => bridgeCommand("openMediaFolder")}>{tr.addonsViewFiles()}</button>
    <span class="spacer"></span>
    {#if result?.unused.length}
        <button id="delete-unused" disabled={busy} on:click={() => (askDelete = true)}>
            {tr.mediaCheckDeleteUnused()}
        </button>
    {/if}
    {#if result?.missing.length}
        <button id="tag-missing" disabled={busy} on:click={tagMissing}>
            {tr.mediaCheckAddTag()}
        </button>
        {#if missingLatex(result.missing)}
            <button id="render-latex" disabled={busy} on:click={renderLatex}>
                {tr.mediaCheckRenderLatex()}
            </button>
        {/if}
    {/if}
    {#if result?.haveTrash}
        <button id="empty-trash" disabled={busy} on:click={onEmptyTrash}>
            {tr.mediaCheckEmptyTrash()}
        </button>
        <button id="restore-trash" disabled={busy} on:click={onRestoreTrash}>
            {tr.mediaCheckRestoreTrash()}
        </button>
    {/if}
</div>

{#if askDelete}
    <Dialog
        title={tr.mediaCheckDeleteUnused()}
        choices={[{ label: tr.mediaCheckDeleteUnused(), action: deleteUnused }]}
        onCancel={() => (askDelete = false)}
    >
        {tr.mediaCheckDeleteUnusedConfirm()}
    </Dialog>
{/if}

<style lang="scss">
    :global(body) {
        margin: 0;
        color: var(--fg);
        background: var(--canvas);
        font-size: 13px;
        display: flex;
        flex-direction: column;
        height: 100vh;
    }

    main {
        flex: 1;
        overflow: auto;
        padding: 0.8em 1em;
    }

    pre {
        white-space: pre;
        font-family: ui-monospace, Menlo, monospace;
        font-size: 12px;
        margin: 0;
    }

    .status {
        color: var(--fg-subtle);
    }

    .error {
        color: var(--accent-danger);
        margin: 0.6em 0;
    }

    .buttons {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5em;
        padding: 0.6em 1em;
        border-top: 1px solid var(--border-subtle);
    }

    .spacer {
        flex: 1;
    }
</style>
