// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The Add Cards window in WKWebView: the notetype and deck choosers,
// typing, sticky fields, adding (checked against the collection), the
// history, duplicates and the format buttons. Run with --test-page add.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const frame = () =>
    Promise.race([
        new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0))),
        new Promise((resolve) => setTimeout(resolve, 50)),
    ]);
async function until(what, f, ms = 10000) {
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
const editables = () =>
    [...document.querySelectorAll(".rich-text-editable")].map((f) => f.shadowRoot?.querySelector("anki-editable"));
async function type(index, text) {
    const editable = editables()[index];
    editable.focus();
    await frame();
    const sel = document.getSelection();
    sel.selectAllChildren(editable);
    document.execCommand("insertText", false, text);
    await frame();
}
/** Answer the picker or dialog that opens next. */
async function answerDialog(answer) {
    await until("a dialog", async () => (await bridge("testDialogs")).length > 0);
    return bridge(`testDialog:${JSON.stringify(answer)}`);
}
async function noteLoad(f) {
    const before = window.__shellLoads ?? 0;
    await f();
    await until("the note to load", () => (window.__shellLoads ?? 0) > before);
    await frame();
}
// count noteLoaded, which every load ends with
const pycmdOrig = window.pycmd;
window.pycmd = (cmd, cb) => {
    if (cmd === "noteLoaded") {
        window.__shellLoads = (window.__shellLoads ?? 0) + 1;
    }
    return pycmdOrig(cmd, cb);
};

// 1. The bars and the chooser buttons.
const notetypeButton = document.getElementById("shell-notetype");
const deckButton = document.getElementById("shell-deck");
await until("the chooser names", () => notetypeButton?.textContent && deckButton?.textContent);
check("bars are above and below the fields", [
    document.getElementById("shell-top").getBoundingClientRect().bottom
        <= document.querySelector(".note-editor").getBoundingClientRect().top + 1,
    document.getElementById("shell-bottom").getBoundingClientRect().top
        >= document.querySelector(".note-editor").getBoundingClientRect().bottom - 1,
], [true, true]);

// 2. Choose Basic and a deck through the pickers.
const [[basic]] = await query("select id from notetypes where name = 'Basic'");
const choose = (id) => async () => {
    pycmd("chooseNotetype");
    await answerDialog({ button: 0, fields: { id: String(id) } });
};
// through another notetype, so Basic is a change; typed text carries over
const [[other]] = await query("select id from notetypes where name != 'Basic' order by id limit 1");
await noteLoad(choose(other));
await type(0, "carried over");
await sleep(800);
await noteLoad(choose(basic));
check("text carried over to the new notetype", editables()[0].textContent, "carried over");
check("notetype chosen", notetypeButton.textContent, "Basic");
check("Basic has two fields", editables().length, 2);
// a normal deck (kind is a protobuf oneof: 0x0a normal, 0x12 filtered)
const [[deckId, deckName]] = await query(
    "select id, name from decks where id != 1 and substr(hex(kind), 1, 2) = '0A' "
        + "and name not like 'shell test%' order by id limit 1",
);
pycmd("chooseDeck");
await answerDialog({ button: 0, fields: { id: String(deckId) } });
await until("the deck name", () => deckButton.textContent !== "" && deckButton.textContent.length > 0);
check("deck chosen", deckButton.textContent, String(deckName).replaceAll("\x1f", "::"));

// 3. Format buttons: all thirteen, and they act on the selection.
const formatButtons = [...document.querySelectorAll(".anki-addon-button")].map((b) => b.dataset.command);
check("format buttons", formatButtons.length, 13);
const stamp = String(Date.now());
await type(0, `shell add ${stamp}`);
const front = editables()[0];
document.getSelection().selectAllChildren(front);
document.querySelector("[data-command='format:strikeThrough']").click();
await until("strikethrough", () => /<(strike|s)>/.test(front.innerHTML));
// Cmd+, is inline code
document.getSelection().selectAllChildren(front);
document.dispatchEvent(
    new KeyboardEvent("keydown", { key: ",", code: "Comma", metaKey: true, bubbles: true }),
);
await until("inline code", () => front.innerHTML.includes("<code>"));
check("format hotkey and button", /<code>.*shell add/.test(front.innerHTML), true);

// 4. A sticky back field, tags, then add.
check("sticky on", await bridge("toggleSticky:1"), true);
await type(1, "sticky back");
await bridge(`saveTags:${JSON.stringify(["shell-add-test"])}`);
await sleep(800);
await noteLoad(() => pycmd("addNote"));
const added = await until("the note in the collection", async () => {
    const rows = await query(
        `select id, flds, tags from notes where flds like '%shell add ${stamp}%'`,
    );
    return rows.length ? rows : null;
});
const [nid, flds, tags] = added[0];
check("added fields", flds.split("\x1f")[1], "sticky back");
check("front kept its formatting", /<code>/.test(flds.split("\x1f")[0]), true);
check("added tags", tags.trim(), "shell-add-test");
const [[cardDeck]] = await query(`select did from cards where nid = ${nid}`);
check("card in the chosen deck", cardDeck, deckId);
check("next note: front empty", editables()[0].innerHTML.replace(/<br>$/, ""), "");
check("next note: sticky back kept", editables()[1].textContent, "sticky back");
check("history enabled", document.getElementById("shell-history").disabled, false);
const history = await bridge("addHistory");
check("history lists the note", history[0]?.[0], nid);

// 5. Empty front: nothing added, a warning instead.
const count = async () => (await query("select count() from notes"))[0][0];
const before = await count();
pycmd("addNote");
const warning = await until("the warning", async () => (await bridge("testDialogs"))[0]?.[1]);
check("empty front warns", /empty/i.test(warning), true);
await bridge(`testDialog:${JSON.stringify({ button: 0, fields: {} })}`);
check("nothing added", await count(), before);

// 6. Duplicates are marked.
await type(0, `shell add ${stamp}`);
await sleep(900);
await until("the duplicate mark", () => document.querySelector(".dupe, [class*='dupe']"));
check("duplicate marked", true, true);
const dupes = document.querySelector(".duplicate-link, a[href='#']");
check("show duplicates link", Boolean(dupes), true);

// back to how it was: sticky off, fields empty (so closing doesn't ask)
check("sticky off", await bridge("toggleSticky:1"), false);
await type(0, "");
await type(1, "");
await sleep(800);

return { ok: checks.every((c) => c.ok) && errors.length === 0, checks, errors };
