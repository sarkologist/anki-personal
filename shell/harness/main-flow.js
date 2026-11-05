// The review flow in the real main window, from the deck browser to the
// congrats screen: run with `--test shell/harness/main-flow.js --test-page main`.
// The shell adds a deck of four notes (Basic, type-in, sound + TTS, LaTeX) and makes
// it current. This script runs again on each page the main window loads;
// `testStage` counts them.

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const until = async (what, fn, ms = 60000) => {
    const end = Date.now() + ms;
    for (;;) {
        const v = await fn();
        if (v) { return v; }
        if (Date.now() > end) { throw new Error("timed out waiting for " + what); }
        await sleep(50);
    }
};
// a shortcut; keys typed into a text field (the type-answer box) aren't
// shortcuts, so leave it first
const key = (k, code = "") => {
    document.activeElement?.blur?.();
    window.dispatchEvent(new KeyboardEvent("keydown", { key: k, code, bubbles: true, cancelable: true }));
};
const query = async (sql) => JSON.parse(JSON.stringify(await bridge("testQuery:" + sql)));
const revlogs = async () =>
    (await query(`select count() from revlog where cid in (select id from cards where did = ${testDeck})`))[0][0];
const path = location.pathname;
log(`stage ${testStage}: ${path}`);

if (path === "/deck-browser" && testStage === 0) {
    const link = await until(
        "the test deck",
        () =>
            [...document.querySelectorAll("button.deck-name")].find((e) =>
                e.textContent.trim().startsWith("shell test")
            ),
    );
    const rows = document.querySelectorAll("tr").length;
    if (!document.querySelector("body").textContent.match(/Studied|studied/)) {
        return { ok: false, error: "no studied-today line" };
    }
    log(`deck browser rows: ${rows}`);
    link.click();
    return { next: true, rows };
}

if (path === "/deck-overview" && testStage === 1) {
    const study = await until(
        "Study Now",
        () => [...document.querySelectorAll("button")].find((b) => /study now/i.test(b.textContent)),
    ).catch((e) => {
        throw new Error(e.message + ": " + document.body.innerText.slice(0, 400));
    });
    const text = document.body.textContent;
    log("overview: " + document.body.innerText.replace(/\s+/g, " ").slice(0, 300));
    if (!text.includes("shell test")) {
        return { ok: false, error: "overview doesn't name the deck", text };
    }
    // Space starts reviewing (the shell's overview shortcut)
    key(" ", "Space");
    return { next: true, study: !!study };
}

if (path === "/_anki/legacyPageData") {
    const results = {};
    const qa = () => document.getElementById("qa");
    const bottom = () => document.getElementById("_shellBottom").contentDocument;
    await until("a question", () => qa().textContent.includes("shell"));
    await until("the answer button", () => bottom()?.getElementById("ansbut"));
    await until(
        "the toolbar",
        () => document.getElementById("_shellToolbar").contentDocument?.body?.textContent.match(/Decks/),
    );
    results.toolbarHeight = document.getElementById("_shellToolbar").offsetHeight;
    results.bottomHeight = document.getElementById("_shellBottom").offsetHeight;
    // what WebKit's <audio> plays
    const a = new Audio();
    results.canPlay = Object.fromEntries(
        [
            "audio/mpeg",
            "audio/mp4",
            "audio/wav",
            "audio/ogg; codecs=vorbis",
            "audio/ogg; codecs=opus",
            "audio/webm",
            "audio/flac",
            "audio/aac",
        ]
            .map((t) => [t, a.canPlayType(t)]),
    );
    results.tts = !!window.speechSynthesis;
    results.voices = window.speechSynthesis ? speechSynthesis.getVoices().length : 0;

    log("checks: " + JSON.stringify(results));
    // mark with "*": the star shows
    key("*");
    await until("the mark", () => !document.getElementById("_mark").hidden);
    key("*");
    await until("no mark", () => document.getElementById("_mark").hidden);

    // the bottom bar says which side is showing (templates may lack <hr id=answer>)
    const onQuestion = () => !!bottom().getElementById("ansbut");
    const onAnswer = () => bottom().querySelectorAll("button[data-ease]").length === 4;
    const text = () => qa().innerText.trim();
    const seen = [];
    let undone = false;
    // the bottom bar changes before reviewer.js has swapped the card text
    let shown = "";
    for (let i = 0; i < 8; i++) {
        await until("a question", () => onQuestion() && text() !== shown);
        const q = text();
        log("question: " + q.slice(0, 40));
        if (q.includes("shell sound")) {
            // a sound and a TTS tag, on the question side, played in turn
            results.playButtons = qa().querySelectorAll(".replay-button").length;
            await until("the sound and TTS to play", () => !_shellAv.busy());
            results.audioLog = _shellAv.log.slice();
        }
        if (q.includes("shell latex")) {
            const img = qa().querySelector("img[src^='latex-']");
            results.latex = img ? img.getAttribute("src") : null;
            results.latexLoaded = img
                ? await until("the LaTeX image", () => img.complete && img.naturalWidth > 0)
                : false;
        }
        const typeans = document.getElementById("typeans");
        if (typeans) {
            typeans.value = "Pairs";
        }
        key(" ", "Space");
        await until("the answer", () => onAnswer() && text() !== q);
        shown = text();
        if (typeans) {
            results.typedComparison = !!qa().querySelector(".typeBad, .typeMissed, .typeGood");
        }
        const before = await revlogs();
        key("k");
        await until("the answer to be saved", async () => (await revlogs()) === before + 1);
        if (!undone) {
            // undo with "u": the same card comes back, on its question
            await until("the next question", () => onQuestion() && text() !== shown);
            shown = text();
            key("u");
            await until("the undo", async () => (await revlogs()) === before);
            await until("the card again", () => onQuestion() && text() === q);
            results.undo = true;
            undone = true;
            continue;
        }
        if (!seen.includes(q)) {
            seen.push(q);
        }
        // Good on a new card leaves it in learning, so it comes back; stop
        // once each card has been answered
        if (seen.length === 4) {
            break;
        }
    }
    results.seen = seen;
    results.revlogs = await revlogs();
    const failed = [
        !results.undo && "undo",
        !results.typedComparison && "typed answer comparison",
        !(results.playButtons >= 2) && "play buttons for the sound and TTS",
        seen.length !== 4 && "each card answered",
        results.revlogs < 4 && "four answers saved",
        !results.audioLog?.includes("ended:shell-test.wav") && "the sound played in <audio>",
        !results.audioLog?.some((e) => e.startsWith("tts-")) && "the TTS tag spoken",
        !(results.latex && results.latexLoaded) && "the LaTeX image",
        !(results.toolbarHeight > 20 && results.bottomHeight > 30) && "toolbar and bottom bar sizes",
    ].filter(Boolean);
    if (failed.length) {
        return { ok: false, failed, ...results };
    }
    // the windows the reviewer opens: card info, options, stats, the editor
    await until("the next question", () => onQuestion());
    for (const k of ["i", "o", "t", "e"]) {
        key(k);
    }
    const wanted = ["card-info", "deck-options", "editor", "stats"];
    results.windows = await until("the windows", async () => {
        const open = await bridge("testWindows");
        return wanted.every((w) => open.includes(w)) && open;
    });
    // back to the decks with "d"
    key("d");
    return { next: true, ...results };
}

if (path === "/deck-browser") {
    return { ok: true, stage: testStage };
}

return { ok: false, error: `unexpected page ${path} at stage ${testStage}` };
