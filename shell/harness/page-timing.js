// Injected into the reviewer page by the memory bench, in the shell and in
// stock Anki alike: how long each _showQuestion/_showAnswer takes from the
// eval until the card is shown (images preloaded, MathJax typeset), in ms.
(() => {
    if (window._benchLat) { return; }
    window._benchLat = { q: [], a: [] };
    for (const [name, key] of [["_showQuestion", "q"], ["_showAnswer", "a"]]) {
        const orig = window[name];
        window[name] = function(...args) {
            const t0 = performance.now();
            orig.apply(this, args);
            // queued actions run in turn, so this one runs once it's shown
            window._queueAction(() => window._benchLat[key].push(performance.now() - t0));
        };
    }
})();
