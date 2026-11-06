// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { shellCommand } from "$lib/shell/shell";

export { shellCommand };

export type Theme = "system" | "light" | "dark";

/** The shell's own preferences (`shell/src/preferences.rs`). */
export interface ShellPreferences {
    theme: Theme;
    /** One command per line. */
    latex: { png: string; svg: string };
    latexDefaults: { png: string; svg: string };
}

export interface ShellUpdate {
    theme?: Theme;
    latex?: { png: string; svg: string };
    resetLatex?: boolean;
}

/** Save the shell's preferences; the error message if they were refused. */
export async function saveShellPreferences(update: ShellUpdate): Promise<string | null> {
    const out = await shellCommand<{ ok?: boolean; error?: string } | null>(
        `setShellPreferences:${JSON.stringify(update)}`,
    );
    return out?.ok ? null : (out?.error ?? "not saved");
}

/** Whole minutes for a number of seconds, as the Qt spin boxes show them. */
export function secsToMins(secs: number): number {
    return Math.round(secs / 60);
}

/** Seconds for minutes typed in; an empty, negative or odd entry is 0. */
export function minsToSecs(mins: number): number {
    return Number.isFinite(mins) ? Math.max(0, Math.round(mins)) * 60 : 0;
}
