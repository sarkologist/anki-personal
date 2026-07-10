// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

const rightBraceEntityPattern = /^&#(?:0*125|x0*7d);/iu;

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

function rightBraceEntityAt(text: string, index: number): string | null {
    return text.slice(index).match(rightBraceEntityPattern)?.[0] ?? null;
}

function skipEscapedCharacter(text: string, index: number): number | null {
    if (text[index] !== "\\" || index + 1 >= text.length) {
        return null;
    }

    const entity = rightBraceEntityAt(text, index + 1);
    if (entity !== null) {
        return index + entity.length + 1;
    }

    return index + 2;
}

function clozeCloseStart(text: string, start: number): number | null {
    let index = start;
    let braceDepth = 0;
    let nestedClozeDepth = 0;

    while (index < text.length) {
        const openEnd = clozeOpenEnd(text, index);
        if (openEnd !== null) {
            nestedClozeDepth++;
            index = openEnd;
            continue;
        }

        const escapedEnd = skipEscapedCharacter(text, index);
        if (escapedEnd !== null) {
            index = escapedEnd;
            continue;
        }

        const entity = rightBraceEntityAt(text, index);
        if (entity !== null) {
            if (braceDepth > 0) {
                braceDepth--;
            }
            index += entity.length;
            continue;
        }

        const char = text[index];
        if (char === "{") {
            braceDepth++;
            index++;
            continue;
        }

        if (char === "}") {
            if (braceDepth > 0) {
                braceDepth--;
                index++;
            } else if (text.startsWith("}}", index)) {
                if (nestedClozeDepth === 0) {
                    return index;
                }
                nestedClozeDepth--;
                index += 2;
            } else {
                index++;
            }
            continue;
        }

        index++;
    }

    return null;
}

function clozeHintStart(text: string): number | null {
    let index = 0;
    let braceDepth = 0;

    while (index < text.length) {
        const escapedEnd = skipEscapedCharacter(text, index);
        if (escapedEnd !== null) {
            index = escapedEnd;
            continue;
        }

        const entity = rightBraceEntityAt(text, index);
        if (entity !== null) {
            if (braceDepth > 0) {
                braceDepth--;
            }
            index += entity.length;
            continue;
        }

        const char = text[index];
        if (char === "{") {
            braceDepth++;
            index++;
        } else if (char === "}") {
            braceDepth = Math.max(0, braceDepth - 1);
            index++;
        } else if (braceDepth === 0 && text.startsWith("::", index)) {
            return index;
        } else {
            index++;
        }
    }

    return null;
}

function clozedText(text: string): string {
    const hintStart = clozeHintStart(text);
    return hintStart === null ? text : text.slice(0, hintStart);
}

export function revealMathjaxClozeAnswers(input: string): string {
    let output = "";
    let index = 0;

    while (index < input.length) {
        const openEnd = clozeOpenEnd(input, index);
        if (openEnd === null) {
            const escapedEnd = skipEscapedCharacter(input, index);
            if (escapedEnd !== null) {
                output += rightBraceEntityAt(input, index + 1) === null
                    ? input.slice(index, escapedEnd)
                    : "\\}";
                index = escapedEnd;
                continue;
            }

            const entity = rightBraceEntityAt(input, index);
            if (entity !== null) {
                output += "}";
                index += entity.length;
                continue;
            }

            output += input[index];
            index++;
            continue;
        }

        const closeStart = clozeCloseStart(input, openEnd);
        if (closeStart === null) {
            output += input[index];
            index++;
            continue;
        }

        // Wrap the reveal in a TeX group so the leading `[` can't be captured
        // as an argument of a preceding token — e.g. `\\[…]` (a row break with
        // an optional spacing argument, which errors) or `a_[x]` (subscript of
        // just `[`). The group changes how the reveal binds to surrounding
        // tokens (and confines any scoped declarations inside the cloze); the
        // bracketed answer itself looks the same.
        output += `{[${revealMathjaxClozeAnswers(clozedText(input.slice(openEnd, closeStart)))}]}`;
        index = closeStart + 2;
    }

    return output;
}
