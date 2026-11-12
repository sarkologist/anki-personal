<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

Custom study for the current deck, as qt/aqt/customstudy.py, with the tag
choice (qt/aqt/taglimit.py) shown below "Study by card state or tag" instead
of in a second dialog.
-->
<script lang="ts">
    import type { CustomStudyDefaultsResponse } from "@generated/anki/scheduler_pb";
    import { customStudy } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";

    import { errorMessage, quiet } from "$lib/shell/shell";

    import { buildRequest, type Choice, CRAM_KINDS, spinFor } from "./lib";

    export let deckId: bigint;
    export let defaults: CustomStudyDefaultsResponse;

    const choices: [Choice, string][] = [
        ["new", tr.customStudyIncreaseTodaysNewCardLimit()],
        ["review", tr.customStudyIncreaseTodaysReviewCardLimit()],
        ["forgot", tr.customStudyReviewForgottenCards()],
        ["ahead", tr.customStudyReviewAhead()],
        ["preview", tr.customStudyPreviewNewCards()],
        ["cram", tr.customStudyStudyByCardStateOrTag()],
    ];
    const cramLabels = [
        tr.customStudyNewCardsOnly(),
        tr.customStudyDueCardsOnly(),
        tr.customStudyAllReviewCardsInRandomOrder(),
        tr.customStudyAllCardsInRandomOrderDont(),
    ];

    let choice: Choice = "new";
    let spin = spinFor(choice, defaults);
    let value = spin.value;
    function choose(next: Choice): void {
        choice = next;
        spin = spinFor(next, defaults);
        value = spin.value;
    }

    let cramIndex = 0;
    const include = new Set(defaults.tags.filter((t) => t.include).map((t) => t.name));
    const exclude = new Set(defaults.tags.filter((t) => t.exclude).map((t) => t.name));
    function toggle(set: Set<string>, name: string, on: boolean): void {
        if (on) {
            set.add(name);
        } else {
            set.delete(name);
        }
    }

    function withChildren(count: number, children: number): string {
        return children
            ? `${count} ${tr.customStudyAvailableChildCount({ count: children })}`
            : String(count);
    }

    function titleFor(choice: Choice): string {
        if (choice === "new") {
            return tr.customStudyAvailableNewCards2({
                countString: withChildren(
                    defaults.availableNew,
                    defaults.availableNewInChildren,
                ),
            });
        }
        if (choice === "review") {
            return tr.customStudyAvailableReviewCards2({
                countString: withChildren(
                    defaults.availableReview,
                    defaults.availableReviewInChildren,
                ),
            });
        }
        return "";
    }

    function afterFor(choice: Choice, count: number): string {
        if (choice === "new" || choice === "review") {
            return tr.customStudyCards({ count });
        }
        if (choice === "cram") {
            return tr.customStudyCardsFromTheDeck({ count });
        }
        return tr.customStudyDays({ count });
    }

    $: title = titleFor(choice);
    $: before = {
        new: tr.customStudyIncreaseTodaysNewCardLimitBy(),
        review: tr.customStudyIncreaseTodaysReviewLimitBy(),
        forgot: tr.customStudyReviewCardsForgottenInLast(),
        ahead: tr.customStudyReviewAheadBy(),
        preview: tr.customStudyPreviewNewCardsAddedInThe(),
        cram: tr.customStudySelect(),
    }[choice];
    $: after = afterFor(choice, value);

    let error = "";
    let busy = false;
    async function ok(): Promise<void> {
        if (busy) {
            return;
        }
        const request = buildRequest(deckId, choice, value, {
            kind: CRAM_KINDS[cramIndex],
            include: [...include],
            exclude: [...exclude],
        });
        busy = true;
        try {
            await customStudy(request, quiet);
            // a limit change shows this deck; the others made the session
            // deck current. Still busy: the window closes.
            const limits = choice === "new" || choice === "review";
            bridgeCommand(limits ? `customStudyDone:${deckId}` : "customStudyDone");
        } catch (err) {
            // most likely nothing matched; keep the dialog open
            error = errorMessage(err);
            busy = false;
        }
    }

    function onKeydown(event: KeyboardEvent): void {
        if (event.key === "Escape") {
            event.preventDefault();
            bridgeCommand("screenClose");
        }
    }
