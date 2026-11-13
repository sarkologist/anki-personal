// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { expect, test } from "vitest";

import { readOnlyText } from "./read-only-field";

test("escaped single-line JSON is decoded and pretty-printed", () => {
    expect(readOnlyText("{&quot;a&quot;: [1, &quot;肥 &amp; 胖&quot;]}")).toBe(
        "{\n  \"a\": [\n    1,\n    \"肥 & 胖\"\n  ]\n}",
    );
});

test("non-JSON content is shown as its text", () => {
    expect(readOnlyText("a &lt;b&gt;<br>c")).toBe("a <b>c");
});

test("markup in the field is never rendered", () => {
    expect(readOnlyText("<img src=x onerror=alert(1)>\"x\"")).toBe("\"x\"");
});
