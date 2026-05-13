// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export const ACTIVE_CLOZE_HIGHLIGHT_CLASS = "anki-active-cloze-highlight";

const ACTIVE_CLOZE_SELECTOR = ".cloze:not([data-shape])";
const ACTIVE_CLOZE_VIEWPORT_MARGIN = 16;
const ACTIVE_CLOZE_HIGHLIGHT_DURATION_MS = 1200;

let highlightedCloze: HTMLElement | null = null;
let highlightTimeout: number | null = null;

interface LocateActiveClozeOptions {
    root?: ParentNode;
    viewportMargin?: number;
    highlightDurationMs?: number;
}

export function findActiveTextCloze(root: ParentNode = document): HTMLElement | null {
    return Array.from(root.querySelectorAll<HTMLElement>(ACTIVE_CLOZE_SELECTOR))
        .find((element) => !isHidden(element)) ?? null;
}

export function locateActiveCloze({
    root = document,
    viewportMargin = ACTIVE_CLOZE_VIEWPORT_MARGIN,
    highlightDurationMs = ACTIVE_CLOZE_HIGHLIGHT_DURATION_MS,
}: LocateActiveClozeOptions = {}): HTMLElement | null {
    const cloze = findActiveTextCloze(root);
    if (!cloze) {
        return null;
    }

    if (!isWithinViewport(cloze, viewportMargin)) {
        cloze.scrollIntoView({ block: "center", inline: "nearest" });
    }
    highlightCloze(cloze, highlightDurationMs);
    return cloze;
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
    const rect = element.getBoundingClientRect();
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight;
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

function highlightCloze(cloze: HTMLElement, durationMs: number): void {
    clearHighlightedCloze();

    highlightedCloze = cloze;
    cloze.classList.add(ACTIVE_CLOZE_HIGHLIGHT_CLASS);
    highlightTimeout = window.setTimeout(() => {
        if (highlightedCloze === cloze) {
            clearHighlightedCloze();
        }
    }, durationMs);
}

function clearHighlightedCloze(): void {
    if (highlightTimeout !== null) {
        window.clearTimeout(highlightTimeout);
        highlightTimeout = null;
    }
    highlightedCloze?.classList.remove(ACTIVE_CLOZE_HIGHLIGHT_CLASS);
    highlightedCloze = null;
}
