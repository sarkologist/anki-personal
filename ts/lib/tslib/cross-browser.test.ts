// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { restoreSelection, saveSelection } from "../domlib/location/document";
import { getRange, getSelection, isSelectionCollapsed } from "./cross-browser";

type Point = [Node, number];

function indexOf(node: Node): number {
    return Array.prototype.indexOf.call(node.parentNode!.childNodes, node);
}

/** Move a point out of any shadow root, to just before its host. */
function retarget([node, offset]: Point): Point {
    const root = node.getRootNode();
    if (root instanceof ShadowRoot) {
        return retarget([root.host.parentNode!, indexOf(root.host)]);
    }
    return [node, offset];
}

/**
 * Emulates WebKit's document selection (checked in WKWebView): collapse,
 * extend and setBaseAndExtent accept endpoints inside shadow roots, but
 * addRange ignores such ranges, and the getters retarget endpoints to the
 * shadow host. Only getComposedRanges reveals the real endpoints.
 */
class WebKitSelection {
    anchor: Point | null = null;
    focus: Point | null = null;

    /** Safari 17.x: getComposedRanges(...shadowRoots) instead of a dictionary */
    constructor(private variadic = false) {}

    get rangeCount(): number {
        return this.anchor ? 1 : 0;
    }

    get anchorNode(): Node | null {
        return this.anchor && retarget(this.anchor)[0];
    }

    get anchorOffset(): number {
        return this.anchor ? retarget(this.anchor)[1] : 0;
    }

    get focusNode(): Node | null {
        return this.focus && retarget(this.focus)[0];
    }

    get focusOffset(): number {
        return this.focus ? retarget(this.focus)[1] : 0;
    }

    get isCollapsed(): boolean {
        return this.anchorNode === this.focusNode && this.anchorOffset === this.focusOffset;
    }

    get type(): string {
        if (!this.anchor) {
            return "None";
        }
        return this.isCollapsed ? "Caret" : "Range";
    }

    get direction(): string {
        if (!this.anchor || this.isCollapsedInside()) {
            return "none";
        }
        return this.isBackward() ? "backward" : "forward";
    }

    getRangeAt(index: number): Range {
        if (index >= this.rangeCount) {
            throw new DOMException("index out of range", "IndexSizeError");
        }
        const range = new Range();
        range.setStart(...retarget(this.anchor!));
        return range;
    }

    getComposedRanges(...args: unknown[]): StaticRange[] {
        let shadowRoots: ShadowRoot[];
        if (this.variadic) {
            if (!args.every((arg) => arg instanceof ShadowRoot)) {
                throw new TypeError("Argument 1 is not a ShadowRoot");
            }
            shadowRoots = args as ShadowRoot[];
        } else {
            shadowRoots = (args[0] as { shadowRoots?: ShadowRoot[] })?.shadowRoots ?? [];
        }

        if (!this.anchor) {
            return [];
        }

        const rescope = (point: Point): Point =>
            shadowRoots.includes(point[0].getRootNode() as ShadowRoot)
                ? point
                : retarget(point);
        const [start, end] = this.isBackward()
            ? [this.focus!, this.anchor]
            : [this.anchor, this.focus!];
        const [startContainer, startOffset] = rescope(start);
        const [endContainer, endOffset] = rescope(end);

        return [
            new StaticRange({ startContainer, startOffset, endContainer, endOffset }),
        ];
    }

    /** Per spec, nodes outside the document tree are never contained */
    containsNode(_node: Node): boolean {
        return false;
    }

    addRange(range: Range): void {
        if (range.startContainer.getRootNode() !== document) {
            return;
        }
        if (!this.anchor) {
            this.anchor = [range.startContainer, range.startOffset];
            this.focus = [range.endContainer, range.endOffset];
        }
    }

    removeAllRanges(): void {
        this.anchor = this.focus = null;
    }

    empty(): void {
        this.removeAllRanges();
    }

    collapse(node: Node, offset = 0): void {
        this.anchor = this.focus = [node, offset];
    }

    extend(node: Node, offset = 0): void {
        this.focus = [node, offset];
    }

    setBaseAndExtent(
        anchorNode: Node,
        anchorOffset: number,
        focusNode: Node,
        focusOffset: number,
    ): void {
        this.anchor = [anchorNode, anchorOffset];
        this.focus = [focusNode, focusOffset];
    }

    private isCollapsedInside(): boolean {
        return this.anchor![0] === this.focus![0] && this.anchor![1] === this.focus![1];
    }

    private isBackward(): boolean {
        const range = new Range();
        range.setStart(...this.anchor!);
        return range.comparePoint(...this.focus!) < 0;
    }
}

