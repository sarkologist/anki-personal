// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// The Alfred bridge add-on (alfred_anki_html_bridge, from the old Anki's
// addons21) in the plugin host, with Add Cards. On a profile copy with the
// add-on in its addons21 and in the settings' "plugins":
//   --test shell/harness/alfred-bridge.js --test-page add
// The add-on remembers the field the user left (`editor_did_unfocus_field`),
// then its local server's /insert pastes there, after the caret, and adds
// the source link; /media stores a file; /health answers; and the note is
// added with all of it.

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
async function type(index, text) {
    const editable = editables()[index];
    editable.focus();
    await frame();
    document.getSelection().selectAllChildren(editable);
    document.execCommand("insertText", false, text);
    await frame();
}

// the add-on's server, from the file it writes when the profile opens
const bridgeFile = await until(
    "the bridge file",
    async () => {
        const text = await bridge("testReadFile:alfred-anki-bridge.json");
        return text && JSON.parse(text);
    },
    30000,
);
const port = Number(new URL(bridgeFile.url).port);
const http = (method, path, body = "") => bridge("testHttp:" + JSON.stringify({ port, method, path, body }));
const token = encodeURIComponent(bridgeFile.token);

await until("the fields", () => editables().length >= 2);
const names = [...document.querySelectorAll(".label-name")].map((l) => l.textContent.trim());
const source = names.findIndex((n) => n.toLowerCase() === "source");
await type(0, "The capital of Italy is {{c1::Rome}}.");
// the field the user was in, before switching to another app
await type(1, "notes: ");
document.getElementById("shell-notetype").focus();
await until("the field to be left", async () => !editables()[1].matches(":focus"));
await sleep(500);

const marker = `alfred-${Date.now() % 100000}`;
const url = `https://example.com/${marker}`;
const inserted = await http(
    "POST",
    `/insert?token=${token}&source_url=${encodeURIComponent(url)}`,
    `<b>${marker}</b>`,
);
const answer = JSON.parse(inserted.body || "{}");
check("/insert", [inserted.status, answer.ok, answer.from_memory, answer.target_field_index], [
    200,
    true,
    true,
    1,
]);
check("the source field was updated", answer.source_updated, source >= 0);
await until("the paste", () => editables()[1].innerHTML.includes(`<b>${marker}</b>`));
check("pasted after the text", editables()[1].innerHTML.startsWith("notes:"), true);
if (source >= 0) {
    check("the source link", editables()[source].innerHTML.includes(url), true);
}
const health = await http("GET", `/health?token=${token}`);
check("/health", [health.status, JSON.parse(health.body).ok], [200, true]);
log("caret: " + health.body);
check("a wrong token", (await http("POST", "/insert?token=nope", "x")).status, 403);

const media = await http("POST", `/media?token=${token}&name=clip.png`, marker);
const stored = JSON.parse(media.body || "{}");
check("/media", [media.status, stored.ok, /^ota-[0-9a-f]+\.png$/.test(stored.filename)], [
    200,
    true,
    true,
]);
check(
    "the media file",
    await bridge(`testReadFile:User 1/collection.media/${stored.filename}`),
    marker,
);

// the window's note has it all: add it
document.getElementById("shell-add").click();
const rows = await until("the added note", async () => {
    const rows = await bridge(`testQuery:select flds from notes where flds like '%${marker}%'`);
    return rows?.length && rows;
});
const fields = rows[0][0].split("\x1f");
check("the added field", fields[1].includes(`<b>${marker}</b>`), true);
if (source >= 0) {
    check("the added source", fields[source].includes(url), true);
}
check("page errors", errors, []);
const failed = checks.filter((c) => !c.ok);
return { ok: failed.length === 0, failed };
