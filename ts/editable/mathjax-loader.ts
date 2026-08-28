// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

interface EditorMathjax {
    startup?: {
        promise?: Promise<void>;
        typeset?: boolean;
        [name: string]: unknown;
    };
    tex2svg?: (input: string) => Element;
    [name: string]: unknown;
}

type MathjaxWindow = Window & { MathJax?: EditorMathjax };

const mathjaxWindow = window as MathjaxWindow;

function isReady(): boolean {
    return typeof mathjaxWindow.MathJax?.tex2svg === "function";
}

function existingMathjax(): Promise<void> | null {
    if (!isReady()) {
        return null;
    }
    return mathjaxWindow.MathJax?.startup?.promise ?? Promise.resolve();
}

let mathjaxLoading: Promise<void> | null = existingMathjax();

/** Load the editor's synchronous SVG MathJax component once, on first use. */
export function loadMathjax(): Promise<void> {
    if (mathjaxLoading) {
        return mathjaxLoading;
    }
    if ((mathjaxLoading = existingMathjax())) {
        return mathjaxLoading;
    }

    const configured = mathjaxWindow.MathJax ?? {};
    configured.startup = {
        ...configured.startup,
        // The editor explicitly converts individual expressions with tex2svg.
        // Scanning the whole editor page at startup is unnecessary.
        typeset: false,
    };
    mathjaxWindow.MathJax = configured;

    const loading = new Promise<void>((resolve, reject) => {
        const script = document.createElement("script");
        script.src = "/_anki/js/vendor/mathjax/tex-svg-full.js";
        script.onload = async () => {
            try {
                await mathjaxWindow.MathJax?.startup?.promise;
                if (!isReady()) {
                    throw new Error("MathJax loaded without tex2svg");
                }
                resolve();
            } catch (error) {
                script.remove();
                reject(error);
            }
        };
        script.onerror = () => {
            script.remove();
            reject(new Error("Failed to load MathJax"));
        };
        document.head.appendChild(script);
    });

    mathjaxLoading = loading;
    void loading.catch(() => {
        if (mathjaxLoading === loading) {
            mathjaxLoading = null;
        }
    });
    return loading;
}
