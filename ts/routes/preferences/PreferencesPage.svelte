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
    import { bridgeCommand } from "@tslib/bridgecommand";
    import { onDestroy } from "svelte";

    import { errorMessage, quiet } from "$lib/shell/shell";

    import {
        minsToSecs,
        saveShellPreferences,
        secsToMins,
        shellCommand,
        type ShellPreferences,
        type Theme,
        toUint,
    } from "./lib";

    export let prefs: Preferences;
    export let shell: ShellPreferences;

    const scheduling = prefs.scheduling!;
    const reviewing = prefs.reviewing!;
    const editing = prefs.editing!;
    const backups = prefs.backups!;

    let error = "";
    let saved = false;

    // number boxes, clamped as Qt's spin boxes when saved
    let rollover = scheduling.rollover;
    let learnAheadMins = secsToMins(scheduling.learnAheadSecs);
    let timeboxMins = secsToMins(reviewing.timeLimitSecs);
    let daily = backups.daily;
    let weekly = backups.weekly;
    let monthly = backups.monthly;
    let minInterval = backups.minimumIntervalMins;
    let showPlayButtons = !reviewing.hideAudioPlayButtons;

    function apply(): void {
        scheduling.rollover = toUint(rollover, 0, 23, scheduling.rollover);
        scheduling.learnAheadSecs = minsToSecs(learnAheadMins);
        reviewing.timeLimitSecs = minsToSecs(timeboxMins);
        reviewing.hideAudioPlayButtons = !showPlayButtons;
        backups.daily = toUint(daily, 0, 9999, backups.daily);
        backups.weekly = toUint(weekly, 0, 9999, backups.weekly);
        backups.monthly = toUint(monthly, 0, 9999, backups.monthly);
        backups.minimumIntervalMins = toUint(
            minInterval,
            5,
            9999,
            backups.minimumIntervalMins,
        );
    }

    // Saved shortly after typing stops, one save at a time (each is a
    // request of its own, which could otherwise land out of order).
    let timer: ReturnType<typeof setTimeout> | undefined;
    let saving: Promise<void> = Promise.resolve();
    function changed(): void {
        saved = false;
        clearTimeout(timer);
        timer = setTimeout(queueSave, 300);
    }

    function queueSave(): Promise<void> {
        timer = undefined;
        apply();
        const snapshot = prefs.clone();
        saving = saving.then(() => save(snapshot));
        return saving;
    }

    async function save(snapshot: Preferences): Promise<void> {
        try {
            await setPreferences(snapshot, quiet);
            error = "";
            saved = true;
            bridgeCommand("preferencesChanged");
        } catch (err) {
            error = errorMessage(err);
        }
    }

    let theme: Theme = shell.theme;
    async function themeChanged(): Promise<void> {
        error = (await saveShellPreferences({ theme })) ?? "";
    }

    let latexPng = shell.latex.png;
    let latexSvg = shell.latex.svg;
    let latexError = "";
    let latexTimer: ReturnType<typeof setTimeout> | undefined;
    let latexSaving: Promise<void> = Promise.resolve();
    function latexChanged(): void {
        clearTimeout(latexTimer);
        latexTimer = setTimeout(queueLatexSave, 500);
    }
    function queueLatexSave(): Promise<void> {
        latexTimer = undefined;
        const latex = { png: latexPng, svg: latexSvg };
        latexSaving = latexSaving.then(async () => {
            latexError = (await saveShellPreferences({ latex })) ?? "";
        });
        return latexSaving;
    }
    async function latexReset(): Promise<void> {
        clearTimeout(latexTimer);
        latexTimer = undefined;
        await latexSaving;
        latexError = (await saveShellPreferences({ resetLatex: true })) ?? "";
        if (!latexError) {
            // back to the old Anki's commands, or the defaults
            const fresh = await shellCommand<ShellPreferences>("shellPreferences");
            latexPng = fresh.latex.png;
            latexSvg = fresh.latex.svg;
        }
    }

    // the shell calls this before the window closes
    window.shellFlush = async () => {
        if (timer !== undefined) {
            clearTimeout(timer);
            void queueSave();
        }
        if (latexTimer !== undefined) {
            clearTimeout(latexTimer);
            void queueLatexSave();
        }
        await Promise.all([saving, latexSaving]);
    };
    onDestroy(() => delete window.shellFlush);

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
            <input type="checkbox" bind:checked={showPlayButtons} on:input={changed} />
            {tr.preferencesShowPlayButtonsOnCardsWith()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={reviewing.interruptAudioWhenAnswering}
                on:input={changed}
            />
            {tr.preferencesInterruptCurrentAudioWhenAnswering()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={reviewing.showRemainingDueCounts}
                on:input={changed}
            />
            {tr.preferencesShowRemainingCardCount()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                id="show-intervals"
                bind:checked={reviewing.showIntervalsOnButtons}
                on:input={changed}
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
                    on:input={changed}
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
                    bind:value={rollover}
                    on:input={changed}
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
                    on:input={changed}
                />
                {tr.preferencesMins()}
            </span>
        </label>
    </section>

    <section>
        <h2>{tr.preferencesEditing()}</h2>
        <label class="row">
            <span>{tr.preferencesDefaultDeck()}</span>
            <select bind:value={editing.addingDefaultsToCurrentDeck} on:input={changed}>
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
                on:input={changed}
            />
            {tr.preferencesPasteClipboardImagesAsPng()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={editing.pasteStripsFormatting}
                on:input={changed}
            />
            {tr.preferencesPasteWithoutShiftKeyStripsFormatting()}
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={editing.renderLatex}
                on:input={changed}
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
                on:input={changed}
            />
        </label>
        <label class="check">
            <input
                type="checkbox"
                bind:checked={editing.ignoreAccentsInSearch}
                on:input={changed}
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
                on:input={latexChanged}
            ></textarea>
        </label>
        <label class="block">
            <span>{tr.preferencesLatexSvgCommands()}</span>
            <textarea
                id="latex-svg"
                rows="4"
                spellcheck="false"
                bind:value={latexSvg}
                on:input={latexChanged}
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
            <input type="number" min="0" bind:value={daily} on:input={changed} />
        </label>
        <label class="row">
            <span>{tr.preferencesWeeklyBackups()}</span>
            <input type="number" min="0" bind:value={weekly} on:input={changed} />
        </label>
        <label class="row">
            <span>{tr.preferencesMonthlyBackups()}</span>
            <input type="number" min="0" bind:value={monthly} on:input={changed} />
        </label>
        <label class="row">
            <span>{tr.preferencesMinutesBetweenBackups()}</span>
            <input type="number" min="5" bind:value={minInterval} on:input={changed} />
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
