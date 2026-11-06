<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

Building a filtered deck, or changing one, as qt/aqt/filtered_deck.py.
-->
<script lang="ts">
    import type { FilteredDeckForUpdate } from "@generated/anki/decks_pb";
    import { addOrUpdateFilteredDeck } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";

    import { errorMessage, quiet } from "$lib/shell/shell";

    import { loadTerms, saveTerms } from "./lib";

    export let deck: FilteredDeckForUpdate;
    export let orderLabels: string[];

    const config = deck.config!;
    const existing = deck.id !== 0n;
    let name = deck.name;
    const loaded = loadTerms(deck);
    const first = loaded.first;
    const second = loaded.second;
    let secondEnabled = loaded.secondEnabled;
    let reschedule = config.reschedule;
    let previewAgain = config.previewAgainSecs;
    let previewHard = config.previewHardSecs;
    let previewGood = config.previewGoodSecs;
    let allowEmpty = deck.allowEmpty;
    let error = "";
    let busy = false;

    async function build(): Promise<void> {
        if (busy) {
            return;
        }
        deck.name = name;
        deck.allowEmpty = allowEmpty;
        config.reschedule = reschedule;
        config.delays = [];
        config.searchTerms = saveTerms(first, second, secondEnabled);
        config.previewAgainSecs = Math.max(0, Math.round(previewAgain || 0));
        config.previewHardSecs = Math.max(0, Math.round(previewHard || 0));
        config.previewGoodSecs = Math.max(0, Math.round(previewGood || 0));
        busy = true;
        try {
            const out = await addOrUpdateFilteredDeck(deck, quiet);
            bridgeCommand(`filteredDeckBuilt:${out.id}`);
        } catch (err) {
            // most likely nothing matched; let the search be changed
            error = errorMessage(err);
        } finally {
            busy = false;
        }
    }

    function focus(input: HTMLInputElement): void {
        input.focus();
        input.select();
    }

    function onKeydown(event: KeyboardEvent): void {
        if (event.key === "Escape") {
            bridgeCommand("screenClose");
        }
    }
</script>

<svelte:window on:keydown={onKeydown} />

<form on:submit|preventDefault={build}>
    <label class="row">
        <span>{tr.actionsName()}</span>
        <input type="text" id="name" bind:value={name} placeholder={deck.name} />
    </label>

    <fieldset>
        <legend>{tr.decksFilter()}</legend>
        <label class="row">
            <span>{tr.actionsSearch()}</span>
            <input type="text" id="search" bind:value={first.search} use:focus />
        </label>
        <div class="row">
            <label>
                {tr.decksLimitTo()}
                <input type="number" id="limit" min="1" bind:value={first.limit} />
            </label>
            <label>
                {tr.decksCardsSelectedBy()}
                <select id="order" bind:value={first.order}>
                    {#each orderLabels as label, index}
                        <option value={index}>{label}</option>
                    {/each}
                </select>
            </label>
        </div>
    </fieldset>

    <label class="check">
        <input type="checkbox" id="second" bind:checked={secondEnabled} />
        {tr.decksEnableSecondFilter()}
    </label>
    {#if secondEnabled}
        <fieldset>
            <legend>{tr.decksFilter2()}</legend>
            <label class="row">
                <span>{tr.actionsSearch()}</span>
                <input type="text" id="search-2" bind:value={second.search} />
            </label>
            <div class="row">
                <label>
                    {tr.decksLimitTo()}
                    <input type="number" min="1" bind:value={second.limit} />
                </label>
                <label>
                    {tr.decksCardsSelectedBy()}
                    <select bind:value={second.order}>
                        {#each orderLabels as label, index}
                            <option value={index}>{label}</option>
                        {/each}
                    </select>
                </label>
            </div>
        </fieldset>
    {/if}

    <fieldset>
        <legend>{tr.actionsOptions()}</legend>
        <label class="check">
            <input type="checkbox" id="reschedule" bind:checked={reschedule} />
            {tr.decksRescheduleCardsBasedOnMyAnswers()}
        </label>
        {#if !reschedule}
            <div class="delays">
                <label class="row">
                    <span>
                        {tr.decksDelayForButton({ button: tr.studyingAgain() })}
                    </span>
                    <span>
                        <input type="number" min="0" bind:value={previewAgain} />
                        {tr.schedulingSeconds()}
                    </span>
                </label>
                <label class="row">
                    <span>{tr.decksDelayForButton({ button: tr.studyingHard() })}</span>
                    <span>
                        <input type="number" min="0" bind:value={previewHard} />
                        {tr.schedulingSeconds()}
                    </span>
                </label>
                <label class="row">
                    <span>{tr.decksDelayForButton({ button: tr.studyingGood() })}</span>
                    <span>
                        <input type="number" min="0" bind:value={previewGood} />
                        {tr.schedulingSeconds()}
                    </span>
                </label>
                <p class="hint">{tr.decksZeroMinutesHint()}</p>
            </div>
        {/if}
        <label class="check">
            <input type="checkbox" bind:checked={allowEmpty} />
            {tr.decksCreateEvenIfEmpty()}
        </label>
    </fieldset>

    {#if error}
        <div class="error" role="alert" id="error">{error}</div>
    {/if}

    <div class="buttons">
        <button type="button" on:click={() => bridgeCommand("screenClose")}>
            {tr.actionsCancel()}
        </button>
        <button type="submit" class="primary" id="build" disabled={busy}>
            {existing ? tr.actionsRebuild() : tr.decksBuild()}
        </button>
    </div>
</form>

<style lang="scss">
    :global(body) {
        margin: 0;
        color: var(--fg);
        background: var(--canvas);
        font-size: 13px;
    }

    form {
        padding: 1em 1.4em;
    }

    fieldset {
        border: 1px solid var(--border-subtle);
        border-radius: var(--border-radius);
        margin: 0.8em 0;
        padding: 0.4em 0.8em 0.6em;
    }

    .row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 0.8em;
        margin: 0.4em 0;
    }

    .row > span:first-child {
        flex: none;
    }

    input[type="text"] {
        flex: 1;
    }

    input[type="number"] {
        width: 5em;
    }

    input,
    select {
        color: var(--fg);
        background: var(--canvas-elevated);
        border: 1px solid var(--border);
        border-radius: var(--border-radius);
        padding: 2px 4px;
    }

    .check {
        display: flex;
        align-items: center;
        gap: 0.5em;
        margin: 0.4em 0;
    }

    .hint {
        color: var(--fg-subtle);
        margin: 0.2em 0;
    }

    .error {
        color: var(--accent-danger);
        margin: 0.6em 0;
    }

    .buttons {
        display: flex;
        justify-content: flex-end;
        gap: 0.5em;
        margin-top: 1em;
    }
</style>
