// Shortcuts survive a reload of the deck list: run with
// `--test shell/harness/shortcuts-reload.js --test-page main`. A refresh that
// arrives before the page has mounted reloads it (`main_window::refresh`), as
// a sync or an add-on can right after launch. This script runs again on each
// page the main window loads; `testStage` counts them.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (what, fn, ms = 10000) => {
    const end = Date.now() + ms;
    for (;;) {
        const v = await fn();
        if (v) { return v; }
        if (Date.now() > end) { throw new Error("timed out waiting for " + what); }
        await sleep(50);
    }
};
const path = location.pathname;
log(`stage ${testStage}: ${path}`);

const hasPickerKey = () => window.__shellKeys?.includes("/");

if (path === "/deck-browser" && testStage === 0) {
    await until("the shortcut keys", hasPickerKey);
    location.reload();
    return { next: true };
}

if (path === "/deck-browser" && testStage === 1) {
    await until("the shortcut keys after the reload", hasPickerKey);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "/", code: "Slash", bubbles: true, cancelable: true }));
    const open = await until("the deck picker", async () => {
        const windows = await bridge("testWindows");
        return windows.some((w) => w.startsWith("dialog-")) && windows;
    });
    return { ok: true, open };
}

return { ok: false, error: `unexpected page ${path} at stage ${testStage}` };
