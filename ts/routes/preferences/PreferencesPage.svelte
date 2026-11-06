<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The native shell's Preferences window: what qt/aqt/preferences.py offers that
the shell uses, plus its LaTeX commands (the "Edit LaTeX toolchain" add-on).
Changes are saved as they're made, as on macOS.
-->
<script lang="ts">
    import type { Preferences } from "@generated/anki/config_pb";
    import { setPreferences } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { onDestroy } from "svelte";

    import { errorMessage, quiet } from "$lib/shell/shell";

    import {
        minsToSecs,
        saveShellPreferences,
        secsToMins,
        shellCommand,
        type ShellPreferences,
        type Theme,
    } from "./lib";

    export let prefs: Preferences;
    export let shell: ShellPreferences;

    const scheduling = prefs.scheduling!;
    const reviewing = prefs.reviewing!;
    const editing = prefs.editing!;
    const backups = prefs.backups!;

    let error = "";
    let saved = false;

    // backend preferences: saved shortly after the last change
    let timer: ReturnType<typeof setTimeout> | undefined;
    function changed(): void {
        saved = false;
        clearTimeout(timer);
        timer = setTimeout(save, 300);
    }

    async function save(): Promise<void> {
        timer = undefined;
        try {
            await setPreferences(prefs, quiet);
            error = "";
            saved = true;
        } catch (err) {
            error = errorMessage(err);
        }
    }
    onDestroy(() => {
        if (timer !== undefined) {
            clearTimeout(timer);
            void save();
        }
    });

    let theme: Theme = shell.theme;
    async function themeChanged(): Promise<void> {
        error = (await saveShellPreferences({ theme })) ?? "";
    }

    let timeboxMins = secsToMins(reviewing.timeLimitSecs);
    let learnAheadMins = secsToMins(scheduling.learnAheadSecs);
    $: reviewing.timeLimitSecs = minsToSecs(timeboxMins);
    $: scheduling.learnAheadSecs = minsToSecs(learnAheadMins);

    let showPlayButtons = !reviewing.hideAudioPlayButtons;
    $: reviewing.hideAudioPlayButtons = !showPlayButtons;

    let latexPng = shell.latex.png;
    let latexSvg = shell.latex.svg;
    let latexError = "";
    async function latexChanged(): Promise<void> {
        latexError =
            (await saveShellPreferences({ latex: { png: latexPng, svg: latexSvg } })) ??
            "";
    }
    async function latexReset(): Promise<void> {
        latexError = (await saveShellPreferences({ resetLatex: true })) ?? "";
        if (!latexError) {
            // back to the old Anki's commands, or the defaults
            const fresh = await shellCommand<ShellPreferences>("shellPreferences");
            latexPng = fresh.latex.png;
            latexSvg = fresh.latex.svg;
        }
    }

    const themes: [Theme, string][] = [
        ["system", tr.preferencesThemeFollowSystem()],
        ["light", tr.preferencesThemeLight()],
        ["dark", tr.preferencesThemeDark()],
    ];
</script>

