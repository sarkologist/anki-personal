// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

interface QueueCounts {
    newCount: number;
    learningCount: number;
    reviewCount: number;
}

interface TreeCounts {
    newCount: number;
    learnCount: number;
    reviewCount: number;
}

export interface Counts {
    new: number;
    learn: number;
    review: number;
}

/** overview.py's `_table`: the deck list counts include cards that burying
    removed from the queue, so the difference is shown next to each count. */
export function buriedCounts(node: TreeCounts | undefined, queued: QueueCounts): Counts {
    if (!node) {
        return { new: 0, learn: 0, review: 0 };
    }
    return {
        new: node.newCount - queued.newCount,
        learn: node.learnCount - queued.learningCount,
        review: node.reviewCount - queued.reviewCount,
    };
}

export function formatBuried(count: number): string {
    if (!count) {
        return "";
    }
    return count > 0 ? `+${count}` : `${count}`;
}

/** When overview.py shows the congrats screen instead. */
export function isFinished(queued: QueueCounts): boolean {
    return !queued.newCount && !queued.learningCount && !queued.reviewCount;
}

/** A standalone document for the deck description, which is user HTML: it is
    shown in a sandboxed iframe without scripts, and this policy forbids them
    as well. `css` carries the page's theme. */
export function descriptionDocument(html: string, css: string): string {
    const safeCss = css.replace(/</g, "\\3c ");
    return `<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="script-src 'none'; object-src 'none'">
<style>
html, body { margin: 0; padding: 0; background: transparent; }
body { padding: 1em; white-space: pre-wrap; overflow-wrap: break-word; }
img { max-width: 100%; }
${safeCss}
</style>
</head>
<body>${html}</body>
</html>`;
}
