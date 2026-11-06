// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The editor agent pane add-on in the plugin host, beside the fields of
// Add Cards or Edit Current. On a profile copy with the add-on in its
// addons21, "plugins": ["editor_agent_pane"] in the settings, and the
// add-on's provider set to Ollama with a local model:
//   --test shell/harness/agent-pane.js --test-page add
//   --test shell/harness/agent-pane.js --test-page editor --test-note 0
// Checks: the shortcut and the toolbar button open and close the pane;
// its form; Enter in the prompt sends a request, which the model answers
// in the transcript with a proposal; Apply fills the editor's field, and
// the note is saved (Edit Current) or added with it (Add).

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const frame = () =>
    Promise.race([
        new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0))),
        new Promise((resolve) => setTimeout(resolve, 50)),
    ]);
async function until(what, f, ms = 20000) {
    const end = Date.now() + ms;
    while (Date.now() < end) {
        const v = await f();
        if (v) {
            return v;
        }
        await sleep(100);
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

const add = !!document.getElementById("shell-add");
const editables = () =>
    [...document.querySelectorAll(".rich-text-editable")].map((f) => f.shadowRoot?.querySelector("anki-editable"));
async function type(index, text) {
    const editable = editables()[index];
    editable.focus();
    await frame();
    document.getSelection().selectAllChildren(editable);
    document.execCommand("insertText", false, text);
    await frame();
}

await until("the fields", () => editables().length >= 2);
const fieldNames = [...document.querySelectorAll(".label-name")].map((l) => l.textContent.trim());
const marker = `agent-ok-${Date.now() % 100000}`;
if (add) {
    await type(0, "The capital of France is {{c1::Paris}}.");
}

// the pane: the shortcut opens it, the toolbar button closes and opens it
const side = document.getElementById("shell-side");
const pane = () => side.contentDocument;
const button = await until(
    "the toolbar's Agent button",
    () => document.querySelector("[data-command=editorAgentPane]"),
    30000,
);
document.dispatchEvent(
    new KeyboardEvent("keydown", {
        key: "E",
        code: "KeyE",
        metaKey: true,
        altKey: true,
        shiftKey: true,
        bubbles: true,
    }),
);
await until("the pane, from the shortcut", () => !side.hidden && pane()?.querySelector(".qdock textarea"));
button.click();
await until("the pane to close", () => side.hidden);
button.click();
await until("the pane again", () => !side.hidden);
check("one toolbar button", document.querySelectorAll("[data-command=editorAgentPane]").length, 1);

const doc = pane();
const labels = () => [...doc.querySelectorAll(".qlabel")].map((l) => l.textContent);
const buttons = () => [...doc.querySelectorAll("button.qbutton")];
const buttonNamed = (text) => buttons().find((b) => b.textContent === text);
check(
    "the context line",
    labels()[0].split(" - ")[0],
    add ? "add" : "review",
);
const provider = [...doc.querySelectorAll(".qform select")][0];
check("the provider", provider.selectedOptions[0]?.textContent, "Ollama");
check(
    "the form's rows",
    ["Provider", "Model", "Card access"].every((l) => labels().includes(l)),
    true,
);
check("Send", !!buttonNamed("Send"), true);
const surface = doc.querySelector("iframe.qweb");
const surfaceId = Number(surface.closest("[data-widget]").dataset.widget);
const transcript = () => side.contentWindow.shellWidgets.evalIn(surfaceId, "document.body.innerText");
await until("the transcript's page", async () => typeof (await transcript()) === "string");

// a request, sent with Enter
const prompt = doc.querySelector("textarea.qtextedit[placeholder=Message]");
prompt.focus();
prompt.value = `Set the ${fieldNames[1]} field to exactly: ${marker}. Change nothing else.`;
prompt.dispatchEvent(new Event("input", { bubbles: true }));
prompt.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
await until("the prompt to be sent", () => prompt.value === "");
const proposal = await until(
    "the model's proposal",
    () => {
        const apply = buttonNamed("Apply proposal");
        return apply && !apply.disabled && apply;
    },
    180000,
);
const shown = await transcript();
check("the transcript has the request", shown.includes(marker), true);
log("transcript: " + shown.slice(0, 400).replaceAll("\n", " | "));

// Apply fills the editor's field
proposal.click();
await until("the field to be filled", () => editables()[1]?.innerHTML.includes(marker), 20000);
await until("the transcript to say so", async () => /Applied/.test(await transcript()));
check("the transcript says applied", true, true);
if (add) {
    document.getElementById("shell-add").click();
    const rows = await until(
        "the added note",
        async () => {
            const rows = await query(
                `select id, flds from notes where flds like '%${marker}%'`,
            );
            return rows.length && rows;
        },
        20000,
    );
    check("the added note has both fields", rows[0][1].split("\x1f").slice(0, 2), [
        "The capital of France is {{c1::Paris}}.",
        marker,
    ]);
} else {
    const saved = await until(
        "the saved note",
        async () => {
            const fields = await bridge(`testNote:${testNote}`);
            return fields?.[1]?.includes(marker) && fields;
        },
        20000,
    );
    check("the saved field", saved[1], marker);
}
if (add) {
    // the next note starts a new request
    await until("a fresh transcript", async () => !(await transcript()).includes(marker));
}

// closing the dock
doc.querySelector(".qdock-close").click();
await until("the pane to close", () => side.hidden);
check("page errors", errors, []);
const failed = checks.filter((c) => !c.ok);
return { ok: failed.length === 0, failed };
