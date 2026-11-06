// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { bridgeCommand } from "@tslib/bridgecommand";

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

export function shellCommand<T>(command: string): Promise<T> {
    return new Promise((resolve) => bridgeCommand<T>(command, resolve));
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

/** A whole number in [min, max] for a number box; `fallback` if the box is
empty or not a number (Svelte binds an empty box as null). */
export function toUint(value: number | null | undefined, min: number, max: number, fallback: number): number {
    if (typeof value !== "number" || !Number.isFinite(value)) {
        return fallback;
    }
    return Math.min(max, Math.max(min, Math.round(value)));
}
