<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { PageData } from "./$types";
    import EmptyCardsPage from "./EmptyCardsPage.svelte";

    export let data: PageData;
</script>

<EmptyCardsPage report={data.report} />
