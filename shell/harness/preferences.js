// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The Preferences window in WKWebView: the theme (dark mode in open and
// newly loaded pages), a collection preference (checked in the
// collection), and the LaTeX commands (a bad line refused, custom
// commands used to render, the old Anki's restored). Run with
// --test-page preferences, on a copy whose addons21 has the "Edit LaTeX
// toolchain" add-on.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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
const rollover = async () => (await query("select cast(val as text) from config where key = 'rollover'"))[0][0];
const night = () => document.documentElement.classList.contains("night-mode");
function set(selector, value) {
    const el = document.querySelector(selector);
    el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
}

await until("the page", () => document.querySelector("#theme"));
const store = JSON.parse(sessionStorage.getItem("prefsTest") || "{}");

if (testStage === 0) {
    const settings = await bridge("testSettings");
    check("the add-on's PNG commands were imported", settings.anki_latex_commands?.png?.at(-1)?.[0], "sips");
    check("the page shows them", document.querySelector("#latex-png").value.includes("sips -s format png"), true);

    // dark mode in the open page, then (below) in a newly loaded one
    set("#theme", "dark");
    await until("night mode", night);
    check("the open page turned dark", document.documentElement.dataset.bsTheme, "dark");
    check("the theme is saved", (await bridge("testSettings")).theme, "dark");

    // a collection preference
    const before = await rollover();
    const changed = before === "5" ? "6" : "5";
    set("#rollover", changed);
    await until("the rollover saved", async () => (await rollover()) === changed);
    check("rollover saved in the collection", await rollover(), changed);

    // what's typed just before closing is saved by the flush the shell
    // runs first; an emptied box keeps the saved value
    set("#rollover", "");
    set("#rollover", changed === "5" ? "7" : "8");
    await window.shellFlush();
    check("flushed before closing", await rollover(), changed === "5" ? "7" : "8");
    set("#rollover", "");
    await window.shellFlush();
    check("an empty box isn't saved", await rollover(), changed === "5" ? "7" : "8");
    // an out-of-range hour is clamped, as Qt's spin box
    set("#rollover", "300");
    await window.shellFlush();
    check("rollover clamped", await rollover(), "23");

    // LaTeX: a bad line is refused and nothing changes
    set("#latex-png", "pdflatex tmp.tex\nprintf \"unclosed");
    await until("the error", () => document.querySelector("#latex-error"));
    check("bad quotes are named", document.querySelector("#latex-error").textContent, "line 2: unmatched quote");
    check("the commands kept", (await bridge("testSettings")).latex_commands, null);

    // custom commands are what renders
    set("#latex-png", "sh -c 'printf shellpng > tmp.png'");
    set("#latex-svg", "sh -c 'test -s tmp.tex && printf shellsvg > tmp.svg'");
    await until("the commands saved", async () => (await bridge("testSettings")).latex_commands?.svg);
    check("the commands saved", (await bridge("testSettings")).latex_commands, {
        png: [["sh", "-c", "printf shellpng > tmp.png"]],
        svg: [["sh", "-c", "test -s tmp.tex && printf shellsvg > tmp.svg"]],
    });
    const rendered = await bridge(`testRenderLatex:[$]shell ${Date.now()}[/$]`);
    check("no render errors", rendered.errors, []);
    check("the custom commands made the image", rendered.image, rendered.svg ? "shellsvg" : "shellpng");

    // back to the old Anki's
    document.querySelector("section:has(#latex-png) button").click();
    await until("the reset", async () => (await bridge("testSettings")).latex_commands === null);
    await until("the old commands shown", () => document.querySelector("#latex-png").value.includes("sips"));
    check("the old Anki's commands are back", (await bridge("testSettings")).anki_latex_commands?.svg?.length, 4);

    sessionStorage.setItem("prefsTest", JSON.stringify({ rollover: before, checks, errors }));
    location.reload();
    return { next: true };
}

// a page loaded while dark is served dark
checks.push(...store.checks);
errors.push(...store.errors);
check("a new page loads dark", [night(), document.documentElement.dataset.bsTheme], [true, "dark"]);
check("the body gets the card classes", document.body.classList.contains("nightMode"), true);

set("#theme", "light");
await until("light mode", () => !night());
check("back to light", (await bridge("testSettings")).theme, "light");
set("#rollover", store.rollover);
await until("the rollover restored", async () => (await rollover()) === store.rollover);

return { ok: checks.every((c) => c.ok) && errors.length === 0, checks: checks.filter((c) => !c.ok), errors };
