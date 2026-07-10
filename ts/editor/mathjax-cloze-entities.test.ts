// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { describe, expect, test } from "vitest";

import { escapeMathjaxClozeEntities } from "./mathjax-cloze-entities";

describe("escapeMathjaxClozeEntities", () => {
    test("escapes trailing mathjax braces when clozing inside mathjax", () => {
        expect(escapeMathjaxClozeEntities("\\({{c1::\\frac{1}{2}}}\\)")).toBe(
            "\\({{c1::\\frac{1}{2&#125;}}\\)",
        );
    });

    test("escapes nested closing groups inside a mathjax cloze", () => {
        expect(
            escapeMathjaxClozeEntities("\\({{c1::\\sqrt{\\frac{a}{b}}}}\\)"),
        ).toBe("\\({{c1::\\sqrt{\\frac{a}{b&#125;&#125;}}\\)");
    });

    test("leaves enclosing mathjax braces outside an inner cloze", () => {
        expect(
            escapeMathjaxClozeEntities("\\[\\frac{3+4{{c1::\\cos x}}}{y}\\]"),
        ).toBe("\\[\\frac{3+4{{c1::\\cos x}}}{y}\\]");
    });

    test("leaves fraction numerator braces outside clozed terms", () => {
        expect(
            escapeMathjaxClozeEntities(
                "\\[\\frac{3+4\\chi(p)^k{{c1::\\cos(kt\\log p)}}+{{c1::\\cos(2kt\\log p)}}}{kp^{k\\sigma}}\\]",
            ),
        ).toBe(
            "\\[\\frac{3+4\\chi(p)^k{{c1::\\cos(kt\\log p)}}+{{c1::\\cos(2kt\\log p)}}}{kp^{k\\sigma}}\\]",
        );
    });

    test("escapes nested closing groups when a cloze wraps mathjax", () => {
        expect(
            escapeMathjaxClozeEntities("{{c1::\\(\\sqrt{\\frac{a}{b}}\\)}}"),
        ).toBe("{{c1::\\(\\sqrt{\\frac{a}{b&#125;}\\)}}");
    });

    test("escapes escaped literal right braces before the cloze suffix", () => {
        expect(escapeMathjaxClozeEntities("\\({{c1::\\}}}\\)")).toBe(
            "\\({{c1::\\&#125;}}\\)",
        );
    });

    test("escapes multiline block mathjax clozes", () => {
        expect(
            escapeMathjaxClozeEntities(
                "\\[{{c1::\\begin{aligned}\na &= b\\\\\n&= c\n\\end{aligned}}}\\]",
            ),
        ).toBe(
            "\\[{{c1::\\begin{aligned}\na &= b\\\\\n&= c\n\\end{aligned&#125;}}\\]",
        );
    });

    test("leaves non-clozed mathjax unchanged", () => {
        const input = "\\(\\sqrt{\\frac{a}{b}}\\)";
        expect(escapeMathjaxClozeEntities(input)).toBe(input);
    });

    test("is idempotent", () => {
        const input = "{{c1::\\(\\sqrt{\\frac{a}{b&#125;}\\)}}";
        expect(escapeMathjaxClozeEntities(input)).toBe(input);
    });
});

/**
 * Frozen copy of the original character-by-character scanner, used to verify
 * that the sliced-plain-run optimization is byte-equivalent.
 */
