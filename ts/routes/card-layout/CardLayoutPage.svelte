<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The card layout screen, as qt/aqt/clayout.py: a note's card types, their
front, back and styling, and a preview of the chosen card. The preview frame
shows card templates, so it's sandboxed: the shell renders each side (see
shell/src/card_layout.rs) and this page posts it there.
-->
<script lang="ts">
    import type { Notetype, Notetype_Template } from "@generated/anki/notetypes_pb";
    import { getNotetype, updateNotetype } from "@generated/backend";
    import * as tr from "@generated/ftl";
    import { bridgeCommand } from "@tslib/bridgecommand";
    import { withoutUnicodeIsolation } from "@tslib/i18n";
    import { onDestroy, onMount } from "svelte";

    import Dialog from "$lib/shell/Dialog.svelte";
    import { errorMessage, quiet, shellCommand } from "$lib/shell/shell";

    import {
        clozeCards,
        flipped,
        isCloze,
        newCardName,
        newTemplate,
        reposition,
        toBase64,
    } from "./lib";

    export let notetype: Notetype;
    export let ord: number;
    export let clozeNumbers: number[];
    export let hasEmptyField: boolean;
    export let night: boolean;

    const cloze = isCloze(notetype);
    const loadedMtime = notetype.mtimeSecs;
    const config = notetype.config!;

    interface Card {
        label: string;
        /** The card's ordinal (the cloze number - 1 for clozes). */
        ord: number;
        /** Its template. */
        index: number;
    }
    $: cards = cloze
        ? clozeCards(clozeNumbers, ord).map((n): Card => ({
              label: tr.cardTemplatesCloze({ val: n }),
              ord: n - 1,
              index: 0,
          }))
        : notetype.templates.map((t, i): Card => ({
              label: `${i + 1}: ${t.name}`,
              ord: i,
              index: i,
          }));
    let selected = cloze
        ? Math.max(0, clozeCards(clozeNumbers, ord).indexOf(ord + 1))
        : Math.min(ord, notetype.templates.length - 1);
    $: card = cards[Math.min(selected, cards.length - 1)];
    $: template = notetype.templates[card.index];

    type Tab = "front" | "back" | "css";
    let tab: Tab = "front";
    let back = false;
    let fillEmpty = false;
    let previewNight = night;
    let dirty = false;
    let error = "";
    let renderError = "";
    let busy = false;
    /** Bumped when a change other than typing alters the text shown, so
    the textarea is drawn again (it keeps what's typed otherwise). */
    let version = 0;

    function text(tab: Tab, template: Notetype_Template): string {
        if (tab === "front") {
            return template.config!.qFormat;
        }
        if (tab === "back") {
            return template.config!.aFormat;
        }
        return config.css;
    }
    function edit(event: Event): void {
        const value = (event.currentTarget as HTMLTextAreaElement).value;
        const c = template.config!;
        if (tab === "front") {
            c.qFormat = value;
        } else if (tab === "back") {
            c.aFormat = value;
        } else {
            config.css = value;
        }
        dirty = true;
        schedulePreview();
    }
    function chooseTab(next: Tab): void {
        tab = next;
        // as Qt, the preview follows the side being edited
        if (next !== "css") {
            back = next === "back";
        }
        schedulePreview();
    }

    // Preview
    let frame: HTMLIFrameElement;
    let timer: ReturnType<typeof setTimeout> | undefined;
    function schedulePreview(): void {
        clearTimeout(timer);
        timer = setTimeout(renderPreview, 200);
    }
    async function renderPreview(): Promise<void> {
        const args = {
            template: toBase64(template.toBinary()),
            css: config.css,
            ord: card.ord,
            fillEmpty,
            answer: back,
            night: previewNight,
        };
        const out = await shellCommand<{
            html?: string;
            bodyClass?: string;
            error?: string;
        }>(`clayoutRender:${JSON.stringify(args)}`);
        if (out.error) {
            renderError = out.error;
            return;
        }
        renderError = "";
        frame?.contentWindow?.postMessage(
            { html: out.html, bodyClass: out.bodyClass, night: previewNight },
            "*",
        );
    }
    function onMessage(event: MessageEvent): void {
        if (event.source === frame?.contentWindow && event.data?.previewReady) {
            renderPreview();
        }
    }
    onMount(() => window.addEventListener("message", onMessage));
    onDestroy(() => {
        window.removeEventListener("message", onMessage);
        clearTimeout(timer);
    });
    $: (card, back, fillEmpty, previewNight, schedulePreview());

    // Card types
    interface Ask {
        title: string;
        message: string;
        input?: string;
        inputType?: "text" | "number";
        confirm: string;
        action: (input: string) => void;
    }
    let ask: Ask | null = null;
    let input = "";
    function open(next: Ask): void {
        input = next.input ?? "";
        ask = next;
    }
    function changed(): void {
        notetype = notetype;
        dirty = true;
        version++;
    }

    async function addCardType(): Promise<void> {
        const notes = await shellCommand<number | null>("clayoutNoteCount");
        open({
            title: tr.cardTemplatesAddCardType().replace("...", ""),
            message: `${tr.cardTemplatesThisWillCreateCardProceed({ count: notes ?? 0 })}\n\n${tr.qtMiscTheRequestedChangeWillRequireA()}`,
            confirm: tr.cardTemplatesAddCardType().replace("...", ""),
            action: () => {
                // without Fluent's isolation marks, which would be saved
                const name = newCardName(notetype.templates, (n) =>
                    withoutUnicodeIsolation(tr.cardTemplatesCard({ val: n })),
                );
                notetype.templates.push(newTemplate(name, template));
                selected = notetype.templates.length - 1;
                changed();
            },
        });
    }

    async function removeCardType(): Promise<void> {
        if (notetype.templates.length < 2) {
            error = tr.cardTemplatesAtLeastOneCardTypeIs();
            return;
        }
        const index = card.index;
        const name = template.name;
        const existing = template.ord?.val;
        const count =
            existing === undefined
                ? 0
                : ((await shellCommand<number | null>(
                      `clayoutCardCount:${existing}`,
                  )) ?? 0);
        open({
            title: tr.cardTemplatesRemoveCardType().replace("...", ""),
            message: `${tr.cardTemplatesDeleteTheAsCardTypeAnd({
                template: name,
                // Qt passes the counted phrase
                cards: tr.cardTemplatesCardCount({ count }) as unknown as number,
            })}\n\n${tr.qtMiscTheRequestedChangeWillRequireA()}`,
            confirm: tr.actionsDelete(),
            action: () => {
                notetype.templates.splice(index, 1);
                selected = 0;
                changed();
            },
        });
    }

    function renameCardType(): void {
        const index = card.index;
        open({
            title: tr.cardTemplatesRenameCardType().replace("...", ""),
            message: tr.actionsNewName(),
            input: template.name,
            inputType: "text",
            confirm: tr.actionsRename(),
            action: (name) => {
                const clean = name.replaceAll('"', "").trim();
                if (clean) {
                    notetype.templates[index].name = clean;
                    changed();
                }
            },
        });
    }

    function repositionCardType(): void {
        const index = card.index;
        open({
            title: tr.cardTemplatesRepositionCardType().replace("...", ""),
            message: `${tr.cardTemplatesEnterNewCardPosition1({
                val: notetype.templates.length,
            })}\n\n${tr.qtMiscTheRequestedChangeWillRequireA()}`,
            input: String(index + 1),
            inputType: "number",
            confirm: tr.actionsReposition(),
            action: (position) => {
                // an emptied box changes nothing, as in Qt
                const n = position === "" || position == null ? NaN : Number(position);
                if (Number.isFinite(n)) {
                    selected = reposition(notetype.templates, index, n);
                    changed();
                }
            },
        });
    }

    function flip(): void {
        const out = flipped(template.config!);
        if (!out) {
            error = tr.cardTemplatesAnkiCouldntFindTheLineBetween();
            return;
        }
        template.config!.qFormat = out.qFormat;
        template.config!.aFormat = out.aFormat;
        changed();
        schedulePreview();
    }

    // Saving and closing
    async function save(): Promise<void> {
        if (busy) {
            return;
        }
        busy = true;
        error = "";
        try {
            // Changed elsewhere meanwhile (a sticky pin, a sync)? Keep that,
            // with this window's templates and styling.
            const current = await getNotetype({ ntid: notetype.id }, quiet);
            let toSave = notetype;
            if (current.mtimeSecs !== loadedMtime) {
                toSave = current.clone();
                toSave.templates = notetype.templates;
                toSave.config!.css = config.css;
            }
            await updateNotetype(toSave, quiet);
            // still busy: the window closes, and a second save would make
            // new card types' cards again
            bridgeCommand("clayoutSaved");
        } catch (err) {
            error = errorMessage(err);
            busy = false;
        }
    }

    let askDiscard = false;
    function cancel(): void {
        if (dirty) {
            askDiscard = true;
        } else {
            bridgeCommand("clayoutClose");
        }
    }
    // the window's close button
    (window as unknown as { shellCloseRequested?: () => void }).shellCloseRequested =
        cancel;
    function stay(): void {
        askDiscard = false;
        bridgeCommand("clayoutStay");
    }

    function onKeydown(event: KeyboardEvent): void {
        if (ask || askDiscard) {
            return;
        }
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
            event.preventDefault();
            save();
        } else if (event.key === "Escape") {
            event.preventDefault();
            cancel();
        }
    }
