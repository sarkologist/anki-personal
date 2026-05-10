// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export type HighlightClass = {
    /** CSS class applied to the <span> wrapping the selected text. */
    className: string;
    /** Short text shown inside the toolbar chip, and used for the tooltip. */
    label: string;
    /**
     * Color used for the chip preview in the toolbar popover. Independent of
     * the actual rendered color, which is controlled by the .hl-N rules in the
     * editor stylesheet (ts/editable/editable-base.scss) and your card-template
     * CSS. Edit this if you want chips to match.
     */
    swatch: string;
    /** Chord-style keyboard shortcut that applies this highlight class. */
    keyCombination: string;
};

export const highlightClasses: HighlightClass[] = [
    { className: "hl-1", label: "1", swatch: "#d40000", keyCombination: "Control+Shift+H, 1" },
    { className: "hl-2", label: "2", swatch: "#4ead1b", keyCombination: "Control+Shift+H, 2" },
    { className: "hl-3", label: "3", swatch: "#d5b60a", keyCombination: "Control+Shift+H, 3" },
];

const classNameSet = new Set(highlightClasses.map((c) => c.className));

export function findHighlightClass(element: Element): string | undefined {
    for (const cls of element.classList) {
        if (classNameSet.has(cls)) {
            return cls;
        }
    }
    return undefined;
}

export function isHighlightClass(name: string): boolean {
    return classNameSet.has(name);
}
