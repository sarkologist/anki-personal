// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Escape closes windows other than the main one, as in Qt, unless the page
// used it (`bridge::ESCAPE_CLOSES_SCRIPT`). Run with --test-page main; it
// adds a note to the copy.
// Checks: the main window stays; Edit Current closes from a field and
// saves what was typed; Add with input asks first, and Escape in that
// prompt keeps editing; the browser keeps
// Escape that clears its sidebar filter, and closes from the search box
// (in the table's frame).

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (what, fn, ms = 20000) => {
    const end = Date.now() + ms;
    for (;;) {
        const v = await fn();
        if (v) {
            return v;
        }
        if (Date.now() > end) {
            throw new Error("timed out waiting for " + what);
        }
        await sleep(150);
    }
};
const checks = [];
function check(name, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    checks.push({ name, ok, actual, expected });
    log(`${ok ? "pass" : "FAIL"} ${name}: ${JSON.stringify(actual)}`);
}
const isOpen = async (label) => (await bridge("testWindows")).includes(label);
// `js` (an expression) in a window, and its JSON value
const inWindow = async (label, js) => {
    await bridge("testStash:null");
    await bridge(
        `testEval:${label}\nPromise.resolve((() => ${js})()).then((v) => pycmd("testStash:" + JSON.stringify(v ?? null)))`,
    );
    return until(`an answer from ${label}`, async () => {
        const v = await bridge("testStash");
        return v === null ? undefined : { v };
    }).then((r) => r.v);
};
// Escape on `target` (an expression) in a window, as a key press sends it
const escape = (label, target) =>
    bridge(
        `testEval:${label}\n(${target}).dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true, cancelable: true, composed: true }))`,
    );
const FIELD = `document.querySelector(".rich-text-editable")?.shadowRoot?.querySelector("anki-editable")`;
const type = (label, text) =>
    bridge(
        `testEval:${label}\n(() => { const f = ${FIELD}; f.focus(); document.getSelection().collapse(f, f.childNodes.length); document.execCommand("insertText", false, ${
            JSON.stringify(text)
        }); })()`,
    );
// stays open for a while after Escape
const staysOpen = async (label) => {
    await sleep(1000);
    return isOpen(label);
};

// the main window
await escape("main", "document.body");
check("main window stays", await staysOpen("main"), true);
// (it has no Escape script; the shell ignores "close" from it too)
await bridge("close");
check("main window ignores close", await staysOpen("main"), true);

// Edit Current: from a field, saving what was typed
const front = `shell escape ${Date.now()}`;
const nid = await bridge(`testAddNote:${JSON.stringify({ notetype: "Basic", fields: [front, "back"] })}`);
await bridge(`testOpenEditor:${nid}`);
await until("the editor", () => isOpen("editor"));
await until("the note in the editor", () => inWindow("editor", `${FIELD}?.textContent === ${JSON.stringify(front)}`));
await type("editor", " edited");
await escape("editor", FIELD);
await until("the editor to close", async () => !(await isOpen("editor")));
check("Edit Current saved on Escape", (await bridge(`testNote:${nid}`))?.[0], `${front} edited`);

// Add: input is kept unless discarded
await bridge("testMenuEvent:add");
await until("the Add window", () => isOpen("add"));
await until("the Add window's field", () => inWindow("add", `!!${FIELD}`));
await type("add", "unsaved");
await escape("add", FIELD);
const prompt = async () => {
    const d = await until("the discard prompt", async () => {
        const d = await bridge("testDialogs");
        return d.length ? d : undefined;
    });
    await until("the prompt's window", () => isOpen(d[0][0]));
    return d;
};
let dialogs = await prompt();
check("Add asks before discarding", dialogs.length, 1);
check("Add stays while asking", await isOpen("add"), true);
// Escape in the prompt keeps editing
await escape(dialogs[0][0], "document.body");
await until("the prompt to close", async () => !(await isOpen(dialogs[0][0])));
check("Escape in the prompt keeps Add", await staysOpen("add"), true);
check("and its input", await inWindow("add", `${FIELD}.textContent`), "unsaved");
await escape("add", FIELD);
dialogs = await prompt();
await bridge(`testDialog:${JSON.stringify({ button: 0, fields: {} })}`);
await until("Add to close", async () => !(await isOpen("add")));

// the browser
const TABLE = `document.getElementById("shell-browser")?.contentDocument`;
await bridge("testMenuEvent:browse");
await until("the browser", () => isOpen("browser"));
await until("the browser's search box", () => inWindow("browser", `!!${TABLE}?.querySelector("input.search")`));
await inWindow(
    "browser",
    `(() => { const f = ${TABLE}.querySelector("input.filter"); f.focus(); f.value = "x"; f.dispatchEvent(new Event("input", { bubbles: true })); return true; })()`,
);
await escape("browser", `${TABLE}.querySelector("input.filter")`);
check("the sidebar filter's Escape keeps the browser", await staysOpen("browser"), true);
check("and clears the filter", await inWindow("browser", `${TABLE}.querySelector("input.filter").value`), "");
await inWindow("browser", `(${TABLE}.querySelector("input.search").focus(), true)`);
await escape("browser", `${TABLE}.querySelector("input.search")`);
const closed = await until("the browser to close", async () => !(await isOpen("browser")), 5000).catch(() => false);
check("the search box's Escape closes the browser", closed, true);

return { ok: checks.every((c) => c.ok), checks: checks.filter((c) => !c.ok) };
