// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Selection-driven editor features in the real editor page, in WKWebView.
// Run by the shell's test channel (see shell/src/testchan.rs) on a fresh
// Cloze note whose first field is "alpha beta gamma"; results are checked
// against the note as saved in the collection.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
// the next paint, or a timer where a hidden page (locked screen) gets none
const frame = () =>
    Promise.race([
        new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0))),
        new Promise((resolve) => setTimeout(resolve, 50)),
    ]);

const errors = [];
window.addEventListener("error", (e) => errors.push(String(e.message)));
window.addEventListener("unhandledrejection", (e) => errors.push(String(e.reason)));

const fields = [...document.querySelectorAll(".rich-text-editable")];
const editable = fields[0]?.shadowRoot?.querySelector("anki-editable");
const other = fields[1]?.shadowRoot?.querySelector("anki-editable");
if (!editable || !other) {
    return { ok: false, error: "editor fields not found" };
}
const root = editable.getRootNode();
const selection = document.getSelection();

/** The text node and offset at a character offset of the field's text. */
function point(offset) {
    const walker = document.createTreeWalker(editable, NodeFilter.SHOW_TEXT);
    let node;
    let last = null;
    while ((node = walker.nextNode())) {
        last = node;
        if (offset <= node.length) {
            return [node, offset];
        }
        offset -= node.length;
    }
    return [last, last?.length ?? 0];
}

function select(start, end = start) {
    const [a, ao] = point(start);
    const [b, bo] = point(end);
    selection.setBaseAndExtent(a, ao, b, bo);
}

/**
 * The field's live HTML. WebKit leaves a trailing placeholder <br> after
 * formatting the end of a field; it isn't saved (see the last check).
 */
function html() {
    return editable.innerHTML.replace(/<br>$/, "");
}

function button(pattern) {
    return [...document.querySelectorAll("button")].find((b) =>
        pattern.test(b.title || b.getAttribute("aria-label") || "")
    );
}

async function savedField() {
    // field saves are debounced by 600ms
    await sleep(900);
    const saved = await bridge(`testNote:${testNote}`);
    return saved?.[0];
}

const checks = [];
function check(name, actual, expected) {
    const ok = actual === expected;
    checks.push({ name, ok, actual, expected });
    log(`${ok ? "pass" : "FAIL"} ${name}: ${JSON.stringify(actual)}`);
}

// What WebKit offers, for the record.
const webkit = {
    rootHasGetSelection: typeof root.getSelection === "function",
    getComposedRanges: typeof selection.getComposedRanges === "function",
    direction: "direction" in selection,
};

editable.focus();
await frame();
check("starts with the seeded text", editable.textContent, "alpha beta gamma");

// 1. The raw WebKit behaviour the shim relies on.
select(6);
await frame();
try {
    const [range] = selection.getComposedRanges({ shadowRoots: [root] });
    webkit.composedDictionaryForm = range?.startContainer === point(6)[0] && range?.startOffset === 6;
} catch (e) {
    webkit.composedDictionaryForm = String(e);
}
webkit.anchorRetargeted = selection.anchorNode !== point(6)[0];

// 2. Typing at a caret placed inside the field.
document.execCommand("insertText", false, "X");
await frame();
check("insert at caret", editable.textContent, "alpha Xbeta gamma");

// 3. The caret survives a blur and refocus (saveSelection/restoreSelection).
select(2);
await frame();
other.focus();
await frame();
editable.focus();
await frame();
document.execCommand("insertText", false, "Y");
await frame();
check("caret restored after refocus", editable.textContent, "alYpha Xbeta gamma");

// 4. Bold, through the surrounder.
select(8, 12);
await frame();
const bold = button(/^Bold/);
check("bold button found", !!bold, true);
bold?.click();
await frame();
check("bold wraps the selection", html(), "alYpha X<b>beta</b> gamma");

// 5. A backward selection, then cloze wrap.
const len = editable.textContent.length;
select(len, len - 5);
await frame();
webkit.backwardDirection = selection.direction;
const cloze = button(/cloze/i);
check("cloze button found", !!cloze, true);
cloze?.click();
await frame();
check("cloze wraps the selection", html(), "alYpha X<b>beta</b> {{c1::gamma}}");

// 6. The edits reached the collection.
other.focus();
check("saved to the collection", await savedField(), "alYpha X<b>beta</b> {{c1::gamma}}");

check("no page errors", errors.join("; "), "");
return { ok: checks.every((c) => c.ok), checks: checks.filter((c) => !c.ok), webkit };
