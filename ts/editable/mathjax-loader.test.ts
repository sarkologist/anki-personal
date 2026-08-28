// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, expect, test, vi } from "vitest";

import { convertMathjax } from "./mathjax";
import { getCachedMathjaxConversionAfterLoad, resetMathjaxCache } from "./mathjax-cache";

const mathjaxWindow = window as Window & {
    MathJax?: {
        startup?: { promise?: Promise<void>; typeset?: boolean };
        tex?: {
            packages?: Record<string, unknown>;
            [name: string]: unknown;
        };
        tex2svg?: (input: string) => Element;
    };
};

afterEach(() => {
    vi.unstubAllGlobals();
    delete mathjaxWindow.MathJax;
    document.head.replaceChildren();
    resetMathjaxCache();
    vi.resetModules();
});

function mathjaxOutput(): Element {
    const wrapper = document.createElement("span");
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    Object.defineProperty(svg, "viewBox", {
        value: { baseVal: { height: 10 } },
    });
    wrapper.appendChild(svg);
    return wrapper;
}

test("memoizes one script load for concurrent callers", async () => {
    const { loadMathjax } = await import("./mathjax-loader");

    const first = loadMathjax();
    const second = loadMathjax();
    const script = document.head.querySelector("script");

    expect(second).toBe(first);
    expect(document.head.querySelectorAll("script")).toHaveLength(1);
    expect(script?.src.endsWith("/_anki/js/vendor/mathjax/tex-svg-full.js")).toBe(
        true,
    );
    expect(mathjaxWindow.MathJax?.startup?.typeset).toBe(false);

    mathjaxWindow.MathJax = {
        tex2svg: vi.fn(),
        startup: { promise: Promise.resolve(), typeset: false },
    };
    script?.dispatchEvent(new Event("load"));

    await expect(first).resolves.toBeUndefined();
});

test("adopts a pre-existing tex2svg implementation", async () => {
    const startupPromise = Promise.resolve();
    mathjaxWindow.MathJax = {
        tex2svg: vi.fn(),
        startup: { promise: startupPromise },
    };
    const { loadMathjax } = await import("./mathjax-loader");

    expect(loadMathjax()).toBe(startupPromise);
    expect(document.head.querySelector("script")).toBeNull();
});

test("adopts a pending startup promise without injecting another script", async () => {
    let finishStartup!: () => void;
    const startupPromise = new Promise<void>((resolve) => {
        finishStartup = resolve;
    });
    mathjaxWindow.MathJax = { startup: { promise: startupPromise } };
    const { loadMathjax } = await import("./mathjax-loader");

    const loading = loadMathjax();
    expect(document.head.querySelector("script")).toBeNull();

    mathjaxWindow.MathJax.tex2svg = vi.fn();
    finishStartup();
    await expect(loading).resolves.toBeUndefined();
});

test("a rejected adopted startup resets and permits a retry", async () => {
    let rejectStartup!: (error: Error) => void;
    const startupPromise = new Promise<void>((_resolve, reject) => {
        rejectStartup = reject;
    });
    mathjaxWindow.MathJax = { startup: { promise: startupPromise } };
    const { loadMathjax } = await import("./mathjax-loader");

    const first = loadMathjax();
    rejectStartup(new Error("startup failed"));
    await expect(first).rejects.toThrow("startup failed");

    const second = loadMathjax();
    expect(second).not.toBe(first);
    expect(document.head.querySelectorAll("script")).toHaveLength(1);
});

test("rejects adopted startup that completes without tex2svg", async () => {
    mathjaxWindow.MathJax = { startup: { promise: Promise.resolve() } };
    const { loadMathjax } = await import("./mathjax-loader");

    await expect(loadMathjax()).rejects.toThrow(
        "MathJax startup completed without tex2svg",
    );
});

test("adopts MathJax injected after the loader module initializes", async () => {
    const { loadMathjax } = await import("./mathjax-loader");
    mathjaxWindow.MathJax = { tex2svg: vi.fn() };

    await expect(loadMathjax()).resolves.toBeUndefined();
    expect(document.head.querySelector("script")).toBeNull();
});

test("rejects a failed script load and permits a retry", async () => {
    const { loadMathjax } = await import("./mathjax-loader");
    const first = loadMathjax();
    document.head.querySelector("script")?.dispatchEvent(new Event("error"));

    await expect(first).rejects.toThrow("Failed to load MathJax");
    const second = loadMathjax();

    expect(second).not.toBe(first);
    expect(document.head.querySelectorAll("script")).toHaveLength(1);
});

test("sets editor config before loading while preserving existing config", async () => {
    const configuredStartup = { typeset: true };
    mathjaxWindow.MathJax = {
        startup: configuredStartup,
        tex: {
            inlineMath: [["$", "$"]],
            packages: { "[+]": ["custom"], "[-]": ["existing"] },
        },
    };
    const { loadMathjax } = await import("./mathjax-loader");

    void loadMathjax();

    expect(mathjaxWindow.MathJax.startup).toMatchObject({
        typeset: false,
    });
    expect(mathjaxWindow.MathJax.tex).toMatchObject({
        inlineMath: [["$", "$"]],
        packages: {
            "[+]": ["custom"],
            "[-]": ["existing", "textmacros"],
        },
    });
    expect(document.head.querySelector("script")).not.toBeNull();
});

test("converts and caches the real SVG only after loading", async () => {
    let finishLoading!: () => void;
    const loading = new Promise<void>((resolve) => {
        finishLoading = resolve;
    });
    const tex2svg = vi.fn(mathjaxOutput);
    vi.stubGlobal("MathJax", { tex2svg });

    const pending = getCachedMathjaxConversionAfterLoad(
        "x^2",
        20,
        0,
        () => loading,
        () => convertMathjax("x^2", 20),
    );
    expect(tex2svg).not.toHaveBeenCalled();

    finishLoading();
    const [svg] = await pending;

    expect(tex2svg).toHaveBeenCalledOnce();
    expect(svg).toContain("<svg");
    expect(svg).toContain("font-size: 20px");
});
