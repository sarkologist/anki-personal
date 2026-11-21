// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Quoting a transcript selection into the editor agent pane's prompt. On a
// profile copy with the add-on in its addons21 and
// "plugins": ["editor_agent_pane"] in the settings (no model needed):
//   --test shell/harness/transcript-quote.js --test-page add
// Checks: rendered math in the transcript is tagged with its TeX; a
// selection that ends inside a formula takes it whole; the Quote button
// shows, and pressing it puts the selection, math as TeX, in the prompt as
// a "> " quote.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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
await until(
    "MathJax in the transcript",
    () => inSurface("!!(window.agentPane && window.MathJax && MathJax.startup && MathJax.startup.document)"),
    30000,
);

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

// typed in the prompt, then the caret clicked back after "hello" (past an
// astral character, so the caret's UTF-16 units and code points differ)
const prompt = doc.querySelector("textarea.qtextedit[placeholder=Message]");
const head = "\u{1D465} hello";
prompt.focus();
prompt.value = head + " world";
prompt.dispatchEvent(new Event("input", { bubbles: true }));
prompt.setSelectionRange(head.length, head.length);
prompt.dispatchEvent(new MouseEvent("mouseup", { bubbles: true }));

// from inside the first paragraph's text into the display formula
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
check("the selection, math as TeX", selected, { text: quoted, hasMath: true });
await until("the Quote button", () => inSurface("!document.querySelector('.agent-quote-button').hidden"));

const before = prompt.value;
await inSurface("document.querySelector('.agent-quote-button').click(); true");
const quote = "\n\n> derivative is \\(f'(x) = 2x\\) here.\n>\n> \\[\\int_0^1 x\\,dx\\]\n\n";
const expected = head + quote + " world";
check(
    "the prompt has the quote",
    await until("the quote in the prompt", () => prompt.value !== before && prompt.value, 10000).catch(() =>
        prompt.value
    ),
    expected,
);
check("the caret is after the quote", prompt.selectionStart, (head + quote).length);
check("the selection is cleared", await inSurface("getSelection().isCollapsed"), true);

doc.querySelector(".qdock-close").click();
await until("the pane to close", () => side.hidden);
check("page errors", errors, []);
const failed = checks.filter((c) => !c.ok);
return { ok: failed.length === 0, failed };
