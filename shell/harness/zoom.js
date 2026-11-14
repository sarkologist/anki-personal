// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// View > Zoom In/Out/Reset Zoom on the main window. Run with --test-page
// main, with "main_window_zoom": 1.3 in the copy's settings file.
// Checks: the saved zoom applies when the window opens; the menu items
// step it by 0.1 and save it; it stays as the window's pages change (the
// overview, the reviewer); Reset Zoom goes back to 1.

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
// the page's zoom: its width in CSS pixels shrinks as it grows
const zoom = async () => {
    const { width } = await bridge("testWindowLook:main");
    return Math.round((width / window.innerWidth) * 100) / 100;
};
const saved = async () => (await bridge("testSettings")).main_window_zoom;
const choose = async (id, expected) => {
    await bridge(`testMenuEvent:${id}`);
    await until(`zoom ${expected}`, async () => (await zoom()) === expected, 5000).catch(() => {});
};
const path = location.pathname;
log(`stage ${testStage}: ${path}`);

const state = (await bridge("testStash:")) ?? { checks: [] };
checks.unshift(...state.checks);
const next = async (go) => {
    await bridge(`testStash:${JSON.stringify({ checks })}`);
    setTimeout(go, 200);
    return { next: true };
};

if (path === "/deck-browser" && testStage === 0) {
    await until("the deck list", () => document.querySelector("button.deck-name"));
    check("the saved zoom applies at startup", await zoom(), 1.3);
    await choose("zoomIn", 1.4);
    check("Zoom In", [await zoom(), await saved()], [1.4, 1.4]);
    await choose("zoomOut", 1.3);
    await choose("zoomOut", 1.2);
    check("Zoom Out twice", [await zoom(), await saved()], [1.2, 1.2]);
    const deck = [...document.querySelectorAll("button.deck-name")].find((e) =>
        e.textContent.trim().startsWith("shell test")
    );
    return next(() => deck.click());
}

if (path === "/deck-overview" && testStage === 1) {
    check("the overview keeps it", await zoom(), 1.2);
    const study = await until(
        "Study Now",
        () => [...document.querySelectorAll("button")].find((b) => /study now/i.test(b.textContent)),
    );
    return next(() => study.click());
}

if (path === "/_anki/legacyPageData" && testStage === 2) {
    await until("a question", () => document.getElementById("qa")?.textContent.includes("shell"));
    check("the reviewer keeps it", await zoom(), 1.2);
    await choose("resetZoom", 1);
    check("Reset Zoom", [await zoom(), await saved()], [1, 1]);
    return { ok: checks.every((c) => c.ok), checks: checks.filter((c) => !c.ok) };
}

return { ok: false, error: `unexpected page ${path} at stage ${testStage}` };
