// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The card browser in WKWebView, checked against the collection: the
// table (search, sorting, selection, columns, modes), the editor beside it,
// the sidebar, the actions on the selection and their dialogs, find and
// replace, duplicates, the previewer and card info. Run with
// --test-page browser: a test deck is the current deck, so the browser
// opens on its four cards.

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
const scalar = async (sql) => (await query(sql))[0][0];
async function answerDialog(answer) {
    await until("a dialog", async () => (await bridge("testDialogs")).length > 0);
    return bridge(`testDialog:${JSON.stringify(answer)}`);
}
/** JS in another window, which leaves its result for us. */
async function inWindow(label, expr) {
    await bridge("testStash:");
    await bridge(
        `testEval:${label}\n(async () => { let v; try { v = await (${expr}); } catch (e) { v = "error: " + e; } `
            + `pycmd("testStash:" + JSON.stringify({ v: v ?? null })); })();`,
    );
    return (await until(`a value from ${label}`, async () => {
        const v = await bridge("testStash:");
        return v && "v" in v ? v : null;
    })).v;
}

const errors = [];
window.addEventListener("error", (e) => errors.push(String(e.message)));

// The table's frame
const frameEl = document.getElementById("shell-browser");
await until("the table's page", () => frameEl.contentWindow?.browserReceive);
const win = frameEl.contentWindow;
const doc = win.document;
win.addEventListener("error", (e) => errors.push(`table: ${e.message}`));
const rows = () => [...doc.querySelectorAll(".tr")];
const cells = (row) => [...row.querySelectorAll(".td")].map((td) => td.textContent.trim());
const header = () => [...doc.querySelectorAll(".th .label")].map((e) => e.textContent);
const column = (label) => header().indexOf(label);
const selectedRows = () => rows().filter((r) => r.classList.contains("selected")).map((r) => Number(r.dataset.row));
const searchBox = doc.querySelector("input.search");
const scroller = doc.querySelector(".scroller");
const key = (code, mods = {}, target = doc) =>
    target.dispatchEvent(
        new win.KeyboardEvent("keydown", {
            code,
            key: mods.key ?? "",
            metaKey: !!mods.meta,
            shiftKey: !!mods.shift,
            altKey: !!mods.alt,
            bubbles: true,
            cancelable: true,
        }),
    );
const press = (row, mods = {}) =>
    row.dispatchEvent(
        new win.MouseEvent("mousedown", { button: 0, bubbles: true, shiftKey: !!mods.shift, metaKey: !!mods.meta }),
    );
const rowCount = () => Number(scroller.getAttribute("aria-rowcount"));
async function search(text) {
    searchBox.value = text;
    searchBox.dispatchEvent(new win.Event("input"));
    key("Enter", { key: "Enter" }, searchBox);
    await sleep(300);
}
const deckCards = `select id from cards where did = ${testDeck}`;
const deckNotes = `select distinct nid from cards where did = ${testDeck}`;