<main>
    {#if error}
        <div class="error" role="alert">{error}</div>
    {/if}

    <section>
        <h2>{tr.preferencesAppearance()}</h2>
        <label class="row">
            <span>{tr.preferencesTheme()}</span>
            <select bind:value={theme} on:change={themeChanged} id="theme">
                {#each themes as [value, label]}
                    <option {value}>{label}</option>
                {/each}
            </select>
        </label>
    </section>

    <section>
        <h2>{tr.preferencesReview()}</h2>
        <label class="check">
            <input type="checkbox" bind:checked={showPlayButtons} on:change={changed} />
            {tr.preferencesShowPlayButtonsOnCardsWith()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={reviewing.interruptAudioWhenAnswering}
                on:change={changed}
            />
            {tr.preferencesInterruptCurrentAudioWhenAnswering()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={reviewing.showRemainingDueCounts}
                on:change={changed}
            />
            {tr.preferencesShowRemainingCardCount()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                id="show-intervals"
                bind:checked={reviewing.showIntervalsOnButtons}
                on:change={changed}
            />
            {tr.preferencesShowNextReviewTimeAboveAnswer()}
        </label>
        <label class="row">
            <span>{tr.preferencesTimeboxTimeLimit()}</span>
            <span>
                <input
                    type="number"
                    min="0"
                    max="9999"
                    bind:value={timeboxMins}
                    on:change={changed}
                />
                {tr.preferencesMins()}
            </span>
        </label>
    </section>

    <section>
        <h2>{tr.preferencesScheduling()}</h2>
        <label class="row">
            <span>{tr.preferencesNextDayStartsAt()}</span>
            <span>
                <input
                    type="number"
                    min="0"
                    max="23"
                    id="rollover"
                    bind:value={scheduling.rollover}
                    on:change={changed}
                />
                {tr.preferencesHoursPastMidnight()}
            </span>
        </label>
        <label class="row">
            <span>{tr.preferencesLearnAheadLimit()}</span>
            <span>
                <input
                    type="number"
                    min="0"
                    max="9999"
                    bind:value={learnAheadMins}
                    on:change={changed}
                />
                {tr.preferencesMins()}
            </span>
        </label>
    </section>

    <section>
        <h2>{tr.preferencesEditing()}</h2>
        <label class="row">
            <span>{tr.preferencesDefaultDeck()}</span>
            <select
                bind:value={editing.addingDefaultsToCurrentDeck}
                on:change={changed}
            >
                <option value={true}>
                    {tr.preferencesWhenAddingDefaultToCurrentDeck()}
                </option>
                <option value={false}>
                    {tr.preferencesChangeDeckDependingOnNoteType()}
                </option>
            </select>
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={editing.pasteImagesAsPng}
                on:change={changed}
            />
            {tr.preferencesPasteClipboardImagesAsPng()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={editing.pasteStripsFormatting}
                on:change={changed}
            />
            {tr.preferencesPasteWithoutShiftKeyStripsFormatting()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={editing.renderLatex}
                on:change={changed}
            />
            {tr.preferencesGenerateLatexImagesAutomatically()}
        </label>
    </section>

    <section>
        <h2>{tr.preferencesBrowsing()}</h2>
        <label class="row">
            <span>{tr.preferencesDefaultSearchText()}</span>
            <input
                type="text"
                placeholder={tr.preferencesDefaultSearchTextExample()}
                bind:value={editing.defaultSearchText}
                on:change={changed}
            />
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={editing.ignoreAccentsInSearch}
                on:change={changed}
            />
            {tr.preferencesIgnoreAccentsInSearch()}
        </label>
    </section>

    <section>
        <h2>{tr.preferencesLatexCommands()}</h2>
        <p class="help">{tr.preferencesLatexCommandsHelp()}</p>
        <label class="block">
            <span>{tr.preferencesLatexPngCommands()}</span>
            <textarea
                id="latex-png"
                rows="4"
                spellcheck="false"
                bind:value={latexPng}
                on:change={latexChanged}
            ></textarea>
        </label>
        <label class="block">
            <span>{tr.preferencesLatexSvgCommands()}</span>
            <textarea
                id="latex-svg"
                rows="4"
                spellcheck="false"
                bind:value={latexSvg}
                on:change={latexChanged}
            ></textarea>
        </label>
        {#if latexError}
            <div class="error" role="alert" id="latex-error">{latexError}</div>
        {/if}
        <button on:click={latexReset}>{tr.preferencesLatexCommandsReset()}</button>
    </section>

    <section>
        <h2>{tr.preferencesBackups()}</h2>
        <p class="help">{tr.preferencesBackupExplanation()}</p>
        <label class="row">
            <span>{tr.preferencesDailyBackups()}</span>
            <input
                type="number"
                min="0"
                bind:value={backups.daily}
                on:change={changed}
            />
        </label>
        <label class="row">
            <span>{tr.preferencesWeeklyBackups()}</span>
            <input
                type="number"
                min="0"
                bind:value={backups.weekly}
                on:change={changed}
            />
        </label>
        <label class="row">
            <span>{tr.preferencesMonthlyBackups()}</span>
            <input
                type="number"
                min="0"
                bind:value={backups.monthly}
                on:change={changed}
            />
        </label>
        <label class="row">
            <span>{tr.preferencesMinutesBetweenBackups()}</span>
            <input
                type="number"
                min="5"
                bind:value={backups.minimumIntervalMins}
                on:change={changed}
            />
        </label>
    </section>

    <p class="status" id="status">{saved ? "✓" : ""}</p>
</main>

<style lang="scss">
    :global(body) {
        margin: 0;
        color: var(--fg);
        background: var(--canvas);
        font-size: 13px;
    }

    main {
        max-width: 40em;
        margin: 0 auto;
        padding: 1em 1.5em 2em;
    }

    section {
        border-bottom: 1px solid var(--border-subtle);
        padding-bottom: 0.8em;
    }

    h2 {
        font-size: 1.1em;
        margin: 1.2em 0 0.6em;
    }

    .row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 1em;
        margin: 0.4em 0;
    }

    .check {
        display: flex;
        align-items: center;
        gap: 0.5em;
        margin: 0.4em 0;
    }

    .block {
        display: block;
        margin: 0.6em 0;

        span {
            display: block;
            margin-bottom: 0.3em;
        }
    }

    input[type="number"] {
        width: 5em;
    }

    input[type="text"] {
        width: 16em;
    }

    textarea {
        width: 100%;
        box-sizing: border-box;
        font-family: ui-monospace, Menlo, monospace;
        font-size: 12px;
    }

    input,
    select,
    textarea {
        color: var(--fg);
        background: var(--canvas-elevated);
        border: 1px solid var(--border);
        border-radius: var(--border-radius);
        padding: 2px 4px;
    }

    .help {
        color: var(--fg-subtle);
        margin: 0.3em 0;
    }

    .error {
        color: var(--accent-danger);
        margin: 0.5em 0;
        white-space: pre-wrap;
    }

    .status {
        color: var(--fg-subtle);
        text-align: end;
    }
</style>