function escapeMathjaxClozeEntitiesReference(storedHtml: string): string {
    const rightBraceEntity = "&#125;";
    const rightBraceEntityPattern = /^&#(?:0*125|x0*7d);/iu;

    function mathjaxCloseDelimiterAt(text: string, index: number): "\\)" | "\\]" | null {
        if (text.startsWith("\\(", index)) {
            return "\\)";
        }
        if (text.startsWith("\\[", index)) {
            return "\\]";
        }
        return null;
    }

    function rightBraceEntityAt(text: string, index: number): string | null {
        return text.slice(index).match(rightBraceEntityPattern)?.[0] ?? null;
    }

    function clozeOpenEnd(text: string, index: number): number | null {
        if (!text.startsWith("{{c", index)) {
            return null;
        }
        let numberEnd = index + 3;
        while (numberEnd < text.length && /[\d,]/u.test(text[numberEnd])) {
            numberEnd++;
        }
        if (!text.startsWith("::", numberEnd)) {
            return null;
        }
        const ordinals = text.slice(index + 3, numberEnd);
        if (!ordinals.split(",").some((ordinal) => /^\d+$/u.test(ordinal))) {
            return null;
        }
        return numberEnd + 2;
    }

    function rightBraceRunEnd(text: string, index: number): number {
        let end = index;
        while (text[end] === "}") {
            end++;
        }
        return end;
    }

    let output = "";
    let index = 0;
    const clozeMathjaxBraceDepths: number[] = [];
    let mathjaxCloseDelimiter: "\\)" | "\\]" | null = null;
    let mathjaxBraceDepth = 0;

    function clozeDepth(): number {
        return clozeMathjaxBraceDepths.length;
    }

    function currentClozeMathjaxBraceDepth(): number | undefined {
        return clozeMathjaxBraceDepths[clozeMathjaxBraceDepths.length - 1];
    }

    while (index < storedHtml.length) {
        const clozeEnd = clozeOpenEnd(storedHtml, index);
        if (clozeEnd !== null) {
            output += storedHtml.slice(index, clozeEnd);
            clozeMathjaxBraceDepths.push(mathjaxBraceDepth);
            index = clozeEnd;
            continue;
        }

        if (mathjaxCloseDelimiter === null) {
            const closeDelimiter = mathjaxCloseDelimiterAt(storedHtml, index);
            if (closeDelimiter !== null) {
                output += storedHtml.slice(index, index + 2);
                mathjaxCloseDelimiter = closeDelimiter;
                mathjaxBraceDepth = 0;
                index += 2;
                continue;
            }

            if (clozeDepth() > 0 && storedHtml.startsWith("}}", index)) {
                output += "}}";
                clozeMathjaxBraceDepths.pop();
                index += 2;
                continue;
            }

            output += storedHtml[index];
            index++;
            continue;
        }

        if (storedHtml.startsWith(mathjaxCloseDelimiter, index)) {
            output += mathjaxCloseDelimiter;
            mathjaxCloseDelimiter = null;
            mathjaxBraceDepth = 0;
            index += 2;
            continue;
        }

        if (storedHtml.startsWith("\\&#", index)) {
            const entity = rightBraceEntityAt(storedHtml, index + 1);
            if (entity !== null) {
                output += "\\" + entity;
                index += entity.length + 1;
                continue;
            }
        }

        const entity = rightBraceEntityAt(storedHtml, index);
        if (entity !== null) {
            output += entity;
            if (mathjaxBraceDepth > 0) {
                mathjaxBraceDepth--;
            }
            index += entity.length;
            continue;
        }

        if (storedHtml.startsWith("\\{", index)) {
            output += "\\{";
            index += 2;
            continue;
        }

        if (storedHtml.startsWith("\\}", index)) {
            const entity = clozeDepth() > 0 && storedHtml[index + 2] === "}"
                ? rightBraceEntity
                : "}";
            output += "\\" + entity;
            index += 2;
            continue;
        }

        if (storedHtml[index] === "{") {
            output += "{";
            mathjaxBraceDepth++;
            index++;
            continue;
        }

        if (storedHtml[index] === "}") {
            const runEnd = rightBraceRunEnd(storedHtml, index);
            while (index < runEnd) {
                const clozeStartBraceDepth = currentClozeMathjaxBraceDepth();
                if (
                    clozeStartBraceDepth !== undefined
                    && mathjaxBraceDepth > clozeStartBraceDepth
                ) {
                    output += runEnd - index > 1 ? rightBraceEntity : "}";
                    mathjaxBraceDepth--;
                    index++;
                } else if (clozeStartBraceDepth !== undefined && runEnd - index >= 2) {
                    output += "}}";
                    clozeMathjaxBraceDepths.pop();
                    index += 2;
                } else if (mathjaxBraceDepth > 0) {
                    output += "}";
                    mathjaxBraceDepth--;
                    index++;
                } else {
                    output += "}";
                    index++;
                }
            }
            continue;
        }

        output += storedHtml[index];
        index++;
    }

    return output;
}

describe("escapeMathjaxClozeEntities plain-run slicing equivalence", () => {
    function mulberry32(seed: number): () => number {
        return () => {
            seed |= 0;
            seed = (seed + 0x6d2b79f5) | 0;
            let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
            t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
            return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
        };
    }

    const tokens = [
        "\\(",
        "\\)",
        "\\[",
        "\\]",
        "{{c1::",
        "{{c2,3::",
        "{{c::",
        "}}",
        "}",
        "{",
        "}}}",
        "\\{",
        "\\}",
        "\\}}",
        "&#125;",
        "&#0125;",
        "&#x7d;",
        "&#X07D;",
        "\\&#125;",
        "&",
        "&#",
        "&amp;",
        "\\frac{a}{b}",
        "plain text ",
        "<b>html</b>",
        "x",
        "\\\\",
        "\n",
    ];

    test("matches the original scanner on 4000 random inputs", () => {
        const random = mulberry32(0xa5eed);
        for (let caseIndex = 0; caseIndex < 4000; caseIndex++) {
            const parts: string[] = [];
            const length = 1 + Math.floor(random() * 20);
            for (let i = 0; i < length; i++) {
                parts.push(tokens[Math.floor(random() * tokens.length)]);
            }
            const input = parts.join("");
            expect(escapeMathjaxClozeEntities(input)).toBe(
                escapeMathjaxClozeEntitiesReference(input),
            );
        }
    });

    test("matches the original scanner on a large plain field", () => {
        const input = "lorem ipsum <div>dolor</div> ".repeat(5000);
        expect(escapeMathjaxClozeEntities(input)).toBe(
            escapeMathjaxClozeEntitiesReference(input),
        );
    });
});
