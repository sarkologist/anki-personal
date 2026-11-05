<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The deck description is user HTML and this page can call the backend, so it
is shown in a sandboxed iframe that can't run scripts. allow-same-origin
(without allow-scripts) only lets this page measure the content and handle
link clicks.
-->
<script lang="ts">
    import { bridgeCommand } from "@tslib/bridgecommand";
    import { onDestroy } from "svelte";

    import { descriptionDocument } from "./lib";

    export let html: string;

    function themeCss(): string {
        const root = getComputedStyle(document.documentElement);
        const body = getComputedStyle(document.body);
        const v = (name: string) => root.getPropertyValue(name).trim();
        return `:root { color-scheme: ${root.colorScheme || "normal"}; }
body { color: ${v("--fg-subtle")}; font-family: ${body.fontFamily}; font-size: ${body.fontSize}; }
a { color: ${v("--fg-link")}; text-decoration: none; }`;
    }

    $: srcdoc = descriptionDocument(html, themeCss());

    let frame: HTMLIFrameElement;
    let height = 0;
    let observer: ResizeObserver | undefined;

    function onClick(event: MouseEvent): void {
        // the frame's own realm, so no instanceof checks
        const target = event.target as Element | null;
        const link = target?.closest?.("a[href]") as HTMLAnchorElement | null;
        if (!link) {
            return;
        }
        event.preventDefault();
        if (/^(https?|mailto):/i.test(link.href)) {
            bridgeCommand(`openUrl:${link.href}`);
        }
    }

    function onLoad(): void {
        observer?.disconnect();
        const doc = frame.contentDocument;
        if (!doc?.body) {
            return;
        }
        const body = doc.body;
        // the body, not the document: that is at least as tall as the frame
        const measure = () => (height = Math.ceil(body.getBoundingClientRect().height));
        observer = new ResizeObserver(measure);
        observer.observe(body);
        measure();
        doc.addEventListener("click", onClick);
    }

    onDestroy(() => observer?.disconnect());
</script>

<iframe
    bind:this={frame}
    title="description"
    sandbox="allow-same-origin"
    {srcdoc}
    style:height="{height}px"
    on:load={onLoad}
></iframe>

<style>
    iframe {
        display: block;
        width: 70%;
        margin: 0 auto;
        border: 0;
        background: transparent;
    }
</style>
