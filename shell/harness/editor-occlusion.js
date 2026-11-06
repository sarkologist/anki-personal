// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Image occlusion from the pasteboard in Add Cards (--test-page add): the
// Image Occlusion notetype, an image file on a scratch pasteboard, then
// the mask editor. Needs a collection with the stock notetype and at
// least one .png in its media folder. Masks aren't drawn: fabric ignores
// synthetic mouse events, so adding stops at Anki's "no occlusions"
// warning.

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
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
const checks = [];
function check(name, actual, expected) {
    const ok = JSON.stringify(actual) === JSON.stringify(expected);
    checks.push({ name, ok, actual, expected });
    log(`${ok ? "pass" : "FAIL"} ${name}: ${JSON.stringify(actual)}`);
}
const query = (sql) => bridge(`testQuery:${sql}`);

const [[io]] = await query(
    "select id from notetypes where name = 'Image Occlusion' order by id limit 1",
);
pycmd("chooseNotetype");
await until("the picker", async () => (await bridge("testDialogs")).length > 0);
await bridge(`testDialog:${JSON.stringify({ button: 0, fields: { id: String(io) } })}`);
await until("the occlusion picker", () => globalThis.editorState === 1);
check("image occlusion picker shown", globalThis.editorState, 1);

// a media file the profile already has, by its absolute path
const png = await bridge("testMediaFile:png");
check("a png to occlude", Boolean(png), true);
await bridge(`testPasteboard:${JSON.stringify({ urls: [`file://${png}`] })}`);
pycmd("addImageForOcclusionFromClipboard");
await until("the mask editor", () => globalThis.canvas && globalThis.editorState === 2);
check("mask editor shows the image", globalThis.editorState, 2);

const count = async () => (await query("select count() from notes"))[0][0];
const before = await count();
pycmd("addNote");
const warning = await until("the warning", async () => (await bridge("testDialogs"))[0]?.[1]);
check("no masks: warned", /occlusion/i.test(warning), true);
await bridge(`testDialog:${JSON.stringify({ button: 0, fields: {} })}`);
check("nothing added", await count(), before);

return { ok: checks.every((c) => c.ok), checks };