function setup() {
    document.body.innerHTML = "<p>before</p>";
    const host = document.createElement("div");
    document.body.append(host);
    const root = host.attachShadow({ mode: "open" });
    const editable = document.createElement("div");
    editable.innerHTML = "<b>bold</b> plain";
    root.append(editable);

    const bold = editable.firstChild!.firstChild as Text;
    const plain = editable.lastChild as Text;

    return { host, root, editable, bold, plain };
}

function emulateWebKit(variadic = false): WebKitSelection {
    const selection = new WebKitSelection(variadic);
    vi.spyOn(document, "getSelection").mockReturnValue(
        selection as unknown as Selection,
    );
    return selection;
}

let fixture: ReturnType<typeof setup>;

beforeEach(() => {
    fixture = setup();
});

afterEach(() => {
    vi.restoreAllMocks();
    document.body.innerHTML = "";
});

describe("getSelection in a shadow root (WebKit)", () => {
    test("reads a caret inside the shadow root", () => {
        const webkit = emulateWebKit();
        const { editable, plain } = fixture;

        const selection = getSelection(editable)!;
        // set after getSelection: the adapter must not cache state
        webkit.collapse(plain, 2);

        expect(selection.rangeCount).toBe(1);
        expect(selection.anchorNode).toBe(plain);
        expect(selection.anchorOffset).toBe(2);
        expect(selection.focusNode).toBe(plain);
        expect(selection.focusOffset).toBe(2);
        expect(selection.isCollapsed).toBe(true);
        expect(selection.type).toBe("Caret");
        expect(isSelectionCollapsed(selection)).toBe(true);
    });

    test("reads a backward selection", () => {
        const webkit = emulateWebKit();
        const { editable, bold, plain } = fixture;
        webkit.setBaseAndExtent(plain, 3, bold, 1);

        const selection = getSelection(editable)!;

        expect(selection.anchorNode).toBe(plain);
        expect(selection.anchorOffset).toBe(3);
        expect(selection.focusNode).toBe(bold);
        expect(selection.focusOffset).toBe(1);
        expect(selection.isCollapsed).toBe(false);
        expect(selection.type).toBe("Range");
        expect(selection.toString()).toBe("old pl");
        expect(isSelectionCollapsed(selection)).toBe(false);
    });

    test("reports no selection", () => {
        emulateWebKit();

        const selection = getSelection(fixture.editable)!;

        expect(selection.rangeCount).toBe(0);
        expect(selection.anchorNode).toBeNull();
        expect(selection.type).toBe("None");
        expect(getRange(selection)).toBeNull();
    });

    test("getRangeAt returns a live Range with the inner endpoints", () => {
        const webkit = emulateWebKit();
        const { editable, bold, plain } = fixture;
        webkit.setBaseAndExtent(bold, 1, plain, 3);

        const selection = getSelection(editable)!;
        const range = getRange(selection)!;

        expect(range).toBeInstanceOf(Range);
        expect(range.startContainer).toBe(bold);
        expect(range.startOffset).toBe(1);
        expect(range.endContainer).toBe(plain);
        expect(range.endOffset).toBe(3);
        expect(() => selection.getRangeAt(1)).toThrow();
    });

    test("reflects writes immediately", () => {
        emulateWebKit();
        const { editable, bold, plain } = fixture;
        const selection = getSelection(editable)!;

        const range = new Range();
        range.selectNodeContents(bold);
        selection.removeAllRanges();
        selection.addRange(range);

        expect(selection.anchorNode).toBe(bold);
        expect(selection.anchorOffset).toBe(0);
        expect(selection.focusOffset).toBe(4);
        expect(selection.toString()).toBe("bold");

        selection.setBaseAndExtent(plain, 1, plain, 4);

        expect(selection.anchorNode).toBe(plain);
        expect(selection.anchorOffset).toBe(1);
        expect(selection.focusOffset).toBe(4);

        selection.extend(bold, 2);

        expect(selection.anchorNode).toBe(plain);
        expect(selection.focusNode).toBe(bold);
        expect(selection.focusOffset).toBe(2);

        selection.empty();

        expect(selection.rangeCount).toBe(0);
    });

    test("containsNode looks inside the shadow root", () => {
        const webkit = emulateWebKit();
        const { editable, bold, plain } = fixture;
        const selection = getSelection(editable)!;

        webkit.setBaseAndExtent(editable, 0, editable, 1);
        expect(selection.containsNode(bold.parentNode!)).toBe(true);
        expect(selection.containsNode(plain)).toBe(false);

        webkit.setBaseAndExtent(bold, 1, plain, 2);
        expect(selection.containsNode(plain)).toBe(false);
        expect(selection.containsNode(plain, true)).toBe(true);
    });

    test("falls back to the variadic getComposedRanges of Safari 17.x", () => {
        const webkit = emulateWebKit(true);
        const { editable, bold, plain } = fixture;
        webkit.setBaseAndExtent(plain, 3, bold, 1);

        const selection = getSelection(editable)!;

        expect(selection.anchorNode).toBe(plain);
        expect(selection.anchorOffset).toBe(3);
        expect(selection.focusNode).toBe(bold);
        expect(selection.focusOffset).toBe(1);
    });

    test("saveSelection/restoreSelection round-trips a caret", () => {
        const webkit = emulateWebKit();
        const { editable, plain } = fixture;
        webkit.collapse(plain, 3);

        const location = saveSelection(editable)!;
        expect(location).toEqual({
            anchor: { coordinates: [1], offset: 3 },
            collapsed: true,
        });

        webkit.collapse(document.body, 0);
        restoreSelection(editable, location);

        expect(webkit.anchor).toEqual([plain, 3]);
        expect(webkit.focus).toEqual([plain, 3]);
    });

    test("saveSelection/restoreSelection round-trips a backward selection", () => {
        const webkit = emulateWebKit();
        const { editable, bold, plain } = fixture;
        webkit.setBaseAndExtent(plain, 3, bold, 1);

        const location = saveSelection(editable)!;
        expect(location).toMatchObject({ collapsed: false, direction: "backward" });

        webkit.removeAllRanges();
        restoreSelection(editable, location);

        expect(webkit.anchor).toEqual([plain, 3]);
        expect(webkit.focus).toEqual([bold, 1]);
    });
});

