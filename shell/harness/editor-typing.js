// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

// Keystroke-to-paint latency in the first field of the editor (the spike's
// bench-typing.js). Run with the shell's test channel on a large note:
//   anki-shell --test shell/harness/editor-typing.js --test-page editor --test-note <nid>
// Fails if the 95th percentile is over 50ms (the spike measured 13-14ms in
// WebKit, against 150-380ms stalls in Qt on a 154-MathJax note).

const editable = document.querySelector(".rich-text-editable")?.shadowRoot?.querySelector(
    "anki-editable",
);
if (!editable) {
    return { ok: false, error: "no editable" };
}
editable.focus();
const selection = document.getSelection();
const last = editable.lastChild ?? editable;
selection.collapse(last, last.nodeType === Node.TEXT_NODE ? last.length : last.childNodes.length);

// A hidden page (locked screen, another Space) gets no animation frames,
// and its timers are clamped (an idle setTimeout(0) takes about 100ms).
// Then this measures the keystroke to a forced layout after the page's
// microtasks: scripts, style and layout, without painting.
const hidden = document.hidden;
const nextPaint = hidden
    ? async () => {
        await Promise.resolve();
        editable.getBoundingClientRect();
    }
    : () => new Promise((resolve) => requestAnimationFrame(() => setTimeout(resolve, 0)));
const samples = [];
await nextPaint();
for (const ch of "the quick brown fox jumps over the lazy dog ".repeat(3)) {
    const start = performance.now();
    document.execCommand("insertText", false, ch);
    await nextPaint();
    samples.push(performance.now() - start);
}
samples.sort((a, b) => a - b);
const q = (p) => Number(samples[Math.floor(p * (samples.length - 1))].toFixed(1));
const result = {
    n: samples.length,
    measured: hidden ? "layout (page hidden)" : "paint",
    median: q(0.5),
    p95: q(0.95),
    max: q(1),
};
return { ok: result.p95 < 50, ...result };
