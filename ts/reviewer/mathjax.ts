// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

type MathJaxWindow = Window & {
    MathJax?: { startup?: { promise?: Promise<void> } };
};

const mathjaxWindow = window as MathJaxWindow;
let mathjaxLoading: Promise<void> | null = mathjaxWindow.MathJax?.startup?.promise ?? null;

export function lazyLoadMathJax(): Promise<void> {
    return mathjaxLoading || (mathjaxLoading = new Promise((resolve, reject) => {
        const configScript = document.createElement("script");
        configScript.src = "/_anki/js/mathjax.js";
        configScript.onload = () => {
            const mathjaxScript = document.createElement("script");
            mathjaxScript.src = "/_anki/js/vendor/mathjax/tex-chtml-full.js";
            mathjaxScript.onload = () => resolve();
            mathjaxScript.onerror = () => reject(new Error("Failed to load MathJax"));
            document.head.appendChild(mathjaxScript);
        };
        configScript.onerror = () => reject(new Error("Failed to load MathJax config"));
        document.head.appendChild(configScript);
    }));
}

export function isMathJaxLoading(): boolean {
    return mathjaxLoading !== null;
}

// follows mathjaxBlockDelimiterPattern and mathjaxInlineDelimiterPattern
const mathjaxRegex = /<anki-mathjax(?:\s|>)|\\\[(.*?)\\\]|\\\((.*?)\\\)/isu;

export function containsMathjax(html: string): boolean {
    return mathjaxRegex.test(html);
}

function trimBreaks(text: string): string {
    return text.replace(/^\n*/, "").replace(/\n*$/, "");
}

function mathjaxText(element: HTMLElement): string {
    if (typeof element.dataset.mathjax === "string") {
        return trimBreaks(element.dataset.mathjax);
    }

    const clone = element.cloneNode(true) as HTMLElement;
    for (const br of clone.querySelectorAll("br")) {
        br.replaceWith("\n");
    }

    return trimBreaks(clone.textContent ?? "");
}

function delimiterFor(element: HTMLElement): ["\\(" | "\\[", "\\)" | "\\]"] {
    const block = element.getAttribute("block");
    return typeof block === "string" && block !== "false" ? ["\\[", "\\]"] : ["\\(", "\\)"];
}

export function replaceEditorMathjaxElements(parent: ParentNode): void {
    for (const element of parent.querySelectorAll<HTMLElement>("anki-mathjax")) {
        const [open, close] = delimiterFor(element);
        element.replaceWith(document.createTextNode(`${open}${mathjaxText(element)}${close}`));
    }
}
