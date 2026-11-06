// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Dark mode in WKWebView, through the main window's pages: the theme
// setting and the system's appearance switching an open page live, pages
// loaded while dark being served dark (the deck list, the overview, the
// reviewer and its bars), the card body's nightMode classes, and the
// window's background (Anki's canvas, not white). Run with
// --test-page main; it changes no system setting.

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
        await sleep(50);
    }
};
const checks = [];
function check(name, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    checks.push({ name, ok, actual, expected });
    log(`${ok ? "pass" : "FAIL"} ${name}: ${JSON.stringify(actual)}`);
}
const marks = (doc = document) => [doc.documentElement.classList.contains("night-mode"), doc.documentElement.dataset.bsTheme];
const DARK = [true, "dark"];
const LIGHT = [false, "light"];
const look = () => bridge("testWindowLook:main");
const path = location.pathname;
log(`stage ${testStage}: ${path}`);

// the checks so far, kept across the window's pages
const state = (await bridge("testStash:")) ?? { checks: [] };
checks.unshift(...state.checks);
const next = async (go) => {
    await bridge(`testStash:${JSON.stringify({ checks })}`);
    setTimeout(go, 200);
    return { next: true };
};

if (path === "/deck-browser" && testStage === 0) {
    await until("the deck list", () => document.querySelector("button.deck-name"));
    // the setting, live
    await bridge("testTheme:dark");
    await until("dark", () => marks()[0]);
    check("dark: the open page", marks(), DARK);
    const dark = await look();
    check("dark: the window's canvas", dark.background, [0x2c, 0x2c, 0x2c]);
    check("the webview draws no white background of its own", dark.drawsBackground, false);
    await bridge("testTheme:light");
    await until("light", () => !marks()[0]);
    check("light: the open page", marks(), LIGHT);
    check("light: the window's canvas", (await look()).background, [0xf5, 0xf5, 0xf5]);
    // following the system, which changes while running
    await bridge("testTheme:system");
    await bridge("testSystemAppearance:light");
    await until("the system going light", () => !marks()[0]);
    check("system light", marks(), LIGHT);
    await bridge("testSystemAppearance:dark");
    await until("the system going dark", () => marks()[0]);
    check("system dark", marks(), DARK);
    const deck = [...document.querySelectorAll("button.deck-name")].find((e) => e.textContent.trim().startsWith("shell test"));
    return next(() => deck.click());
}

if (path === "/deck-overview" && testStage === 1) {
    // served dark, not switched after loading
    check("the overview loads dark", marks(), DARK);
    const study = await until("Study Now", () => [...document.querySelectorAll("button")].find((b) => /study now/i.test(b.textContent)));
    return next(() => study.click());
}

if (path === "/_anki/legacyPageData" && testStage === 2) {
    const qa = () => document.getElementById("qa");
    await until("a question", () => qa().textContent.includes("shell"));
    const frame = (id) => document.getElementById(id).contentDocument;
    await until("the bars", () => frame("_shellToolbar")?.body && frame("_shellBottom")?.body);
    check("the reviewer loads dark", marks(), DARK);
    check("its toolbar", marks(frame("_shellToolbar")), DARK);
    check("its bottom bar", marks(frame("_shellBottom")), DARK);
    const body = () => [...document.body.classList].filter((c) => /night/i.test(c)).sort();
    check("the card body", body(), ["nightMode", "night_mode"]);
    // switching while a card shows changes it and its bars
    await bridge("testTheme:light");
    await until("light", () => !marks()[0]);
    check("the card goes light", [marks(), body()], [LIGHT, []]);
    check("its toolbar too", marks(frame("_shellToolbar")), LIGHT);
    await bridge("testTheme:dark");
    await until("dark", () => marks()[0]);
    check("and dark again", body(), ["nightMode", "night_mode"]);
    await bridge("testTheme:system");
    return { ok: checks.every((c) => c.ok), checks: checks.filter((c) => !c.ok) };
}

return { ok: false, error: `unexpected page ${path} at stage ${testStage}` };
