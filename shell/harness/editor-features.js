// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Edit Current in WKWebView, past the selection checks of
// editor-selection.js: paste (from a scratch pasteboard, never the user's),
// MathJax and LaTeX overlays, field undo, input method composition, the
// format buttons and the options. Run on a fresh cloze note
// (--test-page editor --test-note 0); results are checked against the
// collection.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const frame = () =>
    Promise.race([
        new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0))),
        new Promise((resolve) => setTimeout(resolve, 50)),
    ]);
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
const editables = () =>
    [...document.querySelectorAll(".rich-text-editable")].map((f) => f.shadowRoot?.querySelector("anki-editable"));
const field = (i) => editables()[i];
async function saved(i) {
    await sleep(900);
    return (await bridge(`testNote:${testNote}`))?.[i];
}
async function setField(i, text) {
    const e = field(i);
    e.focus();
    await frame();
    document.getSelection().selectAllChildren(e);
    if (text) {
        document.execCommand("insertText", false, text);
    } else {
        document.execCommand("delete");
    }
    await frame();
}
function caretAtEnd(e) {
    const sel = document.getSelection();
    sel.selectAllChildren(e);
    sel.collapseToEnd();
}

// 1. Paste: external HTML is filtered (no script/style, p -> div).
const back = field(1);
await setField(1, "");
await bridge(`testPasteboard:${
    JSON.stringify({
        html: "<p>pasted <b>bold</b></p><script>bad()</script><style>b{}</style>",
        text: "pasted bold",
    })
}`);
back.focus();
await frame();
back.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, composed: true }));
await until("the paste", () => back.textContent.includes("pasted"));
check("external paste filtered", /<script|<style|<p>/.test(back.innerHTML), false);
check("external paste keeps bold", /<b>bold<\/b>/.test(back.innerHTML), true);
check("paste saved", /pasted <b>bold<\/b>/.test(await saved(1)), true);

// 2. Paste plain text: escaped, runs of spaces kept.
await setField(1, "");
await bridge(`testPasteboard:${JSON.stringify({ text: "a  <b> c" })}`);
back.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, composed: true }));
await until("the text paste", () => back.textContent.includes("<b>"));
check("text paste escaped", back.textContent.replace(/ /g, " "), "a  <b> c");

// 3. A copy from a field pastes back unfiltered (internal).
back.dispatchEvent(new ClipboardEvent("copy", { bubbles: true, composed: true }));
await bridge(`testPasteboard:${JSON.stringify({ html: "<span style=\"color: red\">red</span><style>i{}</style>" })}`);
await setField(1, "");
back.dispatchEvent(new ClipboardEvent("paste", { bubbles: true, cancelable: true, composed: true }));
await until("the internal paste", () => back.textContent.includes("red"));
check("internal paste keeps styles", /color: red/.test(back.innerHTML), true);

// 4. Field undo. WebKit coalesces typing into one undo step until an undo
// or redo; a pause, leaving the field or moving the caret doesn't end it.
// So one undo takes back " typed" and may take more: logged, not checked.
await setField(1, "undo base");
await saved(1);
field(0).focus();
await frame();
back.focus();
caretAtEnd(back);
document.execCommand("insertText", false, " typed");
await frame();
document.execCommand("undo");
await frame();
log(`after undo: ${JSON.stringify(back.textContent)}`);
check("undo takes back the typing", back.textContent.includes("typed"), false);
document.execCommand("redo");
await frame();
check("redo", back.textContent, "undo base typed");
document.execCommand("insertText", false, " again");
await frame();
document.execCommand("undo");
await frame();
check("after a redo, undo is one step", back.textContent, "undo base typed");

// 5. An input method's composition, through the WKWebView itself.
await setField(1, "ime:");
caretAtEnd(back);
const composed = [];
back.addEventListener("compositionend", (e) => composed.push(e.data), { once: true });
const sent = await bridge(
    `testIme:${JSON.stringify([["mark", "に"], ["mark", "にほ"], ["mark", "にほん"], ["insert", "日本"]])}`,
);
await until("the composition", () => back.textContent.includes("日本"), 5000).catch(() => null);
check("ime input reached the page", sent, true);
check("ime composed text", back.textContent, "ime:日本");
check("ime compositionend", composed, ["日本"]);
check("ime saved", (await saved(1)).replace(/<[^>]*>/g, ""), "ime:日本");

// 6. MathJax: "MathJax inline" from the equations menu opens the overlay;
// the result renders in the field and saves as \( \).
await setField(1, "math ");
caretAtEnd(back);
[...document.querySelectorAll("button")]
    .find((x) => /Equations/i.test(x.title || x.getAttribute("aria-label") || ""))
    .click();
