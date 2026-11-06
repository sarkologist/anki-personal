// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Empty Cards in WKWebView: a note whose second card went empty is in the
// report; deleting keeps its first card. Run with --test-page empty-cards
// on a copy: it deletes every empty card in the copy.

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

const store = JSON.parse(sessionStorage.getItem("emptyTest") || "{}");
const cards = async (nid) => (await query(`select count() from cards where nid = ${nid}`))[0][0];

if (testStage === 0) {
    const nid = await bridge(`testAddNote:${JSON.stringify({
        notetype: "Basic (optional reversed card)",
        fields: [`shell empty ${Date.now()}`, "back", "y"],
    })}`);
    check("two cards to start with", await cards(nid), 2);
    await bridge(`testUpdateNote:${JSON.stringify({ nid, fields: ["shell empty", "back", ""] })}`);
    sessionStorage.setItem("emptyTest", JSON.stringify({ nid, checks, errors }));
    // the report is made when the page loads
    location.reload();
    return { next: true };
}

checks.push(...store.checks);
errors.push(...store.errors);
await until("the report", () => document.querySelector("li"));
// without Fluent's isolation marks
const text = (el) => el.textContent.replace(/[\u2068\u2069]/g, "");
const line = [...document.querySelectorAll("li")].find((li) => text(li).startsWith(`${store.nid}:`));
check("the note is listed", line && text(line).includes("1 of 2 cards empty"), true);
check("notes are kept by default", document.querySelector("#keep-notes").checked, true);
document.querySelector("#delete").click();
await until("the delete", () => document.querySelector("#done"));
check("its empty card is gone", await cards(store.nid), 1);
check("the deleted count is shown", /^Deleted \d+ cards?\.$/.test(document.querySelector("#done").textContent.replace(/[⁨⁩]/g, "")), true);
return finish();
