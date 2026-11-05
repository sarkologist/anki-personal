// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

/* eslint
@typescript-eslint/triple-slash-reference: "off",
 */
/// <reference path="shadow-dom.d.ts" />

type BoundaryPoint = [Node, number];

function composedRanges(selection: Selection, roots: ShadowRoot[]): StaticRange[] {
    try {
        return selection.getComposedRanges!({ shadowRoots: roots });
    } catch {
        // Safari 17.x only accepts getComposedRanges(...shadowRoots)
        return selection.getComposedRanges!(...roots);
    }
}

function isInside(range: StaticRange, root: ShadowRoot): boolean {
    return range.startContainer.getRootNode() === root
        && range.endContainer.getRootNode() === root;
}

/**
 * The last selection seen inside a shadow root. WebKit empties the document
 * selection while focus moves, before blur fires (when the editor saves the
 * caret), where Chromium's ShadowRoot.getSelection still has it. It is kept
 * only until the next selectionchange event, which comes after the blur
 * handlers.
 */
let lastInside: { root: ShadowRoot; range: StaticRange } | null = null;

function remember(root: ShadowRoot, ranges: StaticRange[]): void {
    const range = ranges[ranges.length - 1];

    if (!range) {
        // an empty selection may be a focus change in progress
        return;
    } else if (isInside(range, root)) {
        lastInside = { root, range };
    } else if (lastInside?.root === root) {
        lastInside = null;
    }
}

function nodeLength(node: Node): number {
    return node.nodeType === Node.TEXT_NODE || node.nodeType === Node.COMMENT_NODE
        ? (node as CharacterData).length
        : node.childNodes.length;
}

/** The remembered range for root, if it still fits the document. */
function remembered(root: ShadowRoot): StaticRange | null {
    if (lastInside?.root !== root) {
        return null;
    }

    const { startContainer, startOffset, endContainer, endOffset } = lastInside.range;
    const fits = startContainer.isConnected
        && endContainer.isConnected
        && startOffset <= nodeLength(startContainer)
        && endOffset <= nodeLength(endContainer);

    return fits ? lastInside.range : null;
}

/** The open shadow roots along the focus path, outermost first. */
function focusedRoots(): ShadowRoot[] {
    const roots: ShadowRoot[] = [];
    let element = document.activeElement;

    while (element?.shadowRoot) {
        roots.push(element.shadowRoot);
        element = element.shadowRoot.activeElement;
    }

    return roots;
}

function onSelectionChange(): void {
    const selection = document.getSelection();

    if (!selection?.getComposedRanges) {
        return;
    } else if (selection.rangeCount === 0) {
        // the blur handlers have run by now
        lastInside = null;
        return;
    }

    for (const root of focusedRoots()) {
        remember(root, composedRanges(selection, [root]));
    }
}

let trackingSelection = false;

function trackSelection(): void {
    if (!trackingSelection) {
        trackingSelection = true;
        document.addEventListener("selectionchange", onSelectionChange);
    }
}

/**
 * The document selection as seen from inside a shadow root.
 * WebKit retargets the document selection's anchor, focus and ranges to the
 * shadow host; only getComposedRanges reveals the endpoints inside the root.
 * Writes go to the document selection: collapse, extend and setBaseAndExtent
 * accept nodes in shadow roots, but addRange ignores such ranges.
 */
class ShadowRootSelection implements Selection {
    constructor(
        private readonly selection: Selection,
        private readonly root: ShadowRoot,
    ) {}

    private forget(): void {
        if (lastInside?.root === this.root) {
            lastInside = null;
        }
    }

    private composedRanges(): StaticRange[] {
        const ranges = composedRanges(this.selection, [this.root]);
        remember(this.root, ranges);

        if (ranges.length > 0) {
            return ranges;
        }

        const last = remembered(this.root);
        return last ? [last] : [];
    }

    /** [anchor, focus] of the main range */
    private boundaryPoints(): [BoundaryPoint, BoundaryPoint] | null {
        const ranges = this.composedRanges();
        const range = ranges[ranges.length - 1];

        if (!range) {
            return null;
        }

        const start: BoundaryPoint = [range.startContainer, range.startOffset];
        const end: BoundaryPoint = [range.endContainer, range.endOffset];

        return this.direction === "backward" ? [end, start] : [start, end];
    }

    get anchorNode(): Node | null {
        return this.boundaryPoints()?.[0][0] ?? null;
    }

    get anchorOffset(): number {
        return this.boundaryPoints()?.[0][1] ?? 0;
    }

