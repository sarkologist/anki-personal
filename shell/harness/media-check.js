// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Check Media in WKWebView: an unused file, a missing one and missing
// LaTeX images in the report; Tag Missing, Render LaTeX (with scratch
// commands, not the user's toolchain), Delete Unused, Restore Deleted and
// Empty Trash, checked against the collection and the next report. Run
// with --test-page media-check on a copy: it trashes the copy's unused
// files and renders its missing LaTeX.

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

const report = () => document.querySelector("#report")?.textContent ?? "";
const store = JSON.parse(sessionStorage.getItem("mediaTest") || "{}");
// the page counts the checks it has run; each action ends with one
const runs = () => Number(document.querySelector("main")?.dataset.checks ?? 0);
async function act(id, what, confirm = false) {
    const before = runs();
    document.querySelector(id).click();
    if (confirm) {
        await until("the confirmation", () => document.querySelector(".dialog button.primary"));
        document.querySelector(".dialog button.primary").click();
    }
    await until(what, () => runs() > before);
}

if (testStage === 0) {
    await bridge(`testLatexToolchain:${
        JSON.stringify({
            png: [["sh", "-c", "printf shellpng > tmp.png"]],
            svg: [["sh", "-c", "printf shellsvg > tmp.svg"]],
        })
    }`);
    const stamp = Date.now();
    const unused = await bridge(`testAddMediaFile:${JSON.stringify({ name: `shell-unused-${stamp}.txt`, text: "x" })}`);
    const missing = `shell-missing-${stamp}.png`;
    const nid = await bridge(`testAddNote:${
        JSON.stringify({
            notetype: "Basic",
            fields: [`<img src="${missing}"> [$]shell ${stamp}[/$]`, ""],
        })
    }`);
    sessionStorage.setItem("mediaTest", JSON.stringify({ unused, missing, nid, checks, errors }));
    // the check runs when the page loads
    location.reload();
    return { next: true };
}

checks.push(...store.checks);
errors.push(...store.errors);
await until("the report", () => runs() > 0);
check("the unused file is listed", report().includes(`Unused: ${store.unused}`), true);
check("the missing file is listed", report().includes(`Missing: ${store.missing}`), true);

await act("#tag-missing", "tagging");
const [[tags]] = await query(`select tags from notes where id = ${store.nid}`);
check("the note is tagged", tags.trim(), "missing-media");

// (the copy may also miss latex- files that no [$] makes any more)
const missingLatex = () => report().match(/Missing: latex-/g)?.length ?? 0;
const before = missingLatex();
await act("#render-latex", "the LaTeX render");
check("all LaTeX rendered", document.querySelector("#status").textContent, "All LaTeX rendered.");
check("fewer LaTeX images missing", missingLatex() < before, true);

await act("#delete-unused", "the delete", true);
check("the unused file is gone", report().includes(store.unused), false);

await act("#restore-trash", "the restore");
check("restored", report().includes(`Unused: ${store.unused}`), true);

await act("#delete-unused", "the delete", true);
await act("#empty-trash", "the empty trash");
check("the trash is empty", [document.querySelector("#status").textContent, !!document.querySelector("#empty-trash")], [
    "The trash folder is now empty.",
    false,
]);
return finish();
