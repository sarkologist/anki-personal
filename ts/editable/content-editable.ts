// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { bridgeCommand } from "@tslib/bridgecommand";
import { getSelection } from "@tslib/cross-browser";
import { on, preventDefault } from "@tslib/events";
import { getKillText, setKillText } from "@tslib/kill-ring";
import { isApplePlatform } from "@tslib/platform";
import { registerShortcut } from "@tslib/shortcuts";
import type { Callback } from "@tslib/typing";

import type { SelectionLocation } from "$lib/domlib/location";
import { restoreSelection, saveSelection } from "$lib/domlib/location";
import { placeCaretAfterContent } from "$lib/domlib/place-caret";
import { HandlerList } from "$lib/sveltelib/handler-list";

/**
 * Workaround: If you try to invoke an IME after calling
 * `placeCaretAfterContent` on a cE element, the IME will immediately
 * end and the input character will be duplicated
 */
function safePlaceCaretAfterContent(editable: HTMLElement): void {
    placeCaretAfterContent(editable);
    restoreSelection(editable, saveSelection(editable)!);
}

function restoreCaret(element: HTMLElement, location: SelectionLocation | null): void {
    if (!location) {
        return safePlaceCaretAfterContent(element);
    }

    try {
        restoreSelection(element, location);
    } catch {
        safePlaceCaretAfterContent(element);
    }
}

type SetupFocusHandlerAction = (element: HTMLElement) => { destroy(): void };

export interface FocusHandlerAPI {
    /**
     * Prevent the automatic caret restoration, that happens upon field focus
     */
    flushCaret(): void;
    /**
     * Executed upon focus event of editable.
     */
    focus: HandlerList<{ event: FocusEvent }>;
    /**
     * Executed upon blur event of editable.
     */
    blur: HandlerList<{ event: FocusEvent }>;
}

export function useFocusHandler(): [FocusHandlerAPI, SetupFocusHandlerAction] {
    let latestLocation: SelectionLocation | null = null;
    let offFocus: Callback | null;
    let offPointerDown: Callback | null;
    let flush = false;

    function flushCaret(): void {
        flush = true;
    }

    const focus = new HandlerList<{ event: FocusEvent }>();
    const blur = new HandlerList<{ event: FocusEvent }>();

    function prepareFocusHandling(
        editable: HTMLElement,
        location: SelectionLocation | null = null,
    ): void {
        latestLocation = location;

        offFocus?.();
        offFocus = on(
            editable,
            "focus",
            (event: FocusEvent): void => {
                if (flush) {
                    flush = false;
                } else {
                    restoreCaret(event.currentTarget as HTMLElement, latestLocation);
                }

                focus.dispatch({ event });
            },
            { once: true },
        );

        offPointerDown?.();
        offPointerDown = on(
            editable,
            "pointerdown",
            () => {
                offFocus?.();
                offFocus = null;
            },
            { once: true },
        );
    }

    /**
     * Must execute before DOMMirror.
     */
    function onBlur(this: HTMLElement, event: FocusEvent): void {
        prepareFocusHandling(this, saveSelection(this));
        blur.dispatch({ event });
    }

    function setupFocusHandler(editable: HTMLElement): { destroy(): void } {
        prepareFocusHandling(editable);
        const off = on(editable, "blur", onBlur);

        return {
            destroy() {
                off();
                offFocus?.();
                offPointerDown?.();
            },
        };
    }

    return [
        {
            flushCaret,
            focus,
            blur,
        },
        setupFocusHandler,
    ];
}

if (isApplePlatform()) {
    registerShortcut(() => bridgeCommand("paste"), "Control+Shift+V");
}

export function preventBuiltinShortcuts(editable: HTMLElement): void {
    for (const keyCombination of ["Control+B", "Control+U", "Control+I"]) {
        registerShortcut(preventDefault, keyCombination, { target: editable });
    }
}

declare global {
    interface Selection {
        modify(s: string, t: string, u: string): void;
    }
}

// Fix inverted Ctrl+right/left handling in RTL fields
export function fixRTLKeyboardNav(editable: HTMLElement): void {
    editable.addEventListener("keydown", (evt: KeyboardEvent) => {
        if (window.getComputedStyle(editable).direction === "rtl") {
            const selection = getSelection(editable)!;
            let granularity = "character";
            let alter = "move";
            if (evt.ctrlKey) {
                granularity = "word";
            }
            if (evt.shiftKey) {
                alter = "extend";
            }
            if (evt.code === "ArrowRight") {
                selection.modify(alter, "right", granularity);
                evt.preventDefault();
                return;
            } else if (evt.code === "ArrowLeft") {
                selection.modify(alter, "left", granularity);
                evt.preventDefault();
                return;
            }
        }
    });
}

export function emacsWordNavDirection(code: string): "backward" | "forward" | null {
    switch (code) {
        case "KeyB":
            return "backward";
        case "KeyF":
            return "forward";
        default:
            return null;
    }
}

function killRange(
    editable: HTMLElement,
    direction: "forward" | "backward",
    granularity: "word" | "lineboundary",
    eatLineBreak = false,
): void {
    const selection = getSelection(editable);
    if (!selection) {
        return;
    }

    selection.modify("extend", direction, granularity);
    if (eatLineBreak && selection.isCollapsed) {
        // Ctrl+K at the end of a line removes the line break.
        selection.modify("extend", "forward", "character");
    }

    const text = selection.toString();
    if (!text) {
        return;
    }

    setKillText(text);
    document.execCommand("delete");
}

function yank(): void {
    const text = getKillText();
    if (text) {
        document.execCommand("insertText", false, text);
    }
}

/**
 * macOS provides Emacs/readline-style caret movement (Ctrl+B/F/A/E) in text
 * fields natively. Add the bindings it leaves out: the Alt+B/F word jumps (which
 * otherwise insert ∫/ƒ), and the kill/yank editing commands — Alt+D (kill word
 * forward), Ctrl+K (kill to line end), Ctrl+U (kill to line start), Ctrl+W (kill
 * word back) and Ctrl+Y (yank), backed by a shared kill ring. macOS only.
 */
export function emacsKeyboardShortcuts(editable: HTMLElement): void {
    if (!isApplePlatform()) {
        return;
    }

    editable.addEventListener("keydown", (evt: KeyboardEvent) => {
        // Leave Cmd and Shift combinations to their defaults.
        if (evt.metaKey || evt.shiftKey) {
            return;
        }

        if (evt.altKey && !evt.ctrlKey) {
            if (evt.code === "KeyD") {
                killRange(editable, "forward", "word");
                evt.preventDefault();
                return;
            }

            const direction = emacsWordNavDirection(evt.code);
            if (direction) {
                getSelection(editable)?.modify("move", direction, "word");
                evt.preventDefault();
            }
        } else if (evt.ctrlKey && !evt.altKey) {
            // Ctrl+B/F/A/E navigation is native; only add the kill/yank commands.
            switch (evt.code) {
                case "KeyK":
                    killRange(editable, "forward", "lineboundary", true);
                    break;
                case "KeyU":
                    killRange(editable, "backward", "lineboundary");
                    break;
                case "KeyW":
                    killRange(editable, "backward", "word");
                    break;
                case "KeyY":
                    yank();
                    break;
                default:
                    return;
            }
            evt.preventDefault();
        }
    });
}

/** API */

export interface ContentEditableAPI {
    /**
     * Can be used to turn off the caret restoring functionality of
     * the ContentEditable. Can be used when you want to set the caret
     * yourself.
     */
    focusHandler: FocusHandlerAPI;
}
