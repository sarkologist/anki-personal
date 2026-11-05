
(() => {
    const fields = [...document.querySelectorAll(".rich-text-editable")];
    let mathjax = 0, rendered = 0;
    for (const f of fields) {
        const editable = f.shadowRoot?.querySelector("anki-editable");
        for (const m of editable?.querySelectorAll("anki-mathjax") ?? []) {
            mathjax++;
            const span = m.querySelector("span[data-anki=mathjax]");
            if ((span?.shadowRoot ?? span)?.querySelector("svg")) rendered++;
        }
    }
    const text = fields.map((f) => f.shadowRoot?.querySelector("anki-editable")?.textContent?.length ?? 0);
    return `probe fields=${fields.length} textLens=${text.join("/")} mathjax=${mathjax} rendered=${rendered} errors=${(window.__errors || []).length}`;
})()
