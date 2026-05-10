<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script context="module" lang="ts">
    import { LRUCache } from "lru-cache";

    type Cache = LRUCache<string, [string, string]>;

    const caches: { [key: string]: Cache } = {};

    function getCache(...keyParts: any) {
        const key = keyParts.toString(); // primitive parts or arrays only
        if (!(key in caches)) {
            caches[key] = new LRUCache({ max: 10 });
        }
        return caches[key];
    }
</script>

<script lang="ts">
    import { onDestroy, tick } from "svelte";
    import { writable } from "svelte/store";

    import { convertMathjax, unescapeSomeEntities } from "./mathjax";
    import { CooldownTimer } from "./cooldown-timer";
    import { mathjaxConfig } from "./mathjax-element.svelte";

    export let mathjax: string;
    export let block: boolean;
    export let fontSize: number;

    let converted: string = "";
    let title: string = "";

    const debouncer = new CooldownTimer(500);

    $: debouncer.schedule(() => {
        // Color is now handled via `currentColor` and inheritance, so the cache
        // key only depends on the rendered geometry / template version.
        const cache = getCache(fontSize, mathjaxConfig.templateScriptVersion);
        const entry = cache.get(mathjax);
        if (entry) {
            [converted, title] = entry;
        } else {
            const entry = convertMathjax(unescapeSomeEntities(mathjax), fontSize);
            [converted, title] = entry;
            cache.set(mathjax, entry);
        }
    });
    $: empty = title === "MathJax";

    let host: HTMLElement;
    let shadow: ShadowRoot | null = null;
    const imageHeight = writable(0);

    $: verticalCenter = -$imageHeight / 2 + fontSize / 4;

    let observer: ResizeObserver | null = null;

    // The component-internal style targets the host (so layout sits in the
    // light DOM) and the rendered SVG. Sizing for the empty placeholder is
    // driven by --font-size, which crosses the shadow boundary as a normal
    // custom property.
    const INTERNAL_CSS = [
        ":host { display: inline-block; vertical-align: var(--vertical-center, baseline); }",
        ":host(.block) { display: block; margin: 1rem auto; transform: scale(1.1); }",
        ":host(.empty) { vertical-align: text-bottom; }",
        ":host(.empty) svg { width: var(--font-size); height: var(--font-size); }",
    ].join("\n");

    function makeStyle(css: string): HTMLStyleElement {
        const el = document.createElement("style");
        el.textContent = css;
        return el;
    }

    function syncShadow(svgMarkup: string, notetypeCss: string): void {
        if (!host) {
            return;
        }
        if (!shadow) {
            shadow = host.attachShadow({ mode: "open" });
        }
        // Replace previous content. We build the style elements via DOM so
        // the Svelte preprocessor doesn't pick the literal style start-tags
        // up as nested style blocks.
        while (shadow.firstChild) {
            shadow.removeChild(shadow.firstChild);
        }
        shadow.appendChild(makeStyle(INTERNAL_CSS));
        if (notetypeCss) {
            shadow.appendChild(makeStyle(notetypeCss));
        }
        const fragment = document
            .createRange()
            .createContextualFragment(svgMarkup);
        shadow.appendChild(fragment);

        observer?.disconnect();
        const svg = shadow.querySelector("svg");
        if (svg) {
            observer = new ResizeObserver((entries) => {
                for (const entry of entries) {
                    imageHeight.set(entry.contentRect.height);
                    // Forward the resize on the host so external listeners
                    // (e.g. the mathjax overlay's error tooltip) keep working.
                    setTimeout(() => host.dispatchEvent(new Event("resize")));
                }
            });
            observer.observe(svg);
        }
    }

    $: if (host) {
        syncShadow(converted, mathjaxConfig.notetypeCss);
    }

    export function moveCaretAfter(position?: [number, number]): void {
        // This should trigger a focusing of the Mathjax Handle. We pass `host`
        // through as the legacy `image` field so MathjaxOverlay's `detail.image`
        // shape is preserved.
        host.dispatchEvent(
            new CustomEvent("movecaretafter", {
                detail: { image: host, position },
                bubbles: true,
                composed: true,
            }),
        );
    }

    export function selectAll(): void {
        host.dispatchEvent(
            new CustomEvent("selectall", {
                detail: host,
                bubbles: true,
                composed: true,
            }),
        );
    }

    onDestroy(() => {
        observer?.disconnect();
        observer = null;
    });

    // Make sure the shadow gets rebuilt as soon as the host is bound, even
    // before any reactive `converted` change fires.
    $: if (host && !shadow) {
        tick().then(() => syncShadow(converted, mathjaxConfig.notetypeCss));
    }
</script>

<span
    bind:this={host}
    class:block
    class:empty
    class="mathjax"
    style:--vertical-center="{verticalCenter}px"
    style:--font-size="{fontSize}px"
    {title}
    data-anki="mathjax"
    role="img"
    aria-label="MathJax"
    on:dragstart|preventDefault
></span>

<style lang="scss">
    :global(anki-mathjax) {
        white-space: pre;
    }

    .mathjax {
        /* Layout & visibility belong to :host inside the shadow root; this is
         * just a paint hint so empty/block math gets the right outer box */
        display: inline-block;
    }

    .block {
        display: block;
    }
</style>
