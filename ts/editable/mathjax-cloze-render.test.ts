// @vitest-environment jsdom

// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import "mathjax/es5/tex-svg-full";

import { describe, expect, test } from "vitest";

import { revealMathjaxClozeAnswers } from "./mathjax-cloze";

function rendersWithoutError(tex: string): boolean {
    const out = (globalThis as any).MathJax.tex2svg(tex) as Element;
    return !out.innerHTML.includes("data-mjx-error");
}

describe("card mathjax clozes render", () => {
    test("card cloze as an unbraced font-command argument", () => {
        expect(rendersWithoutError(String.raw`\mathbb {\class{cloze}{[...]}}`)).toBe(
            true,
        );
    });
});

describe("revealed mathjax clozes render", () => {
    // Regression for "Bracket argument to \\ must be a dimension": a cloze on
    // the line after a `\\` used to reveal as `\\[…]`, which MathJax read as an
    // optional row-spacing argument.
    test("cloze on the 2nd line of a \\substack", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`S=\sum_{\substack{x\bmod c\\{{c1::x\bar x\equiv 1\,(c)}}}}e`,
        );
        expect(revealed).not.toContain(String.raw`\\[`);
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze as a subscript (would mis-parse as subscript of `[`)", () => {
        const revealed = revealMathjaxClozeAnswers(String.raw`a_{{c1::x}}`);
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze inside a \\frac numerator", () => {
        const revealed = revealMathjaxClozeAnswers(String.raw`\frac{{{c1::a}}}{b}`);
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    // Regression for "Unsupported use of \hfill": \hfill can't sit inside the
    // wrapping group, so the reveal is split around it.
    test("cloze spanning a \\hfill in \\begin{cases}", () => {
        const revealed = revealMathjaxClozeAnswers(
            String
                .raw`Tw_k = \begin{cases} Tu_j \hfill \text{ if } w_k = u_j\\ {{c1::0 \hfill \text{ if } w_k = v_j}}\end{cases}`,
        );
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze wrapping a whole \\begin{cases} that contains \\hfill", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`{{c1::\begin{cases} a \hfill b\\ c \hfill d\end{cases}}}`,
        );
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze spanning \\hfilll renders once split", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`\begin{cases} a \hfilll b\\ {{c1::c \hfilll d}}\end{cases}`,
        );
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    // Regression for "Misplaced alignment tab character &": a cloze spanning
    // rows of an outer `aligned` used to reveal as one group holding that
    // environment's `&` and `\\`.
    test("cloze spanning rows of an outer aligned", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`\begin{aligned}
\overline{L(s,\chi)}
&={{c1::\overline{\sum_{n=1}^{\infty}\frac{\chi(n)}{n^s} }
=\sum_{n=1}^{\infty}\overline{\chi(n)n^{-s} }\\
&=\sum_{n=1}^{\infty}\overline{\chi(n)}\,\overline{e^{-s\log n} }
=\sum_{n=1}^{\infty}\overline{\chi(n)}\,e^{-\overline s\log n}\\
&=\sum_{n=1}^{\infty}\frac{\overline\chi(n)}{n^{\overline s} } }}
=L(\overline s,\overline\chi).
\end{aligned}`,
        );
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze spanning a row break with a spacing argument", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`\begin{aligned}a&={{c1::b\\[2pt]&=c}}\end{aligned}`,
        );
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze spanning a \\cr row break", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`\begin{aligned}a&={{c1::b\cr c}}\end{aligned}`,
        );
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    // The narrowed wrap leaves fills at the cell's top level too, so an answer
    // holding both a separator and a fill needs no splitting on top.
    test("cloze spanning both an alignment tab and a \\hfill", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`\begin{aligned}a&={{c1::b \hfill c\\&=d}}\end{aligned}`,
        );
        expect(rendersWithoutError(revealed)).toBe(true);
    });

    test("cloze holding a whole nested environment stays grouped", () => {
        const revealed = revealMathjaxClozeAnswers(
            String.raw`M={{c1::\begin{pmatrix}a&b\\c&d\end{pmatrix}}}`,
        );
        expect(revealed).toContain(String.raw`{[\begin{pmatrix}`);
        expect(rendersWithoutError(revealed)).toBe(true);
    });
});
