// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The editor agent pane's context tray. On a profile copy with the add-on in
// its addons21 and "plugins": ["editor_agent_pane"] in the settings (no model
// needed):
//   --test shell/harness/context-tray.js --test-page add
// Checks: rendered math in the transcript is tagged with its TeX; a
// selection that ends inside a formula takes it whole; "Add to context" puts
// it in the tray, math as TeX; a comment typed there survives adding the
// editor's selection with Add; removing an item leaves the others.

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

const editables = () =>
    [...document.querySelectorAll(".rich-text-editable")].map((f) => f.shadowRoot?.querySelector("anki-editable"));
await until("the fields", () => editables().length >= 2);
const back = editables()[1];
back.focus();
await frame();
document.getSelection().selectAllChildren(back);
document.execCommand("insertText", false, "The answer is 42.");
await frame();

const side = document.getElementById("shell-side");
const button = await until(
    "the toolbar's Agent button",
    () => document.querySelector("[data-command=editorAgentPane]"),
    30000,
);
button.click();
await until("the pane", () => !side.hidden && side.contentDocument?.querySelector("iframe.qweb"));
const doc = side.contentDocument;
const surface = doc.querySelector("iframe.qweb");
const surfaceId = Number(surface.closest("[data-widget]").dataset.widget);
const inSurface = (js) => side.contentWindow.shellWidgets.evalIn(surfaceId, js);
const comments = () => [...doc.querySelectorAll("input.qlineedit[placeholder='Comment (optional)']")];
const trayLabels = () =>
    comments().map((input) => input.closest("[data-widget]")?.parentElement?.querySelector(".qlabel")?.textContent);
await until(
    "MathJax in the transcript",
    () => inSurface("!!(window.agentPane && window.MathJax && MathJax.startup && MathJax.startup.document)"),
    30000,
);

// a transcript selection, from inside a paragraph into a display formula
const message = "<section class=\"agent-message assistant\"><div class=\"agent-role\">Assistant</div>"
    + "<div class=\"agent-body\"><p>The derivative is \\(f'(x) = 2x\\) here.</p>"
    + "<p>\\[\\int_0^1 x\\,dx\\]</p></div></section>";
await inSurface(`agentPane.appendTranscript(${JSON.stringify(message)}); true`);
check(
    "formulas tagged with their TeX",
    await until(
        "the typeset formulas",
        async () => {
            const tex = await inSurface(
                "[...document.querySelectorAll('mjx-container')].map((m) => m.getAttribute('data-agent-tex'))",
            );
            return tex?.length === 2 && tex.every(Boolean) && tex;
        },
    ),
    ["\\(f'(x) = 2x\\)", "\\[\\int_0^1 x\\,dx\\]"],
);
const selected = await inSurface(`(() => {
    const [p, display] = document.querySelectorAll(".agent-body p");
    const math = display.querySelector("mjx-container");
    const range = document.createRange();
    range.setStart(p.firstChild, 4);
    range.setEnd(math.querySelector("mjx-math") || math, 0);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    return agentPane.selectionText();
})()`);
const quoted = "derivative is \\(f'(x) = 2x\\) here.\n\n\\[\\int_0^1 x\\,dx\\]";
check("the selection, math as TeX", selected, { text: quoted, hasMath: true, role: "assistant" });
await until("the Add to context button", () => inSurface("!document.querySelector('.agent-quote-button').hidden"));
check(
    "the button's label",
    await inSurface("document.querySelector('.agent-quote-button').textContent"),
    "Add to context",
);
await inSurface("document.querySelector('.agent-quote-button').click(); true");
await until("the tray's first item", () => comments().length === 1);
check("the tray shows the reply", trayLabels(), [
    "Reply: \"derivative is \\(f'(x) = 2x\\) here. \\[\\int_0^1 x\\,dx\\]\"",
]);
check("the selection is cleared", await inSurface("getSelection().isCollapsed"), true);
const comment = comments()[0];
comment.value = "why is this 2x?";
comment.dispatchEvent(new Event("input", { bubbles: true }));

// text quoted inside a sent message's attachment isn't the user's own
const sent = "<section class=\"agent-message user\"><div class=\"agent-role\">You</div>"
    + "<div class=\"agent-body\"><div class=\"agent-attachment\"><div class=\"agent-attachment-label\">Reply</div>"
    + "<blockquote class=\"agent-quote\">quoted answer</blockquote></div>my question</div></section>";
await inSurface(`agentPane.appendTranscript(${JSON.stringify(sent)}); true`);
check(
    "a quote in a sent message has no role",
    await inSurface(`(() => {
        const quote = document.querySelector(".agent-message.user .agent-quote");
        getSelection().selectAllChildren(quote);
        const role = agentPane.selectionText().role;
        getSelection().removeAllRanges();
        return role;
    })()`),
    null,
);

// the editor's selection, previewed, then added
back.focus();
await frame();
const text = back.firstChild?.nodeType === Node.TEXT_NODE ? back.firstChild : back.querySelector("*")?.firstChild;
const at = text.textContent.indexOf("42");
document.getSelection().setBaseAndExtent(text, at, text, at + 2);
const labels = () => [...doc.querySelectorAll(".qlabel")].map((l) => l.textContent);
check(
    "the preview of the editor's selection",
    await until("the preview", () => labels().find((l) => l.startsWith("Selected in")), 10000).catch(() => null),
    "Selected in Back: \"42\"",
);
const add = [...doc.querySelectorAll("button.qbutton")].find((b) => b.textContent === "Add");
add.click();
await until("the tray's second item", () => comments().length === 2, 10000).catch(() => null);
check("the tray shows both", trayLabels(), [
    "Reply: \"derivative is \\(f'(x) = 2x\\) here. \\[\\int_0^1 x\\,dx\\]\"",
    "Back: \"42\"",
]);
check("the comment survived the second add", comments().map((c) => c.value), ["why is this 2x?", ""]);

// removing the reply leaves the field's item
const removes = () => [...doc.querySelectorAll(".qtoolbutton")].filter((b) => b.textContent === "×");
removes()[0].click();
await until("one item left", () => comments().length === 1, 10000).catch(() => null);
check("after removing the reply", trayLabels(), ["Back: \"42\""]);

doc.querySelector(".qdock-close").click();
await until("the pane to close", () => side.hidden);
check("page errors", errors, []);
const failed = checks.filter((c) => !c.ok);
return { ok: failed.length === 0, failed };
