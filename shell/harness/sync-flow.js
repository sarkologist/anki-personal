// Sync through the real UI, against a local sync server (never AnkiWeb: the
// shell refuses any other server for a profile copy). Run with
// `--test shell/harness/sync-flow.js --test-page main`, after pointing the
// copy's settings at the server (`customSyncUrl`, no `syncKey`).
//
// - Logged out: the toolbar's Sync asks to log in (user "test", password
//   "pass"), then answers the full-sync prompt with its first button.
// - With `--auto-sync`: the sync on open runs by itself; the first button
//   answers any prompt (a fresh profile's full download).
// The test ends once the sync and media sync are over and no dialog is open.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (what, fn, ms = 300000) => {
    const end = Date.now() + ms;
    for (;;) {
        const v = await fn();
        if (v) { return v; }
        if (Date.now() > end) { throw new Error("timed out waiting for " + what); }
        await sleep(100);
    }
};
const state = async () => {
    const s = await bridge("syncState");
    return typeof s === "string" ? JSON.parse(s) : s;
};

if (testStage > 0) {
    // a full download reloads the deck browser
    log(`stage ${testStage}: ${location.pathname}`);
}
const dialogs = [];
const startedLoggedIn = (await state()).loggedIn;
if (!startedLoggedIn && testStage === 0) {
    const sync = await until(
        "the Sync button",
        () => [...document.querySelectorAll("button, a")].find((b) => b.textContent.trim().startsWith("Sync")),
    );
    sync.click();
}
// answer dialogs until the sync is over
let quiet = 0;
while (quiet < 10) {
    const open = await bridge("testDialogs");
    const s = await state();
    if (open.length) {
        quiet = 0;
        const [label, text] = open[0];
        dialogs.push(text.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").slice(0, 120));
        log("dialog: " + dialogs[dialogs.length - 1]);
        const fields = /password/i.test(text) || /account/i.test(text) ? { user: "test", pass: "pass" } : {};
        await bridge("testDialog:" + JSON.stringify({ button: 0, fields }));
    } else if (s.state === "syncing") {
        quiet = 0;
    } else {
        quiet++;
    }
    await sleep(300);
}
const s = await state();
const cards = (await bridge("testQuery:select count() from cards"))[0][0];
log(`done: ${JSON.stringify(s)}, ${cards} cards`);
if (!s.loggedIn) {
    return { ok: false, error: "not logged in", dialogs };
}
return { ok: true, dialogs, state: s, cards };
