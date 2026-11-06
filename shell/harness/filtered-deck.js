// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The filtered deck dialog in WKWebView: building a new deck (a search
// that matches nothing is refused and the dialog stays), then the
// overview of the new deck, its Options reopening the dialog, and a
// rebuild with a smaller limit, checked against the collection. Run with
// --test-page filtered-deck; the script follows the windows it opens.

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
const finish = () => ({ ok: checks.every((c) => c.ok) && errors.length === 0, checks: checks.filter((c) => !c.ok), errors });

const cardsIn = async (name) =>
    (await query(`select count() from cards where did = (select id from decks where name = '${name}')`))[0][0];

if (testStage === 0) {
    await until("the dialog", () => document.querySelector("#search"));
    check("a new deck is built", document.querySelector("#build").textContent.trim(), "Build");
    const name = `shell filtered ${Date.now()}`;
    set("#name", name);
    // nothing matches
    set("#search", "nid:1");
    document.querySelector("#build").click();
    await until("the error", () => document.querySelector("#error"));
    check("the dialog stays open", (await bridge("testWindows")).includes("filtered-deck"), true);
    // a deck with enough cards to filter
    const [[did]] = await query(
        "select did from cards where queue >= 0 and odid = 0 group by did having count() >= 10 limit 1",
    );
    set("#search", `did:${did}`);
    set("#limit", "5");
    return handOver({ name }, "main", () => document.querySelector("#build").click());
}

const state = await takeOver();
if (testStage === 1) {
    await until("the overview", () => document.querySelector("h3")?.textContent === state.name);
    check("the new deck's overview", location.pathname, "/deck-overview");
    check("5 cards in it", await cardsIn(state.name), 5);
    check("rebuild and empty offered", [!!button("Rebuild"), !!button("Empty")], [true, true]);
    check("no custom study for a filtered deck", !!button("Custom Study"), false);
    return handOver(state, "filtered-deck", () => button("Options").click());
}

if (testStage === 2) {
    await until("the dialog", () => document.querySelector("#search"));
    check("an existing deck is rebuilt", document.querySelector("#build").textContent.trim(), "Rebuild");
    check("its name", document.querySelector("#name").value, state.name);
    check("its limit", document.querySelector("#limit").value, "5");
    set("#limit", "3");
    return handOver(state, "main", () => document.querySelector("#build").click());
}

await until("the overview", () => document.querySelector("h3")?.textContent === state.name);
check("rebuilt with 3 cards", await cardsIn(state.name), 3);
return finish();
