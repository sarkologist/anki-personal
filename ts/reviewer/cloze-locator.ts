// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export const ACTIVE_CLOZE_HIGHLIGHT_CLASS = "anki-active-cloze-highlight";

const ACTIVE_CLOZE_SELECTOR = ".cloze:not([data-shape])";
const ACTIVE_CLOZE_VIEWPORT_MARGIN = 16;
const ACTIVE_CLOZE_HIGHLIGHT_DURATION_MS = 1200;

let highlightedClozes: HTMLElement[] = [];
let highlightTimeout: number | null = null;

interface LocateActiveClozeOptions {
    root?: ParentNode;
    viewportMargin?: number;
    highlightDurationMs?: number;
}

type ViewportRect = Pick<DOMRect, "top" | "right" | "bottom" | "left" | "width" | "height">;

export function findActiveTextCloze(root: ParentNode = document): HTMLElement | null {
    return findActiveTextClozes(root)[0] ?? null;
}

export function findActiveTextClozes(root: ParentNode = document): HTMLElement[] {
    return Array.from(root.querySelectorAll<HTMLElement>(ACTIVE_CLOZE_SELECTOR))
        .filter((element) => !isHidden(element));
}

export function locateActiveCloze({
    root = document,
    viewportMargin = ACTIVE_CLOZE_VIEWPORT_MARGIN,
    highlightDurationMs = ACTIVE_CLOZE_HIGHLIGHT_DURATION_MS,
}: LocateActiveClozeOptions = {}): HTMLElement | null {
    const clozes = findActiveTextClozes(root);
    const firstCloze = clozes[0];
    if (!firstCloze) {
        return null;
    }

    scrollToClozes(clozes, viewportMargin);
    highlightClozes(clozes, highlightDurationMs);
    return firstCloze;
}

function isHidden(element: HTMLElement): boolean {
    const view = element.ownerDocument.defaultView;
    if (!view) {
        return false;
    }

    for (let current: HTMLElement | null = element; current; current = current.parentElement) {
        const style = view.getComputedStyle(current);
        if (style.display === "none" || style.visibility === "hidden") {
            return true;
        }
    }
    return false;
}

function isWithinViewport(element: HTMLElement, margin: number): boolean {
    return isRectWithinViewport(element.getBoundingClientRect(), margin);
}

function scrollToClozes(clozes: HTMLElement[], margin: number): void {
    if (clozes.length === 1) {
        scrollToFirstClozeIfNeeded(clozes[0], margin);
        return;
    }

    const rect = combinedBoundingRect(clozes);
    if (isRectWithinViewport(rect, margin)) {
        return;
    }

    if (fitsWithinViewport(rect, margin)) {
        scrollRectIntoView(rect, margin);
    } else {
        scrollToFirstClozeIfNeeded(clozes[0], margin);
    }
}

function scrollToFirstClozeIfNeeded(cloze: HTMLElement, margin: number): void {
    if (!isWithinViewport(cloze, margin)) {
        cloze.scrollIntoView({ block: "center", inline: "nearest" });
    }
}

function combinedBoundingRect(elements: HTMLElement[]): ViewportRect {
    const firstRect = elements[0].getBoundingClientRect();
    let top = firstRect.top;
    let right = firstRect.right;
    let bottom = firstRect.bottom;
    let left = firstRect.left;

    for (const element of elements.slice(1)) {
        const rect = element.getBoundingClientRect();
        top = Math.min(top, rect.top);
        right = Math.max(right, rect.right);
        bottom = Math.max(bottom, rect.bottom);
        left = Math.min(left, rect.left);
    }

    return {
        top,
        right,
        bottom,
        left,
        width: right - left,
        height: bottom - top,
    };
}

function isRectWithinViewport(rect: ViewportRect, margin: number): boolean {
    const { width: viewportWidth, height: viewportHeight } = viewportSize();
    const horizontalLimit = viewportWidth - margin;
    const verticalLimit = viewportHeight - margin;
    const fitsHorizontally = rect.width <= viewportWidth - 2 * margin;
    const fitsVertically = rect.height <= viewportHeight - 2 * margin;

    const horizontallyVisible = fitsHorizontally
        ? rect.left >= margin && rect.right <= horizontalLimit
        : rect.right >= margin && rect.left <= horizontalLimit;
    const verticallyVisible = fitsVertically
        ? rect.top >= margin && rect.bottom <= verticalLimit
        : rect.bottom >= margin && rect.top <= verticalLimit;

    return horizontallyVisible && verticallyVisible;
}

function fitsWithinViewport(rect: Pick<DOMRect, "width" | "height">, margin: number): boolean {
    const { width: viewportWidth, height: viewportHeight } = viewportSize();

    return rect.width <= viewportWidth - 2 * margin && rect.height <= viewportHeight - 2 * margin;
}

function scrollRectIntoView(
    rect: Pick<DOMRect, "top" | "bottom" | "left" | "right">,
    margin: number,
): void {
    const { width: viewportWidth, height: viewportHeight } = viewportSize();
    const deltaX = scrollDelta(rect.left, rect.right, margin, viewportWidth - margin);
    const deltaY = scrollDelta(rect.top, rect.bottom, margin, viewportHeight - margin);

    if (deltaX !== 0 || deltaY !== 0) {
        window.scrollBy(deltaX, deltaY);
    }
}

function scrollDelta(start: number, end: number, min: number, max: number): number {
    if (start < min) {
        return start - min;
    }
    if (end > max) {
        return end - max;
    }
    return 0;
}

function viewportSize(): { width: number; height: number } {
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;

    return { width: viewportWidth, height: viewportHeight };
}

function highlightClozes(clozes: HTMLElement[], durationMs: number): void {
    clearHighlightedCloze();

    highlightedClozes = clozes;
    for (const cloze of clozes) {
        cloze.classList.add(ACTIVE_CLOZE_HIGHLIGHT_CLASS);
    }
    highlightTimeout = window.setTimeout(() => {
        if (highlightedClozes === clozes) {
            clearHighlightedCloze();
        }
    }, durationMs);
}

function clearHighlightedCloze(): void {
    if (highlightTimeout !== null) {
        window.clearTimeout(highlightTimeout);
        highlightTimeout = null;
    }
    for (const cloze of highlightedClozes) {
        cloze.classList.remove(ACTIVE_CLOZE_HIGHLIGHT_CLASS);
    }
    highlightedClozes = [];
}
