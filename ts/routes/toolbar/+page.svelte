<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The top toolbar on its own, for an iframe above the reviewer. It runs without
backend API access, so it talks to the shell through the bridge only.
-->
<script lang="ts">
    import Toolbar from "./Toolbar.svelte";
</script>

<Toolbar flat />

<style>
    :global(html),
    :global(body) {
        height: auto;
        margin: 0;
        padding: 0;
        overflow: hidden;
        background: transparent;
    }
</style>
