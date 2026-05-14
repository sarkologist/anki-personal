// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { describe, expect, test } from "vitest";

import { undecorateFragment } from "../decorated-elements";
import { FieldUndo } from "./field-undo";

describe("FieldUndo", () => {
    test("ignores legacy LaTeX preview redraws after an agent proposal", () => {
        const base = document.createElement("div");
        base.innerHTML = "old";

        const fieldUndo = new FieldUndo(base, undecorateFragment);

        base.innerHTML = [
            "new <anki-frame data-frames=\"anki-latex\" block=\"false\">",
            "<anki-latex data-latex-kind=\"inline\" data-latex=\"x^2\" decorated>",
            "<span class=\"legacy-latex-placeholder\">LaTeX...</span>",
            "</anki-latex>",
            "</anki-frame>",
        ].join("");
        fieldUndo.flush();

        base.querySelector("anki-latex")!.innerHTML =
            "<img class=\"legacy-latex-preview\" src=\"data:image/png;base64,AAAA\" alt=\"$x^2$\">";
        fieldUndo.flush();

        expect(fieldUndo.undo()).toBe(true);
        expect(base.innerHTML).toBe("old");
        expect(fieldUndo.redo()).toBe(true);
        expect(base.innerHTML).toBe(
            "new <anki-latex data-latex-kind=\"inline\">x^2</anki-latex>",
        );

        fieldUndo.destroy();
    });
});
