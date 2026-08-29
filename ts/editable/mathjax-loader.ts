// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

interface EditorMathjax {
    startup?: {
        promise?: Promise<void>;
        typeset?: boolean;
        [name: string]: unknown;
    };
    tex?: {
        packages?: Record<string, unknown>;
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
    const startupPromise = mathjaxWindow.MathJax?.startup?.promise;
    if (isReady()) {
        return startupPromise ?? Promise.resolve();
    }
    if (startupPromise) {
        return startupPromise.then(() => {
            if (!isReady()) {
                throw new Error("MathJax startup completed without tex2svg");
            }
        });
    }
    return null;
}

let mathjaxLoading: Promise<void> | null = null;

function trackLoading(loading: Promise<void>): Promise<void> {
    mathjaxLoading = loading;
    void loading.catch(() => {
        if (mathjaxLoading !== loading) {
            return;
        }
        mathjaxLoading = null;
        if (mathjaxWindow.MathJax?.startup?.promise) {
            delete mathjaxWindow.MathJax.startup.promise;
        }
    });
    return loading;
}

/** Load the editor's synchronous SVG MathJax component once, on first use. */
export function loadMathjax(): Promise<void> {
    if (mathjaxLoading) {
        return mathjaxLoading;
    }
    const existing = existingMathjax();
    if (existing) {
        return trackLoading(existing);
    }

    const configured = mathjaxWindow.MathJax ?? {};
    const disabledPackages = configured.tex?.packages?.["[-]"];
    configured.tex = {
        ...configured.tex,
        packages: {
            ...configured.tex?.packages,
            "[-]": [
                ...(Array.isArray(disabledPackages) ? disabledPackages : []),
                ...(
                    Array.isArray(disabledPackages) && disabledPackages.includes("textmacros")
                        ? []
                        : ["textmacros"]
                ),
            ],
        },
    };
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

    return trackLoading(loading);
}
