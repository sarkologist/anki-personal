<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The current deck's overview, as qt/aqt/overview.py drew it, or the congrats
screen once nothing is left to study.
-->
<script lang="ts">
    import { UnburyDeckRequest_Mode } from "@generated/anki/scheduler_pb";
    import {
        emptyFilteredDeck,
        rebuildFilteredDeck,
        unburyDeck,
    } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";
    import { onDestroy } from "svelte";

    import BottomBar from "$lib/shell/BottomBar.svelte";
    import Dialog from "$lib/shell/Dialog.svelte";
    import { errorMessage, provideShellGlobal, quiet } from "$lib/shell/shell";

    import CongratsPage from "../congrats/CongratsPage.svelte";
    import Toolbar from "../toolbar/Toolbar.svelte";
    import { loadOverview, type OverviewData } from "./data";
    import DescriptionFrame from "./DescriptionFrame.svelte";
    import { formatBuried } from "./lib";

    export let initial: OverviewData;

    let data = initial;
    let error = "";

    async function refresh(): Promise<void> {
        try {
            data = await loadOverview();
            error = "";
        } catch (err) {
            error = errorMessage(err);
        }
    }

    provideShellGlobal("shellRefresh", () => void refresh());

    // as the congrats page, check again for cards that became due
    const timer = setInterval(() => {
        if (data.finished) {
            refresh();
        }
    }, 60_000);
    onDestroy(() => clearInterval(timer));

    async function run(op: () => Promise<unknown>): Promise<void> {
        try {
            await op();
        } catch (err) {
            error = errorMessage(err);
            return;
        }
        await refresh();
    }

    // The congrats component shows the description with {@html} and links
    // to bridge commands; both are handled here instead.
    $: congrats = (() => {
        const info = data.congrats.clone();
        info.deckDescription = "";
        info.bridgeCommandsSupported = false;
        return info;
    })();

    $: haveBuried = data.congrats.haveSchedBuried || data.congrats.haveUserBuried;

    $: rows = [
        {
            title: tr.actionsNew(),
            klass: "new-count",
            count: data.counts.new,
            buried: data.buried.new,
        },
        {
            title: tr.schedulingLearning(),
            klass: "learn-count",
            count: data.counts.learn,
            buried: data.buried.learn,
        },
        {
            title: tr.studyingToReview(),
            klass: "review-count",
            count: data.counts.review,
            buried: data.buried.review,
        },
    ];

    const filteredDescription = [
        tr.studyingThisIsASpecialDeckFor(),
        tr.studyingCardsWillBeAutomaticallyReturnedTo(),
        tr.studyingDeletingThisDeckFromTheDeck(),
    ].join(" ");

    let askUnbury = false;

    function unbury(mode: UnburyDeckRequest_Mode): void {
        askUnbury = false;
        run(() => unburyDeck({ deckId: data.deckId, mode }, quiet));
    }

    function onUnbury(): void {
        if (data.congrats.haveSchedBuried && data.congrats.haveUserBuried) {
            askUnbury = true;
        } else {
            unbury(UnburyDeckRequest_Mode.ALL);
        }
    }

    function focus(button: HTMLButtonElement): void {
        button.focus();
    }
</script>

<Toolbar />

