// Chinese Frontier in the plugin host, on a profile copy with the add-on in
// its addons21 and "plugins": ["chinese_frontier"] in the settings:
// `--test shell/harness/frontier-flow.js --test-page main`, with
// ANKI_SHELL_TEST_TIMEOUT raised, since the job calls the model CLI.
// Checks the Tools items, the deck browser notice, the settings panel and
// its live status, the reviewer menu's items, and that the background job
// adds a sentence card (a new one, or one taking over a pending fix's
// card) within 20 minutes.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (what, fn, ms = 60000) => {
    const end = Date.now() + ms;
    for (;;) {
        const v = await fn();
        if (v) { return v; }
        if (Date.now() > end) { throw new Error("timed out waiting for " + what); }
        await sleep(200);
    }
};
const query = async (sql) => JSON.parse(JSON.stringify(await bridge("testQuery:" + sql)));
const config = async (key) => {
    const rows = await query(`select cast(val as text) from config where key = '${key}'`);
    return rows.length ? JSON.parse(rows[0][0]) : {};
};
const panels = async () => (await bridge("testWindows")).filter((l) => l.startsWith("addon-panel-"));
// run `js` (an expression) in a window and get its JSON value back
const inWindow = async (label, js) => {
    await bridge(`testEval:${label}\npycmd("testStash:" + JSON.stringify((() => ${js})()))`);
    return until(`an answer from ${label}`, async () => {
        const v = await bridge("testStash");
        return v === null ? null : { v };
    }).then((r) => r.v);
};

if (location.pathname !== "/deck-browser" || testStage !== 0) {
    return { next: true };
}
const minutes = 20;
const chineseDeck = (await config("chineseFrontier")).deck_id;
const fixesBefore = Object.keys(await config("chineseFrontierFixes"));
const generatedNotes = "select id from notes where tags like '%chinese-frontier::generated%'";
const generatedBefore = (await query(generatedNotes)).map((r) => r[0]);
log(`deck ${chineseDeck}, pending fixes ${fixesBefore.length}, generated notes ${generatedBefore.length}`);

const menu = await until("the add-on's Tools items", async () => {
    const items = await bridge("testAddonMenu");
    return items.length >= 3 && items;
});
log("Tools: " + menu.map((i) => i.text).join(" | "));
const settingsItem = menu.find((i) => i.text === "Chinese Frontier…");
if (
    !settingsItem
    || !menu.some((i) => i.text.startsWith("Chinese Frontier: needs attention"))
    || !menu.some((i) => i.text === "French Frontier…")
) {
    return { ok: false, error: "missing Tools items", menu };
}

const notice = await until(
    "the deck browser notice",
    () => document.body.innerText.match(/Chinese Frontier: [^\n]*attention[^\n]*/)?.[0],
    120000,
);
log("notice: " + notice);

await bridge("testAddonAction:" + settingsItem.id);
const [panel] = await until("the settings panel", async () => (await panels()).length && panels());
const text = () => inWindow(panel, "document.body.innerText");
// the first read-only box
const status = () => inWindow(panel, "document.querySelector('textarea[readonly]')?.value ?? ''");
const first = await until("the settings form", async () => {
    const t = await text();
    return t.includes("Chinese source deck") && t;
});
log("settings panel: " + first.replace(/\s+/g, " ").slice(0, 500));

// a learner card in the chosen deck gets the fix items
const [[card]] = await query(
    `select c.id from cards c join notes n on c.nid = n.id where c.did = ${chineseDeck}
     and c.queue >= 0 and n.tags not like '%chinese-frontier::%' limit 1`,
);
const items = await bridge("testContextMenu:" + card);
log(`reviewer menu for ${card}: ` + items.map((i) => i.text).join(" | "));
if (!items.some((i) => /sentence/.test(i.text))) {
    return { ok: false, error: "no Frontier items in the reviewer menu", items };
}

// the background job: a pending fix becomes a generated note; the settings
// panel's status follows it
const end = Date.now() + minutes * 60000;
let last = "";
for (;;) {
    const fixes = Object.keys(await config("chineseFrontierFixes"));
    const done = fixesBefore.filter((cid) => !fixes.includes(cid));
    const added = (await query(generatedNotes)).map((r) => r[0]).filter((id) => !generatedBefore.includes(id));
    const now = (await status()).split("\n")[0];
    if (now !== last) {
        log("status: " + now);
        last = now;
    }
    if (added.length) {
        const notes = await query(
            `select n.id, n.flds, c.id, c.queue, c.type, c.due, c.ivl from notes n join cards c on c.nid = n.id
             where n.id in (${added.join(",")})`,
        );
        log(`added: ${JSON.stringify(notes)}; fixes done: ${done}`);
        await inWindow(
            panel,
            "(document.querySelectorAll('button').forEach((b) => b.textContent === 'Close' && b.click()), true)",
        );
        await until("the panel to close", async () => !(await panels()).length);
        return { ok: true, added: notes, fixed: done };
    }
    if (Date.now() > end) {
        return { ok: false, error: `no fix done in ${minutes} minutes`, status: last };
    }
    await sleep(15000);
}