</script>

<svelte:window on:keydown={onKeydown} />

<div class="layout">
    <div class="top">
        <label>
            {tr.cardTemplatesCardType()}
            <select id="card" bind:value={selected}>
                {#each cards as c, i}
                    <option value={i}>{c.label}</option>
                {/each}
            </select>
        </label>
        {#if !cloze}
            <button id="add-card-type" on:click={addCardType}>
                {tr.cardTemplatesAddCardType()}
            </button>
            <button id="remove-card-type" on:click={removeCardType}>
                {tr.cardTemplatesRemoveCardType()}
            </button>
            <button id="reposition-card-type" on:click={repositionCardType}>
                {tr.cardTemplatesRepositionCardType()}
            </button>
        {/if}
        <button id="rename-card-type" on:click={renameCardType}>
            {tr.cardTemplatesRenameCardType()}
        </button>
    </div>

    <div class="main">
        <section class="editor">
            <div class="tabs" role="tablist">
                <button
                    role="tab"
                    id="tab-front"
                    class:active={tab === "front"}
                    on:click={() => chooseTab("front")}
                >
                    {tr.cardTemplatesFrontTemplate()}
                </button>
                <button
                    role="tab"
                    id="tab-back"
                    class:active={tab === "back"}
                    on:click={() => chooseTab("back")}
                >
                    {tr.cardTemplatesBackTemplate()}
                </button>
                <button
                    role="tab"
                    id="tab-css"
                    class:active={tab === "css"}
                    on:click={() => chooseTab("css")}
                >
                    {tr.cardTemplatesTemplateStyling()}
                </button>
            </div>
            {#key `${card.index}:${tab}:${version}`}
            <textarea
                id="template"
                spellcheck="false"
                value={text(tab, template)}
                on:input={edit}
            ></textarea>
            {/key}
        </section>

        <section class="preview">
            <div class="preview-bar">
                <label class="check">
                    <input type="radio" name="side" value={false} bind:group={back} />
                    {tr.cardTemplatesFrontPreview()}
                </label>
                <label class="check">
                    <input type="radio" name="side" value={true} bind:group={back} />
                    {tr.cardTemplatesBackPreview()}
                </label>
                <span class="spacer"></span>
                {#if hasEmptyField}
                    <label class="check">
                        <input
                            type="checkbox"
                            id="fill-empty"
                            bind:checked={fillEmpty}
                        />
                        {tr.cardTemplatesFillEmpty()}
                    </label>
                {/if}
                <label class="check">
                    <input type="checkbox" bind:checked={previewNight} />
                    {tr.cardTemplatesNightMode()}
                </label>
            </div>
            {#if renderError}
                <div class="error" role="alert" id="render-error">{renderError}</div>
            {/if}
            <iframe
                bind:this={frame}
                id="preview"
                title={tr.cardTemplatesPreviewBox()}
                sandbox="allow-scripts"
                src="/_anki/legacyPageData?id=card-layout-preview"
            ></iframe>
        </section>
    </div>

    {#if error}
        <div class="error" role="alert" id="error">{error}</div>
    {/if}

    <div class="buttons">
        {#if !cloze}
            <button id="flip" on:click={flip}>{tr.cardTemplatesFlip()}</button>
        {/if}
        <span class="spacer"></span>
        <button on:click={cancel}>{tr.actionsCancel()}</button>
        <button id="save" class="primary" disabled={busy} on:click={save}>
            {tr.actionsSave()}
        </button>
    </div>
</div>

{#if ask}
    <Dialog
        title={ask.title}
        choices={[
            {
                label: ask.confirm,
                action: () => {
                    const a = ask;
                    ask = null;
                    a?.action(input);
                },
            },
        ]}
        onCancel={() => (ask = null)}
    >
        <p class="ask">{ask.message}</p>
        {#if ask.inputType}
            <!-- svelte-ignore a11y-autofocus -->
            <input id="ask-input" type={ask.inputType} bind:value={input} autofocus />
        {/if}
    </Dialog>
{/if}

{#if askDiscard}
    <Dialog
        title={tr.editingCards()}
        choices={[
            {
                label: tr.actionsDiscard(),
                action: () => bridgeCommand("clayoutClose"),
            },
        ]}
        onCancel={stay}
    >
        {tr.cardTemplatesDiscardChanges()}
    </Dialog>
{/if}

<style lang="scss">
    :global(body) {
        margin: 0;
        color: var(--fg);
        background: var(--canvas);
        font-size: 13px;
    }

    .layout {
        display: flex;
        flex-direction: column;
        height: 100vh;
        box-sizing: border-box;
        padding: 0.6em 0.8em;
        gap: 0.5em;
    }

    .top,
    .buttons,
    .preview-bar {
        display: flex;
        align-items: center;
        gap: 0.5em;
        flex-wrap: wrap;
    }

    .main {
        flex: 1;
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.8em;
        min-height: 0;
    }

    section {
        display: flex;
        flex-direction: column;
        min-height: 0;
    }

    .tabs {
        display: flex;
        gap: 0.3em;
        margin-bottom: 0.3em;

        .active {
            font-weight: bold;
            border-color: var(--border-focus);
        }
    }

    textarea {
        flex: 1;
        resize: none;
        font-family: ui-monospace, Menlo, monospace;
        font-size: 12px;
        color: var(--fg);
        background: var(--canvas-code, var(--canvas-elevated));
        border: 1px solid var(--border);
        border-radius: var(--border-radius);
        padding: 0.4em;
    }

    iframe {
        flex: 1;
        border: 1px solid var(--border);
        border-radius: var(--border-radius);
        background: var(--canvas);
    }

    select,
    input[type="text"],
    input[type="number"] {
        color: var(--fg);
        background: var(--canvas-elevated);
        border: 1px solid var(--border);
        border-radius: var(--border-radius);
        padding: 2px 4px;
    }

    .check {
        display: flex;
        align-items: center;
        gap: 0.3em;
    }

    .spacer {
        flex: 1;
    }

    .error {
        color: var(--accent-danger);
        white-space: pre-wrap;
    }

    .ask {
        white-space: pre-wrap;
    }
</style>
