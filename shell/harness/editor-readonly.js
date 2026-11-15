// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// A read-only field's stored value never changes through the editor. Run
// with --test-page editor --test-note 0.
// Checks: a note load that makes the focused field read-only (Ctrl+N/P
// keep the field) takes the caret out of its hidden inputs, and the blur
// that follows saves its HTML as loaded (entities and all), not as the DOM
// serializes it.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const checks = [];
function check(name, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    checks.push({ name, ok, actual, expected });
    log(`${ok ? "pass" : "FAIL"} ${name}: ${JSON.stringify(actual)}`);
}
// as Frontier stores its JSON: html.escape(json.dumps(...))
const STORED = "{&quot;k&quot;: &#x27;v&#x27;}";
const input = (i) =>
    document.querySelectorAll(".rich-text-editable")[i]?.shadowRoot?.querySelector(
        "anki-editable",
    );
const focused = (el) => el?.getRootNode().activeElement === el;
// the caret in field 1 of a note where it's editable, as before Ctrl+N
setFields([["Text", "text"], ["Back Extra", "editable"]]);
setReadOnly([false, false]);
focusField(1);
await sleep(300);
check("field 1 has the caret first", focused(input(1)), true);
await bridge(`testUpdateNote:${JSON.stringify({ nid: testNote, fields: ["text", STORED] })}`);
// the next note, as `editor.rs` loads it: field 1 is read-only there and
// keeps the focus (focusTo)
setFields([["Text", "text"], ["Back Extra", STORED]]);
setReadOnly([false, true]);
focusField(1);
await sleep(300);
check("the read-only field's input has no focus", focused(input(1)), false);
// leave it, then wait out the debounced saves
focusField(0);
await sleep(300);
document.activeElement?.blur();
await sleep(1500);
check("its stored value is unchanged", (await bridge(`testNote:${testNote}`))?.[1], STORED);
return { ok: checks.every((c) => c.ok), checks: checks.filter((c) => !c.ok) };
