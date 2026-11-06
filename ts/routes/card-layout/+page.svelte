<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { PageData } from "./$types";
    import CardLayoutPage from "./CardLayoutPage.svelte";

    export let data: PageData;
</script>

<CardLayoutPage
    notetype={data.notetype}
    ord={data.ord}
    clozeNumbers={data.clozeNumbers}
    hasEmptyField={data.hasEmptyField}
    night={data.night}
/>
