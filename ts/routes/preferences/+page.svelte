<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
-->
<script lang="ts">
    import type { PageData } from "./$types";
    import PreferencesPage from "./PreferencesPage.svelte";

    export let data: PageData;
</script>

<PreferencesPage prefs={data.prefs} shell={data.shell} />
