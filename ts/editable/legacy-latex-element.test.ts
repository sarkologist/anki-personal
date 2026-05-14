// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { describe, expect, test } from "vitest";

import { undecorateFragment } from "../editor/decorated-elements";
import { fragmentToStored } from "../editor/rich-text-input/transform";
import { LegacyLatex } from "./legacy-latex-element.svelte";

describe("LegacyLatex", () => {
    test("converts legacy inline and display delimiters to editor elements", () => {
        expect(
            LegacyLatex.toUndecorated("a [$]x^2[/$] b [$$]y[/$$] c"),
        ).toBe(
            "a <anki-latex data-latex-kind=\"inline\">x^2</anki-latex> b <anki-latex data-latex-kind=\"display\">y</anki-latex> c",
        );
    });

    test("converts editor elements back to legacy delimiters", () => {
        expect(
            LegacyLatex.toStored(
                "a <anki-latex data-latex-kind=\"inline\">x^2</anki-latex> b "
                    + "<anki-latex data-latex-kind=\"display\">y</anki-latex> c",
            ),
        ).toBe("a [$]x^2[/$] b [$$]y[/$$] c");
    });

    test("leaves full legacy latex blocks undecorated", () => {
        expect(LegacyLatex.toUndecorated("[latex]x[/latex]")).toBe(
            "[latex]x[/latex]",
        );
    });

    test("preserves html inside legacy latex delimiters", () => {
        const undecorated = LegacyLatex.toUndecorated("[$]<b>x</b>[/$]");

        expect(undecorated).toBe(
            "<anki-latex data-latex-kind=\"inline\"><b>x</b></anki-latex>",
        );
        expect(LegacyLatex.toStored(undecorated)).toBe("[$]<b>x</b>[/$]");
    });

    test("stores decorated previews as legacy latex delimiters", () => {
        const fragment = document.createRange().createContextualFragment(
            [
                "<anki-frame data-frames=\"anki-latex\" block=\"false\">",
                "<anki-latex data-latex-kind=\"inline\" data-latex=\"x^2\" decorated>",
                "<img class=\"legacy-latex-preview\" src=\"data:image/png;base64,AAAA\" alt=\"$x^2$\">",
                "</anki-latex>",
                "</anki-frame>",
            ].join(""),
        );

        undecorateFragment(fragment);

        expect(fragmentToStored(fragment)).toBe("[$]x^2[/$]");
    });
});
