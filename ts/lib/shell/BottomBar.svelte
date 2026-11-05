<!--
Copyright: Ankitects Pty Ltd and contributors
License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

The row of buttons that toolbar.py's BottomBar draws below the main view,
pinned to the bottom of the page.
-->
<div class="spacer"></div>
<div class="bottom-bar">
    <slot />
</div>

<style lang="scss">
    @use "$lib/sass/elevation" as *;

    .spacer {
        height: 4em;
    }

    .bottom-bar {
        position: fixed;
        left: 0;
        right: 0;
        bottom: 0;
        z-index: 10;
        padding: 9px;
        text-align: center;
        background: var(--canvas);

        :global(button) {
            font-weight: 500;
            padding: 8px 10px;
            margin: 0 4px;
            border-radius: var(--border-radius-large);
            @include elevation(1, $opacity-boost: -0.08);
            &:hover {
                @include elevation(2);
            }
        }
    }
</style>