// 0. The theme: the table's frame follows the page (dark mode).
const night = document.documentElement.classList.contains("night-mode");
log(`night mode: ${night}`);
check("the table's frame has the page's theme", doc.documentElement.classList.contains("night-mode"), night);
const luminance = (el) => {
    const m = win.getComputedStyle(el).backgroundColor.match(/[\d.]+/g).map(Number);
    return (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255;
};
check("its background matches the theme", luminance(doc.body) < 0.5, night);

// 1. The default search: the current deck, the test deck.
await until("its four cards", () => rows().length === 4 && cells(rows()[0]).some(Boolean));
check(
    "default search shows the current deck",
    rowCount(),
    Number(await scalar(`select count() from cards where did = ${testDeck}`)),
);
const activeCols = JSON.parse(
    String.fromCharCode(...(await query("select val from config where key = 'activeCols'"))[0][0]),
);
check("the saved columns", header().length, activeCols.length);
const sortField = column("Sort Field");
check("sort field column", sortField >= 0, true);

// 2. Sorting by a header click, and reversing.
const sortHeader = doc.querySelectorAll(".th")[sortField];
sortHeader.click();
await until("sorted by it", () => sortHeader.classList.contains("sorted"));
await sleep(300);
const fronts = () => rows().map((r) => cells(r)[sortField]);
const ascending = sortHeader.querySelector(".arrow").textContent === "▲";
const sorted = [...fronts()].sort((a, b) => a.localeCompare(b));
check("rows sorted by the sort field", fronts(), ascending ? sorted : [...sorted].reverse());
sortHeader.click();
await until("reversed", () => sortHeader.querySelector(".arrow").textContent === (ascending ? "▼" : "▲"));
await sleep(300);
check("a second click reverses", fronts(), ascending ? [...sorted].reverse() : sorted);
// ascending from here on
if (sortHeader.querySelector(".arrow").textContent !== "▲") {
    sortHeader.click();
    await until("ascending", () => sortHeader.querySelector(".arrow").textContent === "▲");
    await sleep(300);
}
check(
    "sort saved in the collection",
    JSON.parse(String.fromCharCode(...(await query("select val from config where key = 'sortType'"))[0][0])),
    "noteFld",
);

// 3. One row: its note in the editor, and editing it updates the row.
press(rows()[0]);
await until("the editor", () => !document.body.classList.contains("shell-no-note"));
const editable = () => document.querySelector(".rich-text-editable")?.shadowRoot?.querySelector("anki-editable");
await until("the note's text", () => editable()?.textContent === fronts()[0]);
const [[firstNote]] = await query(
    `select nid from cards where did = ${testDeck} and nid in (select id from notes where sfld = '${fronts()[0]}')`,
);
check("the editor shows the selected card's note", editable().textContent, fronts()[0]);
editable().focus();
document.getSelection().selectAllChildren(editable());
document.execCommand("insertText", false, "shell browser edited");
await until("the edit saved", async () => (await bridge(`testNote:${firstNote}`))?.[0] === "shell browser edited");
await until("the row redrawn", () => fronts().includes("shell browser edited"));
check("the row shows the edit", fronts().includes("shell browser edited"), true);

// 4. Shift+click: several rows, no editor.
press(rows()[2], { shift: true });
await until("three selected", () => selectedRows().length === 3);
await until("no editor", () => document.body.classList.contains("shell-no-note"));
check("several rows hide the editor", document.body.classList.contains("shell-no-note"), true);
const ids = async (sql) => (await query(sql)).map((r) => r[0]);

// 5. Actions on the selection, by their keys.
const three = async () => {
    // the three selected rows' cards, by sort field
    const names = selectedRows().map((r) => cells(rows()[r])[sortField]);
    return ids(
        `select c.id from cards c, notes n where c.nid = n.id and c.did = ${testDeck} and n.sfld in (${
            names.map((n) => `'${n}'`).join(",")
        })`,
    );
};
const selected = await three();
check("three cards", selected.length, 3);
const inList = `(${selected.join(",")})`;
key("KeyJ", { meta: true });
await until(
    "suspended",
    async () => (await scalar(`select count() from cards where queue = -1 and id in ${inList}`)) === 3,
);
key("KeyJ", { meta: true });
await until(
    "unsuspended",
    async () => (await scalar(`select count() from cards where queue = -1 and id in ${inList}`)) === 0,
);
check("Cmd+J suspends and unsuspends", true, true);
key("Digit1", { meta: true });
await until(
    "flagged",
    async () => (await scalar(`select count() from cards where flags & 7 = 1 and id in ${inList}`)) === 3,
);
// the red row colour, light or dark with the theme (the rows are redrawn
// after the change; look at one not selected)
press(rows()[3]);
await until("one row", () => selectedRows().join() === "3");
const flaggedRow = await until("a red row", () => rows().find((r) => r.classList.contains("color-3")));
check(
    "flag colours follow the theme",
    [
        win.getComputedStyle(doc.documentElement).getPropertyValue("--flag-1").trim(),
        win.getComputedStyle(flaggedRow).backgroundColor !== "rgba(0, 0, 0, 0)",
    ],
    [night ? "#f87171" : "#ef4444", true],
);
press(rows()[0]);
press(rows()[2], { shift: true });
await until("three again", () => selectedRows().length === 3);
key("Digit1", { meta: true });
await until(
    "unflagged",
    async () => (await scalar(`select count() from cards where flags & 7 = 1 and id in ${inList}`)) === 0,
);
check("Cmd+1 flags and unflags", true, true);
key("KeyK", { meta: true });
const marked = () =>
    scalar(
        `select count() from notes where tags like '% marked %' and id in (select nid from cards where id in ${inList})`,
    );
await until("marked", async () => (await marked()) === 3);
check("Cmd+K marks", await marked(), 3);
// undo from the table
key("KeyZ", { meta: true });
await until("unmarked by undo", async () => (await marked()) === 0);
check("Cmd+Z undoes", await marked(), 0);
// tags
key("KeyA", { meta: true, shift: true });
await answerDialog({ button: 0, fields: { tags: "shell-browser-tag" } });
const tagged = () =>
    scalar(
        `select count() from notes where tags like '%shell-browser-tag%' and id in (select nid from cards where id in ${inList})`,
    );
await until("tagged", async () => (await tagged()) === 3);
key("KeyA", { meta: true, shift: true, alt: true });
await answerDialog({ button: 0, fields: { tags: "shell-browser-tag" } });
await until("untagged", async () => (await tagged()) === 0);
check("add and remove tags", await tagged(), 0);
// deck
key("KeyD", { meta: true });
await answerDialog({ button: 0, fields: { id: "1" } });
await until("moved", async () => (await scalar(`select count() from cards where did = 1 and id in ${inList}`)) === 3);
key("KeyD", { meta: true });
await answerDialog({ button: 0, fields: { id: String(testDeck) } });
await until(
    "moved back",
    async () => (await scalar(`select count() from cards where did = ${testDeck} and id in ${inList}`)) === 3,
);
check("change deck", true, true);
// scheduling: due today, then reset, then reposition
key("KeyD", { meta: true, shift: true });
await answerDialog({ button: 0, fields: { days: "0" } });
await until(
    "due today",
    async () => (await scalar(`select count() from cards where type = 2 and id in ${inList}`)) === 3,
);
key("KeyN", { meta: true, alt: true });
await answerDialog({ button: 0, fields: { restore: "true", reset: "true" } });
await until(
    "new again",
    async () => (await scalar(`select count() from cards where type = 0 and id in ${inList}`)) === 3,
);
check("set due date and reset", true, true);
key("KeyS", { meta: true, shift: true });
await answerDialog({ button: 0, fields: { start: "900000", step: "1", random: "false", shift: "false" } });
await until(
    "repositioned",
    async () => (await scalar(`select count() from cards where due >= 900000 and id in ${inList}`)) === 3,
);
check("reposition", true, true);

// 6. Find and replace in the selected notes.
key("KeyF", { meta: true, alt: true });
await answerDialog({
    button: 0,
    fields: { find: "back", replace: "BACKX", field: "__all", selected: "true", ignoreCase: "true", regex: "false" },
});
const replaced = () =>
    scalar(
        `select count() from notes where flds like '%BACKX%' and id in (select nid from cards where id in ${inList})`,
    );
await until("replaced", async () => (await replaced()) > 0);
check("find and replace", (await replaced()) > 0, true);
check(
    "only the selected notes",
    await scalar(
        `select count() from notes where flds like '%BACKX%' and id not in (select nid from cards where id in ${inList})`,
    ),
    0,
);

// 7. Duplicates: make two notes' backs the same, find and tag them.
const [noteA, noteB] = await ids(`select distinct nid from cards where id in ${inList} order by nid`);
const backs = await query(`select id, flds from notes where id in (${noteA}, ${noteB})`);
log(`backs ${JSON.stringify(backs)}`);
// select only the second and give it the first's back
const rowOf = async (nid) => {
    const [[sfld]] = await query(`select sfld from notes where id = ${nid}`);
    return rows().find((r) => cells(r)[sortField] === String(sfld));
};
press(await rowOf(noteB));
await until("one selected", () => selectedRows().length === 1);
const backA = String(backs.find((b) => b[0] === noteA)[1]).split("\x1f")[1];
key("KeyF", { meta: true, alt: true });
await answerDialog({
    button: 0,
    fields: { find: "^.*$", replace: backA, field: "Back", selected: "true", ignoreCase: "false", regex: "true" },
});
await until(
    "same backs",
    async () => (await scalar(`select count() from notes where id = ${noteB} and flds like '%${backA}'`)) === 1,
);
await bridge("testMenuEvent:browser:findDupes");
await answerDialog({ button: 0, fields: { field: "Back", search: `did:${testDeck}` } });
await until("the duplicates window", async () => (await bridge("testWindows")).includes("browse-dupes"));
const groups = await inWindow(
    "browse-dupes",
    "new Promise((r) => setTimeout(() => r([...document.querySelectorAll('li')].map((li) => li.textContent)), 300))",
);
log(`groups ${JSON.stringify(groups)}`);
check("one group of two", groups.length === 1 && /^\u2068?2\u2069? notes/.test(groups[0]), true);
await inWindow("browse-dupes", "document.querySelector('a[data-i]').click()");
await until("the group searched", () => rowCount() === 2 || rowCount() === 4);
log(`group search: ${searchBox.value}`);
check("searching the group finds its notes", /^nid:/.test(searchBox.value), true);
await inWindow("browse-dupes", "document.getElementById('tag').click()");
await until(
    "tagged duplicate",
    async () =>
        (await scalar(`select count() from notes where tags like '% duplicate %' and id in (${noteA}, ${noteB})`))
            === 2,
);
check("tag duplicates", true, true);

// 8. Notes mode, and back.
await search(`did:${testDeck}`);
await until("the deck again", () => rowCount() === 4);
key("KeyT", { meta: true, alt: true });
await until("notes mode", () => doc.querySelector(".mode button.active")?.textContent.trim() === "Notes");
await until("notes counted", async () => rowCount() === (await query(deckNotes)).length);
check("notes mode shows notes", rowCount(), (await query(deckNotes)).length);
key("KeyT", { meta: true, alt: true });
await until("cards mode", () => doc.querySelector(".mode button.active")?.textContent.trim() === "Cards");
check("cards mode again", rowCount(), 4);

// 9. Columns from the header's menu.
const before = header().length;
await bridge("testMenuEvent:browser:column:cardIvl");
await until("the column", () => header().length === before + 1);
check("a column shown", header().includes("Interval"), true);
await bridge("testMenuEvent:browser:column:cardIvl");
await until("the column gone", () => header().length === before);
check("and hidden", header().includes("Interval"), false);

// 10. The sidebar: a deck, Cmd+click to add, Option+click to negate, a saved search.
const sideItem = (name) =>
    [...doc.querySelectorAll(".sidebar .item")].find((i) => i.querySelector(".name")?.textContent === name);
const [[deckName]] = await query(`select name from decks where id = ${testDeck}`);
sideItem(String(deckName)).dispatchEvent(new win.MouseEvent("click", { bubbles: true }));
await until("the deck searched", () => searchBox.value.includes("deck:shell test"));
check("a sidebar deck searches for it", rowCount(), 4);
sideItem("New").dispatchEvent(new win.MouseEvent("click", { bubbles: true, metaKey: true }));
await until("ANDed", () => searchBox.value.endsWith("is:new"));
check("Cmd+click adds to the search", searchBox.value.includes("deck:") && searchBox.value.endsWith("is:new"), true);
sideItem("Red").dispatchEvent(new win.MouseEvent("click", { bubbles: true, altKey: true }));
await until("negated", () => searchBox.value === "-flag:1");
check("Option+click negates", searchBox.value, "-flag:1");
const filter = doc.querySelector(".sidebar .filter");
filter.value = String(deckName).slice(0, 10);
filter.dispatchEvent(new win.Event("input"));
await sleep(200);
check(
    "the filter shows matches with parents",
    [...doc.querySelectorAll(".sidebar .item .name")].map((n) => n.textContent).includes(String(deckName)),
    true,
);
filter.value = "";
filter.dispatchEvent(new win.Event("input"));

// 11. The search bar: errors are shown, the history kept.
await search("(");
await until("an error", async () => (await bridge("testDialogs")).length > 0);
await answerDialog({ button: 0, fields: {} });
await search(`did:${testDeck}`);
await until("the deck", () => rowCount() === 4);
check(
    "searches kept in the history",
    [...doc.querySelectorAll("#browser-history option")].some((o) => o.value === `did:${testDeck}`),
    true,
);

// 12. Previewer and card info follow the current card.
await until("its rows", () => rows().length === 4 && cells(rows()[0])[sortField]);
press(rows()[0]);
await until("one row", () => selectedRows().length === 1);
const firstFront = fronts()[0];
key("KeyP", { meta: true, shift: true });
await until("the previewer", async () => (await bridge("testWindows")).includes("browse-preview"));
const shown = () =>
    inWindow(
        "browse-preview",
        "document.getElementById('card').contentDocument?.getElementById('qa')?.textContent || ''",
    );
await until("the card", async () => (await shown()).includes(firstFront));
check("the previewer shows the card", (await shown()).includes(firstFront), true);
check(
    "the previewer's card follows the theme",
    await inWindow(
        "browse-preview",
        "document.getElementById('card').contentDocument.body.classList.contains('nightMode')",
    ),
    night,
);
const both = await inWindow("browse-preview", "document.getElementById('both').checked");
await inWindow("browse-preview", "document.getElementById('next').click()");
if (both) {
    // "back side only": Next goes to the next card
    await until("the next card", () => selectedRows()[0] === 1);
} else {
    await until("its answer", async () => (await shown()).length > firstFront.length);
}
check("Next shows the answer, or the next card", true, true);
press(rows()[0]);
await until("the first again", () => selectedRows()[0] === 0);
await sleep(500);
const shownBefore = await shown();
key("ArrowDown", { key: "ArrowDown" }, scroller);
await until("the arrow moves down", () => selectedRows().join() === "1");
// (the copy's templates decide what each side shows)
await until("the previewer follows", async () => {
    const now = await shown();
    return now && now !== shownBefore;
});
check("the previewer follows the table", true, true);
key("KeyI", { meta: true, shift: true });
await until("card info", async () => (await bridge("testWindows")).includes("browse-card-info"));
check("card info opens", true, true);

// 13. Show Duplicates in the browser's editor searches for them.
press(await rowOf(noteA));
await until("its note", () => !document.body.classList.contains("shell-no-note") && editable());
pycmd("dupes");
await until("dupe search", () => searchBox.value.includes("dupe:"));
check("Show Duplicates searches dupe: (the note itself, at least)", rowCount() >= 1, true);

// 14. Delete: the row stays, greyed.
await search(`did:${testDeck}`);
await until("the deck", () => rowCount() === 4);
const notesBefore = (await query(deckNotes)).length;
await until("its rows", () => rows().length === 4 && cells(rows()[3])[sortField]);
press(rows()[3]);
await until("one row", () => selectedRows().length === 1);
key("Backspace", { meta: true });
await until("deleted", async () => (await query(deckNotes)).length === notesBefore - 1);
await until("greyed", () => rows().some((r) => r.classList.contains("disabled")));
check("Cmd+Backspace deletes the note", (await query(deckNotes)).length, notesBefore - 1);

check("no page errors", errors, []);
return { ok: checks.every((c) => c.ok), failed: checks.filter((c) => !c.ok) };
