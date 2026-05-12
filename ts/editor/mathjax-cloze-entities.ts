// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

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

/**
 * Escape MathJax right braces that would otherwise be interpreted as cloze
 * close markers while the field is still raw stored text.
 */
export function escapeMathjaxClozeEntities(storedHtml: string): string {
    let output = "";
    let index = 0;
    let clozeDepth = 0;
    let mathjaxCloseDelimiter: "\\)" | "\\]" | null = null;
    let mathjaxBraceDepth = 0;

    while (index < storedHtml.length) {
        const clozeEnd = clozeOpenEnd(storedHtml, index);
        if (clozeEnd !== null) {
            output += storedHtml.slice(index, clozeEnd);
            clozeDepth++;
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

            if (clozeDepth > 0 && storedHtml.startsWith("}}", index)) {
                output += "}}";
                clozeDepth--;
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
            const entity = clozeDepth > 0 && storedHtml[index + 2] === "}"
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
            const runLength = runEnd - index;

            while (index < runEnd) {
                if (mathjaxBraceDepth > 0) {
                    output += clozeDepth > 0 && runLength > 1 ? rightBraceEntity : "}";
                    mathjaxBraceDepth--;
                    index++;
                } else if (clozeDepth > 0 && runEnd - index >= 2) {
                    output += "}}";
                    clozeDepth--;
                    index += 2;
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