describe("selection while focus moves (WebKit)", () => {
    // WebKit empties the document selection before it fires blur, which is
    // when the editor saves the caret; ShadowRoot.getSelection in Chromium
    // still has it then.
    test("keeps the last selection inside the root while the selection is empty", () => {
        const webkit = emulateWebKit();
        const { editable, plain } = fixture;
        editable.tabIndex = 0;
        editable.focus();
        // as the editor does on focus; this starts tracking selection changes
        getSelection(editable);
        webkit.collapse(plain, 3);
        document.dispatchEvent(new Event("selectionchange"));

        webkit.removeAllRanges();

        expect(saveSelection(editable)).toEqual({
            anchor: { coordinates: [1], offset: 3 },
            collapsed: true,
        });
    });

    test("forgets it once the selection moves out of the root", () => {
        const webkit = emulateWebKit();
        const { editable, plain } = fixture;
        const selection = getSelection(editable)!;
        webkit.collapse(plain, 3);
        expect(selection.anchorNode).toBe(plain);

        webkit.collapse(document.body.firstChild!.firstChild!, 1);
        expect(selection.anchorNode).not.toBe(plain);
        webkit.removeAllRanges();

        expect(selection.rangeCount).toBe(0);
        expect(saveSelection(editable)).toBeNull();
    });

    test("forgets it once selectionchange reports the selection empty", () => {
        const webkit = emulateWebKit();
        const { editable, plain } = fixture;
        const selection = getSelection(editable)!;
        webkit.collapse(plain, 3);
        expect(selection.anchorNode).toBe(plain);
        webkit.removeAllRanges();
        // still there for the blur handlers that run before the event
        expect(selection.rangeCount).toBe(1);

        document.dispatchEvent(new Event("selectionchange"));

        expect(selection.rangeCount).toBe(0);
        expect(saveSelection(editable)).toBeNull();
    });

    test("drops a remembered range the text no longer fits", () => {
        const webkit = emulateWebKit();
        const { editable, plain } = fixture;
        const selection = getSelection(editable)!;
        webkit.collapse(plain, 5);
        expect(selection.rangeCount).toBe(1);
        webkit.removeAllRanges();

        plain.data = "x";

        expect(selection.rangeCount).toBe(0);
        expect(() => getRange(selection)).not.toThrow();
    });

    test("forgets it when cleared through the adapter", () => {
        const webkit = emulateWebKit();
        const { editable, plain } = fixture;
        const selection = getSelection(editable)!;
        webkit.collapse(plain, 3);
        expect(selection.rangeCount).toBe(1);

        selection.removeAllRanges();

        expect(selection.rangeCount).toBe(0);
    });
});

describe("getSelection elsewhere", () => {
    test("uses ShadowRoot.getSelection when present (Chromium)", () => {
        emulateWebKit();
        const { root, editable } = fixture;
        const chromium = {} as Selection;
        Object.assign(root, { getSelection: () => chromium });

        expect(getSelection(editable)).toBe(chromium);
    });

    test("uses the document selection without getComposedRanges (Gecko)", () => {
        expect(getSelection(fixture.editable)).toBe(document.getSelection());
    });

    test("uses the document selection outside shadow roots", () => {
        const webkit = emulateWebKit();

        expect(getSelection(document.body.firstChild!)).toBe(webkit);
    });
});
