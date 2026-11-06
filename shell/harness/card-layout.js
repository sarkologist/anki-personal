// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The card layout in WKWebView, on a note of a fresh copy of "Basic (and
// reversed card)": the preview (a sandboxed frame, which reports what it
// shows) following template edits, sides and night mode; the frame's lack
// of API access; renaming, adding and removing card types; the discard
// prompt; and saving, checked in the collection. Run with
// --test-page card-layout. With --test-page editor --test-note 0 it checks
// the editor's Cards button opens it.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(what, f, ms = 15000) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
        const v = await f();
        if (v) {
            return v;
        }
        await sleep(50);
    }
    throw new Error(`timed out waiting for ${what}`);
}

const errors = [];
window.addEventListener("error", (e) => errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => errors.push(String(e.reason)));

const checks = [];
function check(name, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    checks.push({ name, ok, actual, expected });
    log(`${ok ? "pass" : "FAIL"} ${name}: ${JSON.stringify(actual)}`);
}
const query = async (sql) => {
    const rows = await bridge(`testQuery:${sql}`);
    if (rows?.error) {
        throw new Error(rows.error);
    }
    return rows;
};
function set(selector, value) {
    const el = document.querySelector(selector);
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
}
const button = (text) => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
// the checks so far go with the script to the next window (testStash)
async function handOver(state, next, action) {
    await bridge(`testStash:${JSON.stringify({ ...state, checks, errors })}`);
    setTimeout(action, 300);
    return { next: true, in: next };
}
async function takeOver() {
    const state = await bridge("testStash:");
    checks.push(...(state?.checks ?? []));
    errors.push(...(state?.errors ?? []));
    return state ?? {};
}
const finish = () => ({
    ok: checks.every((c) => c.ok) && errors.length === 0,
    checks: checks.filter((c) => !c.ok),
    errors,
});

if (!location.pathname.startsWith("/card-layout")) {
    // the editor: Cards... saves the note, then opens the card layout
    pycmd("cards");
    await until("the card layout", async () => (await bridge("testWindows")).includes("card-layout"));
    check("Cards opens the card layout", true, true);
    return finish();
}

let shown = { shown: "", night: false };
window.addEventListener("message", (e) => {
    if (e.data && "shown" in e.data) {
        shown = e.data;
    }
});
const preview = (what, f) => until(what, () => f(shown));
const template = () => document.querySelector("#template");
function type(text) {
    template().value = text;
    template().dispatchEvent(new Event("input", { bubbles: true }));
}
async function answer(confirm = true) {
    await until("the dialog", () => document.querySelector(".dialog"));
    const input = document.querySelector("#ask-input");
    return { input, done: () => document.querySelector(confirm ? ".dialog button.primary" : ".dialog button").click() };
}

await until("the page", () => template());
check("two card types", document.querySelectorAll("#card option").length, 2);
check("the front template", template().value, "{{Front}}");
await preview("the front", (s) => s.shown.trim() === "shell front");

// edits show in the preview
type("Edited: {{Front}}");
await preview("the edit", (s) => s.shown.trim() === "Edited: shell front");
document.querySelector("#tab-back").click();
await preview("the back", (s) => s.shown.includes("shell back") && s.shown.includes("Edited: shell front"));
document.querySelector("#tab-css").click();
await until("the styling", () => template().value.includes(".card"));
check("the styling", template().value.includes(".card"), true);

// night mode in the preview
const night = [...document.querySelectorAll(".preview-bar input[type=checkbox]")].at(-1);
const wasNight = shown.night;
night.click();
await preview("night mode", (s) => s.night === !wasNight);

// the frame has an opaque origin: card JS there can't read this page
const frame = document.querySelector("#preview");
let blocked = false;
try {
    void frame.contentWindow.document.body;
} catch {
    blocked = true;
}
check("the preview can't be reached into", blocked, true);

// card types: rename the second, add a third, remove it again
set("#card", "1");
await sleep(50);
document.querySelector("#rename-card-type").click();
let dialog = await answer();
dialog.input.value = "Shell Reverse";
dialog.input.dispatchEvent(new Event("input", { bubbles: true }));
dialog.done();
await until(
    "the rename",
    () => document.querySelector("#card option:nth-child(2)")?.textContent === "2: Shell Reverse",
);
document.querySelector("#add-card-type").click();
(await answer()).done();
await until("a third card type", () => document.querySelectorAll("#card option").length === 3);
document.querySelector("#remove-card-type").click();
(await answer()).done();
await until("two again", () => document.querySelectorAll("#card option").length === 2);

// Cancel with changes asks first; keep them
button("Cancel").click();
(await answer(false)).done();
check("still open after keeping the changes", (await bridge("testWindows")).includes("card-layout"), true);

// save: checked in the collection before the shell closes the window
const real = window.bridgeCommand;
const saved = new Promise((resolve) => {
    window.bridgeCommand = window.pycmd = (cmd, cb) => (cmd === "clayoutSaved" ? resolve() : real(cmd, cb));
});
document.querySelector("#save").click();
await saved;
const [[qfmt]] = await query(
    "select cast(config as text) from templates where ntid = (select id from notetypes where name like 'shell layout %' order by id desc limit 1) and ord = 0",
);
check("the edited front is saved", qfmt.includes("Edited: {{Front}}"), true);
const names = await query(
    "select name from templates where ntid = (select id from notetypes where name like 'shell layout %' order by id desc limit 1) order by ord",
);
check("the renamed card type is saved", names.map((r) => r[0]), ["Card 1", "Shell Reverse"]);
return finish();
