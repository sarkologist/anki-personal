// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

export interface MathjaxSurround {
    prefix: string;
    suffix: string;
}

export function mathjaxHighlightSurround(className: string): MathjaxSurround {
    return {
        prefix: `{\\color{var(--${className})}`,
        suffix: "}",
    };
}