</script>

<svelte:window on:keydown={onKeydown} />

<form on:submit|preventDefault={ok}>
    <div class="choices">
        {#each choices as [key, label]}
            <label class="check">
                <input
                    type="radio"
                    name="choice"
                    value={key}
                    checked={choice === key}
                    on:change={() => choose(key)}
                />
                {label}
            </label>
        {/each}
    </div>

    <div class="settings">
        {#if title}
            <p class="title" id="title">{title}</p>
        {/if}
        <label class="spin">
            {before}
            <input type="number" id="value" min={spin.min} max={spin.max} bind:value />
            {after}
        </label>

        {#if choice === "cram"}
            <div class="cram">
                {#each cramLabels as label, index}
                    <label class="check">
                        <input
                            type="radio"
                            name="cram"
                            value={index}
                            bind:group={cramIndex}
                        />
                        {label}
                    </label>
                {/each}
            </div>
            {#if defaults.tags.length}
                <div class="tags">
                    <div>
                        <p>{tr.customStudyRequireOneOrMoreOfThese()}</p>
                        <ul>
                            {#each defaults.tags as tag}
                                <li>
                                    <label class="check">
                                        <input
                                            type="checkbox"
                                            checked={include.has(tag.name)}
                                            on:change={(e) =>
                                                toggle(
                                                    include,
                                                    tag.name,
                                                    e.currentTarget.checked,
                                                )}
                                        />
                                        {tag.name}
                                    </label>
                                </li>
                            {/each}
                        </ul>
                    </div>
                    <div>
                        <p>{tr.customStudySelectTagsToExclude()}</p>
                        <ul>
                            {#each defaults.tags as tag}
                                <li>
                                    <label class="check">
                                        <input
                                            type="checkbox"
                                            checked={exclude.has(tag.name)}
                                            on:change={(e) =>
                                                toggle(
                                                    exclude,
                                                    tag.name,
                                                    e.currentTarget.checked,
                                                )}
                                        />
                                        {tag.name}
                                    </label>
                                </li>
                            {/each}
                        </ul>
                    </div>
                </div>
            {/if}
        {/if}
    </div>

    {#if error}
        <div class="error" role="alert" id="error">{error}</div>
    {/if}

    <div class="buttons">
        <button type="button" on:click={() => bridgeCommand("screenClose")}>
            {tr.actionsCancel()}
        </button>
        <button type="submit" class="primary" id="ok" disabled={busy}>
            {tr.customStudyOk()}
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

    .check {
        display: flex;
        align-items: center;
        gap: 0.5em;
        margin: 0.3em 0;
    }

    .settings {
        border-top: 1px solid var(--border-subtle);
        margin-top: 0.8em;
        padding-top: 0.6em;
    }

    .title {
        font-weight: bold;
        margin: 0.3em 0 0.6em;
    }

    .spin input {
        width: 6em;
        margin: 0 0.3em;
    }

    input[type="number"] {
        color: var(--fg);
        background: var(--canvas-elevated);
        border: 1px solid var(--border);
        border-radius: var(--border-radius);
        padding: 2px 4px;
    }

    .cram {
        margin: 0.6em 0 0.6em 1em;
    }

    .tags {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 1em;

        p {
            margin: 0.3em 0;
        }

        ul {
            list-style: none;
            margin: 0;
            padding: 0.3em;
            max-height: 12em;
            overflow-y: auto;
            border: 1px solid var(--border-subtle);
            border-radius: var(--border-radius);
        }
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
