<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

Empty Cards, as qt/aqt/emptycards.py.
-->
<script lang="ts">
    import type { EmptyCardsReport } from "@generated/anki/card_rendering_pb";
    import { getEmptyCards, removeCards } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";

    import { errorMessage, quiet } from "$lib/shell/shell";

    import { cardsToDelete, parseReport, stillEmpty } from "./lib";

    export let report: EmptyCardsReport;

    const sections = parseReport(report.report);
    let keepNotes = true;
    let error = "";
    let done = "";
    let busy = false;

    async function remove(): Promise<void> {
        busy = true;
        try {
            // only cards still empty: the window isn't modal
            const fresh = await getEmptyCards({}, quiet);
            const cardIds = cardsToDelete(stillEmpty(report, fresh), keepNotes);
            await removeCards({ cardIds }, quiet);
            done = tr.emptyCardsDeletedCount({ cards: cardIds.length });
            bridgeCommand("emptyCardsDone");
        } catch (err) {
            error = errorMessage(err);
        } finally {
            busy = false;
        }
    }
</script>

<main>
    {#if done}
        <p id="done">{done}</p>
    {:else if !report.notes.length}
        <p id="none">{tr.emptyCardsNotFound()}</p>
    {:else}
        {#each sections as section}
            <div class="heading">{section.heading}</div>
            <ol>
                {#each section.lines as line}
                    <li class:allempty={line.allEmpty}>
                        <span class="nid">{line.nid}:</span>
                        {line.text}
                    </li>
                {/each}
            </ol>
        {/each}
    {/if}
    {#if error}
        <div class="error" role="alert" id="error">{error}</div>
    {/if}
</main>

{#if report.notes.length && !done}
    <div class="buttons">
        <label class="check">
            <input type="checkbox" id="keep-notes" bind:checked={keepNotes} />
            {tr.emptyCardsPreserveNotesCheckbox()}
        </label>
        <span class="spacer"></span>
        <button id="delete" class="primary" disabled={busy} on:click={remove}>
            {tr.emptyCardsDeleteButton()}
        </button>
    </div>
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

    .heading {
        font-weight: bold;
        margin-top: 0.6em;
    }

    .allempty {
        color: var(--accent-danger);
    }

    .nid {
        font-variant-numeric: tabular-nums;
    }

    .error {
        color: var(--accent-danger);
    }

    .buttons {
        display: flex;
        align-items: center;
        gap: 0.5em;
        padding: 0.6em 1em;
        border-top: 1px solid var(--border-subtle);
    }

    .check {
        display: flex;
        align-items: center;
        gap: 0.4em;
    }

    .spacer {
        flex: 1;
    }
</style>
