// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// suspended_card_priority in the browser, through the plugin host: its
// column (filled and sorted by the add-on), its menu, and an action that
// asks the browser for its current card. Run with --test-page browser on a
// copy with the add-on in addons21, "plugins": ["suspended_card_priority"]
// in the settings, and the profile's suspended-card-priority.json.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(what, f, ms = 20000) {
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

const frameEl = document.getElementById("shell-browser");
await until("the table's page", () => frameEl.contentWindow?.browserReceive);
const win = frameEl.contentWindow;
const doc = win.document;
const header = () => [...doc.querySelectorAll(".th .label")].map((e) => e.textContent);
const rows = () => [...doc.querySelectorAll(".tr")];
const cells = (row) => [...row.querySelectorAll(".td")].map((td) => td.textContent.trim());
const searchBox = doc.querySelector("input.search");

// 1. The add-on's column and menu.
await until(
    "the add-on's menu",
    () => [...doc.querySelectorAll("button.menu")].some((b) => b.textContent.includes("Priority")),
);
await until("the add-on's column label", () => header().includes("Priority"));
check("the Priority column is labelled by the add-on", header().includes("Priority"), true);
const [[, items]] = (await bridge("testBrowserMenus")).filter(([title]) => title === "Priority");
check("its menu items", items.map((i) => i.text).includes("Explain current priority"), true);
const action = (text) => bridge(`testMenuEvent:addon:${items.find((i) => i.text === text).id}`);

// 2. "Show Math priority queue": its search, sorted by the add-on.
await action("Show Math priority queue");
await until("its search", () => searchBox.value.includes("is:suspended") && searchBox.value.includes("math"));
const priority = header().indexOf("Priority");
const sortedHeader = doc.querySelectorAll(".th")[priority];
await until("sorted by Priority", () => sortedHeader.classList.contains("sorted"));
check("sorted by Priority, highest first", sortedHeader.querySelector(".arrow")?.textContent, "▼");
// the selection (the test deck's card) moved with the sort; look at the top
const scroller = doc.querySelector(".scroller");
const toTop = async () => {
    scroller.scrollTop = 0;
    scroller.dispatchEvent(new win.Event("scroll"));
    await until("the top rows", () => rows()[0]?.dataset.row === "0" && rows().every((r) => r.dataset.id));
    await sleep(300);
};
await toTop();
const scores = JSON.parse(await bridge("testReadFile:User 1/suspended-card-priority.json")).scores;
const shownIds = rows().map((r) => Number(r.dataset.id));
const expected = shownIds.map((id) => scores[String(id)]?.score ?? null);
const numbers = expected.filter((s) => s !== null);
check("rows in the add-on's order (saved scores, unscored last)", numbers, [...numbers].sort((a, b) => b - a));
const firstNull = expected.indexOf(null);
check(
    "unscored rows after the scored ones",
    firstNull === -1 || expected.slice(firstNull).every((s) => s === null),
    true,
);
const texts = rows().map((r) => cells(r)[priority]);
log(`first rows: ${JSON.stringify(texts.slice(0, 8))}`);
// what the add-on writes: its score, "Review", or "Stale" for cards changed
// since they were scored (most of this copy's, after its MathJax rewrite)
check(
    "the column shows the add-on's values",
    texts.some(Boolean) && texts.every((t) => /^(\d+|Review|Stale|)$/.test(t)),
    true,
);
check("scores as saved", texts.every((t, i) => !/^\d+$/.test(t) || Number(t) === expected[i]), true);
const [[suspended]] = await query(
    `select count() from cards where queue = -1 and did in (select id from decks where name = 'math' or name like 'math\x1f%')`,
);
check("the search's count", Number(doc.querySelector(".scroller").getAttribute("aria-rowcount")), suspended);

// 3. An action asking the browser for its current card.
rows()[0].dispatchEvent(new win.MouseEvent("mousedown", { button: 0, bubbles: true }));
await sleep(500);
await action("Explain current priority");
const dialog = await until("its message", async () => (await bridge("testDialogs"))[0]);
log(`message: ${dialog[1].slice(0, 160)}`);
check(
    "Explain shows the current card's priority",
    /Priority \d+\/100|Needs review|changed since|not been scored/.test(dialog[1]),
    true,
);
await bridge(`testDialog:${JSON.stringify({ button: 0, fields: {} })}`);

// 4. Reversing re-sorts through the add-on: unscored cards stay last.
sortedHeader.click();
await until("ascending", () => sortedHeader.querySelector(".arrow")?.textContent === "▲");
await sleep(800);
await toTop();
const ascending = rows().map((r) => scores[String(Number(r.dataset.id))]?.score ?? null);
const ascNumbers = ascending.filter((s) => s !== null);
check("ascending, lowest first", ascNumbers, [...ascNumbers].sort((a, b) => a - b));

return { ok: checks.every((c) => c.ok), failed: checks.filter((c) => !c.ok) };
