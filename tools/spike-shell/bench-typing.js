// Keystroke-to-paint latency in the first rich-text field of the editor.
// Shared by the spike shell and stock Anki (injected via CDP), so both run the
// exact same measurement. Result lands in window.__typingResult.
(async () => {
    const report = (msg) => window.__spikeReport?.(msg);
    const field = document.querySelector(".rich-text-editable");
    const editable = field?.shadowRoot?.querySelector("anki-editable");
    if (!editable) {
        window.__typingResult = { error: "no editable" };
        report("typing error: no editable");
        return;
    }
    editable.focus();
    const sel = editable.getRootNode().getSelection?.() ?? getSelection();
    const range = document.createRange();
    range.selectNodeContents(editable);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);

    const nextPaint = () =>
        new Promise((resolve) =>
            requestAnimationFrame(() => setTimeout(resolve, 0))
        );
    const samples = [];
    const text = "the quick brown fox jumps over the lazy dog ".repeat(3);
    await nextPaint();
    for (const ch of text) {
        const start = performance.now();
        document.execCommand("insertText", false, ch);
        await nextPaint();
        samples.push(performance.now() - start);
    }
    samples.sort((a, b) => a - b);
    const q = (p) => samples[Math.floor(p * (samples.length - 1))].toFixed(1);
    window.__typingResult = {
        n: samples.length,
        median: q(0.5),
        p95: q(0.95),
        max: q(1),
    };
    report(`typing n=${samples.length} median=${q(0.5)}ms p95=${q(0.95)}ms max=${q(1)}ms`);
})();
