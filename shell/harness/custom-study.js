// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Custom study in WKWebView, on the current deck: the choices' ranges,
// then "study by card state or tag" making the Custom Study Session deck,
// checked against the collection and in the overview. Run with
// --test-page custom-study (on the current deck if it's a normal one,
// else the normal deck with the most cards).

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

const SESSION = "Custom Study Session";
const choose = async (value) => {
    const radio = document.querySelector(`input[name=choice][value=${value}]`);
    radio.click();
    await sleep(50);
};

if (testStage === 0) {
    await until("the dialog", () => document.querySelector("#value"));
    check(
        "new cards first, with what's available",
        document.querySelector("#title")?.textContent.startsWith("Available new cards"),
        true,
    );
    await choose("forgot");
    check("forgotten cards go back 30 days at most", document.querySelector("#value").max, "30");
    check("no counts for it", document.querySelector("#title"), null);
    await choose("cram");
    // all cards in random order
    document.querySelectorAll("input[name=cram]")[3].click();
    set("#value", "7");
    return handOver({}, "main", () => document.querySelector("#ok").click());
}

await takeOver();
await until("the overview", () => document.querySelector("h3")?.textContent === SESSION);
const count =
    (await query(`select count() from cards where did = (select id from decks where name = '${SESSION}')`))[0][0];
check("7 cards in the session deck", count, 7);
check("it's a filtered deck", !!button("Rebuild"), true);
return finish();
