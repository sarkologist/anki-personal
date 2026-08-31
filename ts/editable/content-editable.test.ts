// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { afterEach, describe, expect, test, vi } from "vitest";

import { emacsKeyboardShortcuts, emacsWordNavDirection } from "./content-editable";

describe("emacsWordNavDirection", () => {
    test("maps B/F to word movement", () => {
        expect(emacsWordNavDirection("KeyB")).toBe("backward");
        expect(emacsWordNavDirection("KeyF")).toBe("forward");
    });

    test("ignores other keys", () => {
        expect(emacsWordNavDirection("KeyA")).toBeNull();
        expect(emacsWordNavDirection("KeyE")).toBeNull();
        expect(emacsWordNavDirection("ArrowLeft")).toBeNull();
    });
});

describe("emacsKeyboardShortcuts", () => {
    afterEach(() => {
        document.body.replaceChildren();
        Reflect.deleteProperty(window.getSelection()!, "modify");
        Reflect.deleteProperty(document, "execCommand");
        vi.restoreAllMocks();
    });

    test("Control-D deletes the next character on macOS", () => {
        vi.spyOn(window.navigator, "platform", "get").mockReturnValue("MacIntel");

        const editable = document.createElement("div");
        const text = document.createTextNode("ab");
        editable.appendChild(text);
        document.body.appendChild(editable);

        const selection = window.getSelection()!;
        const range = document.createRange();
        range.setStart(text, 0);
        range.collapse(true);
        selection.removeAllRanges();
        selection.addRange(range);
        selection.modify = vi.fn(() => selection.extend(text, 1));
        document.execCommand = vi.fn(() => {
            selection.getRangeAt(0).deleteContents();
            return true;
        });

        emacsKeyboardShortcuts(editable);
        const event = new KeyboardEvent("keydown", {
            code: "KeyD",
            ctrlKey: true,
            cancelable: true,
        });
        editable.dispatchEvent(event);

        expect(event.defaultPrevented).toBe(true);
        expect(editable.textContent).toBe("b");
        expect(selection.modify).toHaveBeenCalledWith(
            "extend",
            "forward",
            "character",
        );
        expect(document.execCommand).toHaveBeenCalledWith("delete");
    });
});