<div class="overview">
    {#if error}
        <div class="error">{error}</div>
    {/if}

    {#if data.finished}
        <div class="congrats">
            <CongratsPage info={congrats} refreshPeriodically={false} />
        </div>
        {#if data.description && !data.filtered}
            <div class="finished-description">
                <DescriptionFrame html={data.description} />
            </div>
        {/if}
    {:else}
        <h3>{data.name}</h3>
        {#if data.filtered}
            <div class="descfont dyn">{filteredDescription}</div>
        {:else if data.description}
            <DescriptionFrame html={data.description} />
        {:else}
            <p></p>
        {/if}

        <div class="study">
            <table class="counts">
                <tbody>
                    {#each rows as row}
                        <tr>
                            <td>{row.title}:</td>
                            <td>
                                <b>
                                    <span class={row.klass}>{row.count}</span>
                                    {#if row.buried}
                                        <span
                                            class="bury-count"
                                            title={tr.studyingCountsDiffer()}
                                        >
                                            {formatBuried(row.buried)}
                                        </span>
                                    {/if}
                                </b>
                            </td>
                        </tr>
                    {/each}
                </tbody>
            </table>
            <button class="study-now" on:click={() => bridgeCommand("study")} use:focus>
                {tr.studyingStudyNow()}
            </button>
        </div>
    {/if}
</div>

<BottomBar>
    <button
        title={tr.actionsShortcutKey({ val: "O" })}
        on:click={() => bridgeCommand("opts")}
    >
        {tr.actionsOptions()}
    </button>
    {#if !data.filtered}
        <button
            title={tr.actionsShortcutKey({ val: "C" })}
            on:click={() => bridgeCommand("studymore")}
        >
            {tr.actionsCustomStudy()}
        </button>
    {/if}
    {#if data.filtered}
        <button
            title={tr.actionsShortcutKey({ val: "R" })}
            on:click={() => run(() => rebuildFilteredDeck({ did: data.deckId }, quiet))}
        >
            {tr.actionsRebuild()}
        </button>
        <button
            title={tr.actionsShortcutKey({ val: "E" })}
            on:click={() => run(() => emptyFilteredDeck({ did: data.deckId }, quiet))}
        >
            {tr.studyingEmpty()}
        </button>
    {/if}
    {#if haveBuried}
        <button title={tr.actionsShortcutKey({ val: "U" })} on:click={onUnbury}>
            {tr.studyingUnbury()}
        </button>
    {/if}
</BottomBar>

{#if askUnbury}
    <Dialog
        title={tr.studyingUnbury()}
        choices={[
            {
                label: tr.studyingManuallyBuriedCards(),
                action: () => unbury(UnburyDeckRequest_Mode.USER_ONLY),
            },
            {
                label: tr.studyingBuriedSiblings(),
                action: () => unbury(UnburyDeckRequest_Mode.SCHED_ONLY),
            },
            {
                label: tr.studyingAllBuriedCards(),
                action: () => unbury(UnburyDeckRequest_Mode.ALL),
            },
        ]}
        onCancel={() => (askUnbury = false)}
    >
        {tr.studyingWhatWouldYouLikeToUnbury()}
    </Dialog>
{/if}

<style lang="scss">
    @use "$lib/sass/button-mixins" as button;

    :global(body) {
        margin: 0;
        color: var(--fg);
        background: var(--canvas);
    }

    .overview {
        margin: 2em;
        text-align: center;
    }

    h3 {
        margin-bottom: 0;
    }

    .descfont {
        padding: 1em;
        color: var(--fg-subtle);
    }

    .dyn {
        width: 70%;
        margin: 0 auto;
        white-space: pre-wrap;
    }

    .study {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 2em;
        width: 400px;
        max-width: 100%;
        margin: 0 auto;
        padding: 5px;
    }

    .counts {
        border-collapse: separate;
        border-spacing: 5px;
        text-align: start;

        td {
            padding: 5px;
        }
    }

    .study-now {
        @include button.base($primary: true, $with-disabled: false);
        font-weight: 500;
        padding: 8px 10px;
        border-radius: var(--border-radius-large);
    }

    .congrats {
        display: flex;
        justify-content: center;
        text-align: start;
    }

    .finished-description {
        max-width: 30em;
        margin: 1em auto 0;
        border: 1px solid var(--border);
        text-align: start;

        :global(iframe) {
            width: 100%;
        }
    }

    .new-count {
        color: var(--state-new);
    }

    .learn-count {
        color: var(--state-learn);
    }

    .review-count {
        color: var(--state-review);
    }

    .bury-count {
        color: var(--fg-disabled);
        font-weight: bold;
        margin-inline-start: 2px;
    }

    .error {
        margin: 0 auto 1em;
        max-width: 40em;
        padding: 0.5em 1em;
        border: 1px solid var(--accent-danger);
        border-radius: var(--border-radius);
        color: var(--accent-danger);
    }
</style>