    get focusNode(): Node | null {
        return this.boundaryPoints()?.[1][0] ?? null;
    }

    get focusOffset(): number {
        return this.boundaryPoints()?.[1][1] ?? 0;
    }

    get rangeCount(): number {
        return this.composedRanges().length;
    }

    get isCollapsed(): boolean {
        return getRange(this)?.collapsed ?? true;
    }

    get type(): string {
        if (this.rangeCount === 0) {
            return "None";
        }
        return this.isCollapsed ? "Caret" : "Range";
    }

    get direction(): string {
        return this.selection.direction ?? "none";
    }

    /**
     * Unlike Selection.getRangeAt, the range is a copy: changing it does not
     * change the selection.
     */
    getRangeAt(index: number): Range {
        const staticRange = this.composedRanges()[index];

        if (!staticRange) {
            throw new DOMException(`Range index ${index} out of bounds`, "IndexSizeError");
        }

        const range = new Range();
        range.setStart(staticRange.startContainer, staticRange.startOffset);
        range.setEnd(staticRange.endContainer, staticRange.endOffset);
        return range;
    }

    /**
     * The document selection would never contain nodes in a shadow root
     */
    containsNode(node: Node, allowPartialContainment = false): boolean {
        const range = getRange(this);

        if (!range || range.startContainer.getRootNode() !== node.getRootNode()) {
            return false;
        } else if (allowPartialContainment) {
            return range.intersectsNode(node);
        }

        const nodeRange = new Range();
        nodeRange.selectNodeContents(node);

        return range.compareBoundaryPoints(Range.START_TO_START, nodeRange) <= 0
            && range.compareBoundaryPoints(Range.END_TO_END, nodeRange) >= 0;
    }

    toString(): string {
        return getRange(this)?.toString() ?? "";
    }

    addRange(range: Range): void {
        if (range.startContainer.getRootNode() === document) {
            this.selection.addRange(range);
        } else if (this.selection.rangeCount === 0) {
            this.selection.setBaseAndExtent(
                range.startContainer,
                range.startOffset,
                range.endContainer,
                range.endOffset,
            );
        }
    }

    removeRange(range: Range): void {
        this.forget();
        this.selection.removeRange(range);
    }

    removeAllRanges(): void {
        this.forget();
        this.selection.removeAllRanges();
    }

    empty(): void {
        this.forget();
        this.selection.empty();
    }

    collapse(node: Node | null, offset?: number): void {
        this.selection.collapse(node, offset);
    }

    setPosition(node: Node | null, offset?: number): void {
        this.selection.setPosition(node, offset);
    }

    collapseToStart(): void {
        this.selection.collapseToStart();
    }

    collapseToEnd(): void {
        this.selection.collapseToEnd();
    }

    extend(node: Node, offset?: number): void {
        this.selection.extend(node, offset);
    }

    setBaseAndExtent(
        anchorNode: Node,
        anchorOffset: number,
        focusNode: Node,
        focusOffset: number,
    ): void {
        this.selection.setBaseAndExtent(anchorNode, anchorOffset, focusNode, focusOffset);
    }

    selectAllChildren(node: Node): void {
        this.selection.selectAllChildren(node);
    }

    deleteFromDocument(): void {
        this.selection.deleteFromDocument();
    }

    modify(alter?: string, direction?: string, granularity?: string): void {
        this.selection.modify(alter, direction, granularity);
    }
}

/**
 * Gecko has no .getSelection on ShadowRoot, only .activeElement.
 * Neither has WebKit, see ShadowRootSelection.
 */
export function getSelection(element: Node): Selection | null {
    const root = element.getRootNode();

    if (root.getSelection) {
        return root.getSelection();
    }

    const selection = document.getSelection();

    if (selection?.getComposedRanges && root instanceof ShadowRoot) {
        trackSelection();
        return new ShadowRootSelection(selection, root);
    }

    return selection;
}

/**
 * Browser has potential support for multiple ranges per selection built in,
 * but in reality only Gecko supports it.
 * If there are multiple ranges, the latest range is the _main_ one.
 */
export function getRange(selection: Selection): Range | null {
    const rangeCount = selection.rangeCount;

    return rangeCount === 0 ? null : selection.getRangeAt(rangeCount - 1);
}

/**
 * Avoid using selection.isCollapsed: it will always return
 * true in shadow root in Gecko
 * (this bug seems to also happens in Blink)
 */
export function isSelectionCollapsed(selection: Selection): boolean {
    return getRange(selection)!.collapsed;
}
