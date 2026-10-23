// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { afterEach, expect, test, vi } from "vitest";

import { ChangeTimer } from "./change-timer";

afterEach(() => {
    vi.useRealTimers();
});

test("fires the scheduled action once after the delay", () => {
    vi.useFakeTimers();
    const timer = new ChangeTimer();
    const action = vi.fn();

    timer.schedule(action, 600);
    vi.advanceTimersByTime(600);
    timer.fireImmediately();

    expect(action).toHaveBeenCalledTimes(1);
});

test("a cleared action never runs, even on a later immediate flush", () => {
    // NoteEditor clears pending saves when loading a note; a flush on close
    // must not resurrect a save queued before the load.
    vi.useFakeTimers();
    const timer = new ChangeTimer();
    const action = vi.fn();

    timer.schedule(action, 600);
    timer.clear();
    vi.advanceTimersByTime(600);
    timer.fireImmediately();

    expect(action).not.toHaveBeenCalled();
});

test("only the latest scheduled action runs", () => {
    const timer = new ChangeTimer();
    const first = vi.fn();
    const second = vi.fn();

    timer.schedule(first, 600);
    timer.schedule(second, 600);
    timer.fireImmediately();

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
});
