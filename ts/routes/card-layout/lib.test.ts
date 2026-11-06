// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { Notetype_Template, Notetype_Template_Config } from "@generated/anki/notetypes_pb";
import { expect, test } from "vitest";

import { clozeCards, flipped, fromBase64, newCardName, newTemplate, reposition, toBase64 } from "./lib";

const template = (name: string, qFormat = "", aFormat = "") =>
    new Notetype_Template({ name, config: new Notetype_Template_Config({ qFormat, aFormat }) });
const cardName = (n: number) => `Card ${n}`;

test("new card types get the first free name and the current sides", () => {
    expect(newCardName([template("Card 1"), template("Card 3")], cardName)).toBe("Card 4");
    expect(newCardName([template("Card 1"), template("Reverse")], cardName)).toBe("Card 3");
    const t = newTemplate("Card 2", template("Card 1", "{{Front}}", "{{Back}}"));
    expect([t.name, t.ord, t.config?.qFormat, t.config?.aFormat]).toEqual([
        "Card 2",
        undefined,
        "{{Front}}",
        "{{Back}}",
    ]);
});

test("flipping needs the answer line", () => {
    const config = new Notetype_Template_Config({
        qFormat: "{{Front}}",
        aFormat: "{{FrontSide}}\n\n<hr id=answer>\n\n{{Back}}",
    });
    expect(flipped(config)).toEqual({
        qFormat: "{{Back}}",
        aFormat: "{{FrontSide}}\n\n<hr id=answer>\n\n{{Front}}",
    });
    expect(flipped(new Notetype_Template_Config({ qFormat: "a", aFormat: "b" }))).toBeNull();
});

test("repositioning clamps to the list", () => {
    const list = [template("a"), template("b"), template("c")];
    expect(reposition(list, 0, 3)).toBe(2);
    expect(list.map((t) => t.name)).toEqual(["b", "c", "a"]);
    expect(reposition(list, 2, 0)).toBe(0);
    expect(list.map((t) => t.name)).toEqual(["a", "b", "c"]);
});

test("cloze cards include the one opened", () => {
    expect(clozeCards([1, 3], 0)).toEqual([1, 3]);
    expect(clozeCards([1, 3], 4)).toEqual([1, 3, 5]);
    expect(clozeCards([], 0)).toEqual([1]);
});

test("base64 round trip", () => {
    const bytes = new Uint8Array([0, 1, 254, 255, 128]);
    expect(fromBase64(toBase64(bytes))).toEqual(bytes);
});
