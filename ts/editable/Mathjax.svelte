<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import { onDestroy, tick } from "svelte";

    import { convertMathjax, unescapeSomeEntities } from "./mathjax";
    import { CooldownTimer } from "./cooldown-timer";
    import { getCachedMathjaxConversion } from "./mathjax-cache";
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
        [converted, title] = getCachedMathjaxConversion(
            mathjax,
            fontSize,
            mathjaxConfig.templateScriptVersion,
            () => convertMathjax(unescapeSomeEntities(mathjax), fontSize),
        );
    });
    $: empty = title === "MathJax";

    let host: HTMLElement;
    let shadow: ShadowRoot | null = null;

    let observer: ResizeObserver | null = null;

    // The component-internal style targets the host (so layout sits in the
    // light DOM) and the rendered SVG. Sizing for the empty placeholder is
    // driven by --font-size, which crosses the shadow boundary as a normal
    // custom property.
    const INTERNAL_CSS = [
        // - `overflow: hidden` shifts the inline-block's baseline to its
        //   bottom margin edge (per CSS spec). Without it, the host's baseline
        //   is its line baseline (somewhere in the middle), and mirroring
        //   MathJax's SVG-level `vertical-align: -i ex` onto the host wouldn't
        //   land the math baseline on the parent text baseline.
        // - `line-height: 0` collapses the strut so the host's box hugs the
        //   SVG. Otherwise short math (no descender, small SVG) leaves the
        //   font's strut descent below the SVG, so the host bottom sits
        //   *below* the SVG bottom and the math floats above the text line.
        ":host { display: inline-block; overflow: hidden; max-width: none !important; line-height: 0; vertical-align: var(--vertical-center, baseline); }",
        "svg { max-width: none !important; overflow: visible; }",
        // `width: fit-content` is needed so `margin: auto` actually centers:
        // the host is a <span>, not a replaced element, so without it
        // `display: block` would fill the parent width and the auto margins
        // would collapse to zero, leaving the math flush-left.
        ":host(.block) { display: block; overflow: visible; width: fit-content; max-width: none !important; margin: 1rem auto; transform: scale(1.1); }",
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
        const fragment = document.createRange().createContextualFragment(svgMarkup);
        shadow.appendChild(fragment);

        observer?.disconnect();
        const svg = shadow.querySelector("svg") as SVGSVGElement | null;
        if (svg) {
            // Mirror MathJax's own SVG `vertical-align: -X.XXXex` onto the
            // host so the math's typographic baseline ends up aligned with the
            // parent text baseline — the same alignment MathJax produces in
            // the reviewer/preview where the SVG isn't wrapped. Combined with
            // `overflow: hidden` on the host (baseline = bottom margin edge),
            // a host vertical-align of `-i ex` puts the host bottom at `i ex`
            // below the parent baseline, which is exactly where the SVG
            // bottom — and thus the math baseline `i ex` above it — line up.
            const va = svg.style.verticalAlign;
            if (va) {
                host.style.setProperty("--vertical-center", va);
            } else {
                host.style.removeProperty("--vertical-center");
            }
            // Forward the resize on the host so external listeners (e.g. the
            // mathjax overlay's error tooltip) keep working.
            observer = new ResizeObserver(() => {
                setTimeout(() => host.dispatchEvent(new Event("resize")));
            });
            observer.observe(host);
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