await frame();
const inline = await until(
    "the MathJax menu",
    () => [...document.querySelectorAll(".dropdown-item, button")].find((x) => /MathJax Inline/i.test(x.textContent)),
);
inline.click();
const mathInput = await until(
    "the MathJax overlay",
    () =>
        document.activeElement?.tagName === "TEXTAREA" && document.querySelector(".CodeMirror")
            ? document.activeElement
            : null,
).catch(() => null);
check("mathjax overlay opens", Boolean(mathInput), true);
if (mathInput) {
    document.execCommand("insertText", false, "x^2");
    await until("the formula in the field", () => back.querySelector("anki-mathjax[data-mathjax='x^2']"));
    const rendered = await until(
        "the rendered formula",
        () => /<svg|<mjx|<img/.test(back.querySelector("[data-anki='mathjax']")?.shadowRoot?.innerHTML ?? ""),
    ).catch(() => false);
    check("mathjax rendered in the field", rendered, true);
    field(0).focus();
    await frame();
    check("mathjax saved", /\\\(x\^2\\\)/.test(await saved(1)), true);
}

// 7. LaTeX preview (the legacy overlay's renderer) through the bridge.
const preview = await new Promise((resolve) => {
    const prev = globalThis.receiveLatexPreviewResult;
    globalThis.receiveLatexPreviewResult = (r) => {
        globalThis.receiveLatexPreviewResult = prev;
        resolve(r);
    };
    pycmd(`renderLatexPreview:${JSON.stringify({ requestId: "t1", kind: "inline", html: "x^2" })}`, (r) => {
        if (r?.status === "ready") {
            globalThis.receiveLatexPreviewResult = prev;
            resolve(r.result);
        }
    });
    setTimeout(() => resolve({ timeout: true }), 15000);
});
check(
    "latex preview",
    preview.ok === true
        ? preview.src.startsWith("data:image/") || /^latex-/.test(preview.src)
        : preview.errorText ?? preview,
    true,
);

// 8. Format buttons in Edit Current too.
await setField(1, "list item");
document.getSelection().selectAllChildren(back);
document.querySelector("[data-command='format:insertUnorderedList']").click();
await until("the list", () => back.querySelector("ul li"));
check("list saved", /<ul><li>(<[^>]+>)*list item/.test(await saved(1)), true);

// 9. Options: close-HTML-tags toggles and is kept in the collection.
const before = (await bridge("testQuery:select val from config where key = 'closeHTMLTags'"))?.[0]?.[0];
await bridge("toggleCloseHTMLTags");
const after = (await bridge("testQuery:select val from config where key = 'closeHTMLTags'"))?.[0]?.[0];
await bridge("toggleCloseHTMLTags");
check("closeHTMLTags toggled", before !== after, true);

// 10. Frames: an add-on's side panel loads; a media document (which would
// share the editor's bridge) and another window's page don't.
await bridge("testSidePanel:<p id=panel>side panel</p>");
const side = document.getElementById("shell-side");
const panelText = await until("the side panel", () => side.contentDocument?.getElementById("panel")?.textContent).catch(
    () => null,
);
check("side panel shown", [side.hidden, panelText], [false, "side panel"]);
// its splitter resizes it, and the width is kept
const sideSplit = document.getElementById("shell-side-split");
const splitAt = sideSplit.getBoundingClientRect();
const pointer = (type, x) =>
    sideSplit.dispatchEvent(
        new PointerEvent(type, {
            pointerId: 1,
            isPrimary: true,
            clientX: x,
            clientY: splitAt.top + 50,
            bubbles: true,
        }),
    );
const wantWidth = Math.round(side.getBoundingClientRect().width) + 100;
pointer("pointerdown", splitAt.left + 3);
pointer("pointermove", innerWidth - wantWidth);
pointer("pointerup", innerWidth - wantWidth);
check("side panel resized", Math.round(side.getBoundingClientRect().width), wantWidth);
const keptWidth = await until("the kept width", async () => {
    const w = (await bridge("testSettings"))?.editor?.sideWidth;
    return w === wantWidth && w;
}).catch(() => null);
check("side panel width kept", keptWidth, wantWidth);
const blocked = [];
document.addEventListener("securitypolicyviolation", (e) => blocked.push(e.effectiveDirective));
const mediaFrame = document.createElement("iframe");
mediaFrame.src = "shell-media-page.html";
document.body.appendChild(mediaFrame);
await until("the frame to be refused", () => blocked.includes("frame-src"), 5000).catch(() => null);
check("media document frame refused", blocked.includes("frame-src"), true);
mediaFrame.remove();
const other = await fetch("/_anki/legacyPageData?id=main");
check("another window's page refused", other.status, 404);

await setField(1, "");
await sleep(900);
return { ok: checks.every((c) => c.ok) && errors.length === 0, checks, errors };
