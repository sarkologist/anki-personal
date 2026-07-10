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

// The infinite-glue fills MathJax routes to its restricted `HFill` handler,
// longest first. \hfilneg is excluded by the non-letter boundary check below.
const fillCommands = ["\\hfilll", "\\hfill", "\\hfil"];

/**
 * End index of a `\hfilll`/`\hfill`/`\hfil` at `index`, or null. Requires a
 * non-letter boundary so `\hfil` does not match inside `\hfill`/`\hfilneg`.
 */
function fillCommandEnd(text: string, index: number): number | null {
    for (const command of fillCommands) {
        if (text.startsWith(command, index)) {
            const end = index + command.length;
            if (!/[a-zA-Z]/u.test(text[end] ?? "")) {
                return end;
            }
        }
    }
    return null;
}

/** End index of the `{…}` group starting at `start` (a `{`), or null. */
function texGroupEnd(text: string, start: number): number | null {
    let depth = 0;
    let index = start;
    while (index < text.length) {
        if (text[index] === "\\") {
            const next = text[index + 1];
            index += next === "{" || next === "}" ? 2 : 1;
            continue;
        }
        const char = text[index];
        if (char === "{") {
            depth++;
        } else if (char === "}") {
            if (depth === 1) {
                return index + 1;
            }
            depth = Math.max(0, depth - 1);
        }
        index++;
    }
    return null;
}

/** End index of `\begin`/`\end` plus its `{name}` argument, or null. */
function envCommandEnd(text: string, index: number, command: string): number | null {
    if (!text.startsWith(command, index)) {
        return null;
    }
    let index2 = index + command.length;
    if (/[a-zA-Z]/u.test(text[index2] ?? "")) {
        return null;
    }
    // ASCII whitespace only, to match the backend's char::is_ascii_whitespace
    // (Rust) — a Unicode-aware `\s` would diverge on e.g. a non-breaking space.
    while (index2 < text.length && " \t\n\x0c\r".includes(text[index2])) {
        index2++;
    }
    if (text[index2] !== "{") {
        return null;
    }
    return texGroupEnd(text, index2);
}

/**
 * Spans of top-level fill commands — those not nested inside a group or a
 * `\begin…\end` environment. Mirrors the backend's alignment-separator
 * scanning in rslib/src/cloze.rs so the editor preview matches the card.
 */
function topLevelFillSpans(text: string): Array<[number, number]> {
    const spans: Array<[number, number]> = [];
    let braceDepth = 0;
    let envDepth = 0;
    let index = 0;

    while (index < text.length) {
        if (braceDepth === 0 && envDepth === 0) {
            const end = fillCommandEnd(text, index);
            if (end !== null) {
                spans.push([index, end]);
                index = end;
                continue;
            }
        }

        if (text[index] === "\\") {
            const beginEnd = envCommandEnd(text, index, "\\begin");
            if (beginEnd !== null) {
                envDepth++;
                index = beginEnd;
                continue;
            }
            const endEnd = envCommandEnd(text, index, "\\end");
            if (endEnd !== null) {
                envDepth = Math.max(0, envDepth - 1);
                index = endEnd;
                continue;
            }
            const next = text[index + 1];
            index += next === "{" || next === "}" || next === "&" || next === "\\"
                ? 2
                : 1;
            continue;
        }

        const char = text[index];
        if (char === "{") {
            braceDepth++;
        } else if (char === "}") {
            braceDepth = Math.max(0, braceDepth - 1);
        }
        index++;
    }

    return spans;
}

function wrapClozeSegment(segment: string): string {
    return segment.trim() === "" ? segment : `{[${segment}]}`;
}

/**
 * Wrap a revealed cloze answer in `{[…]}`, splitting the wrap around any
 * top-level fill command (which MathJax forbids inside a group) so the fill
 * stays at the alignment cell's top level.
 */
function wrapClozeReveal(content: string): string {
    const spans = topLevelFillSpans(content);
    if (spans.length === 0) {
        return `{[${content}]}`;
    }

    let output = "";
    let lastEnd = 0;
    for (const [start, end] of spans) {
        output += wrapClozeSegment(content.slice(lastEnd, start));
        output += content.slice(start, end);
        lastEnd = end;
    }
    output += wrapClozeSegment(content.slice(lastEnd));
    return output;
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
        // bracketed answer itself looks the same. The wrap is split around any
        // top-level `\hfill`/`\hfil`, which MathJax rejects inside a group.
        output += wrapClozeReveal(
            revealMathjaxClozeAnswers(clozedText(input.slice(openEnd, closeStart))),
        );
        index = closeStart + 2;
    }

    return output;
}
