// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The editor agent pane's queue window (Tools > Agent Queue) as an add-on
// window. On a profile copy with the add-on in its addons21, its
// agent_queue.sqlite holding open requests (and none queued, so nothing
// runs), and "plugins": ["editor_agent_pane"] in the settings:
//   --test shell/harness/agent-queue.js --test-page main
// Checks: the Tools item opens the window with the store's open requests
// in its table and the first one selected; picking a row moves the
// selection and the detail; the Defer button's menu, and its date dialog;
// Discard's reason prompt (cancelled, so nothing changes); Escape closes
// the window, and the item opens it again.

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
        await sleep(150);
    }
};
const checks = [];
function check(name, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    checks.push({ name, ok, actual, expected });
    log(`${ok ? "pass" : "FAIL"} ${name}: ${JSON.stringify(actual)}`);
}
const windows = async () => (await bridge("testWindows")).filter((l) => l.startsWith("addon-window-"));
// `js` (an expression, maybe a promise) in a window, and its JSON value
const inWindow = async (label, js) => {
    await bridge("testStash:null");
    await bridge(
        `testEval:${label}\nPromise.resolve((() => ${js})()).then((v) => pycmd("testStash:" + JSON.stringify(v ?? null)))`,
    );
    return until(`an answer from ${label}`, async () => {
        const v = await bridge("testStash");
        return v === null ? undefined : { v };
    }).then((r) => r.v);
};
// for what may close the window, so it can't answer
const fire = (label, js) => bridge(`testEval:${label}\n${js}`);
const key = (label, k) =>
    fire(
        label,
        `(document.activeElement || document.body).dispatchEvent(new KeyboardEvent("keydown", { key: "${k}", code: "${k}", bubbles: true, cancelable: true }))`,
    );
const clickButton = (label, text) =>
    fire(
        label,
        `[...document.querySelectorAll("button")].find((b) => b.textContent.replace(" ▾", "") === ${
            JSON.stringify(text)
        })?.click()`,
    );
const STATE = `(() => {
    const rows = [...document.querySelectorAll(".qtable tbody tr")];
    return {
        headers: [...document.querySelectorAll(".qtable th")].map((t) => t.textContent),
        rows: rows.map((r) => r.cells[1]?.textContent),
        selected: rows.findIndex((r) => r.classList.contains("selected")),
        enabled: [...document.querySelectorAll("button.qbutton")].filter((b) => !b.disabled && b.checkVisibility()).map((b) => b.textContent.replace(" ▾", "")),
        menu: [...document.querySelectorAll(".qmenu > *")].map((e) => e.textContent || "-"),
    };
})()`;
const detail = (label) =>
    inWindow(
        label,
        `window.shellWidgets.evalIn(Number(document.querySelector(".qweb").closest("[data-widget]").dataset.widget), "document.body.innerText")`,
    );

const items = await until("the add-on's Tools items", async () => {
    const items = await bridge("testAddonMenu");
    return items.length ? items : undefined;
});
const item = items.find((i) => i.text === "Agent Queue");
check("Tools > Agent Queue", !!item, true);
await bridge("testAddonAction:" + item.id);
const queue = await until("the queue window", async () => (await windows())[0]);
let state = await until("the open requests", async () => {
    const s = await inWindow(queue, STATE);
    return s.rows.length && s.selected >= 0 ? s : undefined;
});
check("headers", state.headers, ["", "Request", "Card", "Deck", "Age", "Ver."]);
log(`rows: ${JSON.stringify(state.rows)}`);
check("the first row is selected", state.selected, 0);
log(`enabled: ${JSON.stringify(state.enabled)}`);
check("its actions", ["Discard", "Defer"].every((b) => state.enabled.includes(b)), true);
const first = await until("the first request's detail", async () => (await detail(queue)) || undefined);
log(`detail: ${JSON.stringify(first.slice(0, 120))}`);

if (state.rows.length > 1) {
    await inWindow(queue, `document.querySelectorAll(".qtable tbody tr")[1].cells[1].click() || 1`);
    state = await until("the second row", async () => {
        const s = await inWindow(queue, STATE);
        return s.selected === 1 ? s : undefined;
    });
    check("picking a row selects it", state.selected, 1);
    const second = await until("the second request's detail", async () => {
        const d = await detail(queue);
        return d && d !== first ? d : undefined;
    });
    check("and shows its detail", second !== first, true);
}

// the Defer button's menu; Escape closes it, not the window
await clickButton(queue, "Defer");
state = await until("the Defer menu", async () => {
    const s = await inWindow(queue, STATE);
    return s.menu.length ? s : undefined;
});
log(`defer menu: ${JSON.stringify(state.menu)}`);
check("the menu has the date choice", state.menu.includes("Until a date and time…"), true);
await key(queue, "Escape");
state = await inWindow(queue, STATE);
check("Escape closes the menu", state.menu, []);
check("and not the window", (await windows()).includes(queue), true);

// its date dialog, cancelled
await clickButton(queue, "Defer");
await until("the Defer menu", async () => (await inWindow(queue, STATE)).menu.length);
await inWindow(
    queue,
    `[...document.querySelectorAll(".qmenu-item")].find((b) => b.textContent === "Until a date and time…").click() || 1`,
);
const picker = await until("the date dialog", async () => (await windows()).find((l) => l !== queue));
const pickerState = await until("its fields", async () => {
    const s = await inWindow(
        picker,
        `({ value: document.querySelector("input[type=datetime-local]")?.value, focused: document.activeElement?.className, buttons: [...document.querySelectorAll("button")].map((b) => b.textContent) })`,
    );
    return s.value ? s : undefined;
});
log(`date dialog: ${JSON.stringify(pickerState)}`);
check("date dialog", [pickerState.focused, pickerState.buttons], ["qdatetime", ["Cancel", "OK"]]);
await clickButton(picker, "Cancel");
await until("the date dialog to close", async () => !(await windows()).includes(picker));

// Discard asks why; Escape cancels, so the request stays
const rowsBefore = (await inWindow(queue, STATE)).rows;
await clickButton(queue, "Discard");
const prompt = await until("the reason prompt", async () => (await windows()).find((l) => l !== queue));
const promptState = await until("its field", async () => {
    const s = await inWindow(
        prompt,
        `({ focused: document.activeElement?.className, text: document.querySelector(".qlabel")?.textContent })`,
    );
    return s.focused ? s : undefined;
});
check("reason prompt", [promptState.text, promptState.focused], [
    "Why discard it? (optional, kept with the request)",
    "qlineedit",
]);
await key(prompt, "Escape");
await until("the prompt to close", async () => !(await windows()).includes(prompt));
await sleep(500);
check("cancelled, nothing discarded", (await inWindow(queue, STATE)).rows, rowsBefore);

// Escape closes the window; the Tools item opens it again
await key(queue, "Escape");
await until("the window to close", async () => !(await windows()).includes(queue));
await bridge("testAddonAction:" + item.id);
const again = await until("the window again", async () => (await windows())[0]);
state = await until("its rows", async () => {
    const s = await inWindow(again, STATE);
    return s.rows.length ? s : undefined;
});
check("reopened with the same requests", state.rows, rowsBefore);

return { ok: checks.every((c) => c.ok), checks: checks.filter((c) => !c.ok) };
