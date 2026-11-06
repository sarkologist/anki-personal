// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The native shell's card browser: the table, sidebar and search bar, in a
// frame beside the editor (see shell/src/browser_window.rs).

import "./browser-base.scss";

import { mount } from "svelte";

import { call, type Init } from "./bridge";
import BrowserPage from "./BrowserPage.svelte";

async function setup(): Promise<void> {
    const init = await call<Init>("browserReady");
    mount(BrowserPage, {
        target: document.getElementById("browser") ?? document.body,
        props: { init },
    });
    window.parent.shellBrowserFlush?.();
}

setup();
