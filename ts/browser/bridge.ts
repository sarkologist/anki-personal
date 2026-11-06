// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

import { bridgeCommand } from "@tslib/bridgecommand";

import type { SidebarItem } from "./lib";

/** A bridge command's reply. The shell keeps commands in order. */
export function call<T>(command: string): Promise<T> {
    return new Promise((resolve) => bridgeCommand<T>(command, resolve));
}

export interface Column {
    key: string;
    label: string;
    tooltip: string;
    sortable: boolean;
    center: boolean;
    font: boolean;
}

export interface TableInfo {
    gen: number;
    notes_mode: boolean;
    search: string;
    count: number;
    columns: Column[];
    sort_column: string;
    sort_backwards: boolean;
}

export interface SelectionInfo {
    gen: number;
    ranges: [number, number][];
    current: number | null;
}

export interface Cell {
    text: string;
    rtl?: boolean;
}

export interface Row {
    id: number;
    cells: Cell[];
    color?: number;
    font?: string;
    fontSize?: number;
    disabled?: boolean;
}

export interface Shortcut {
    action: string;
    code: string;
    meta: boolean;
    shift: boolean;
    alt: boolean;
}

/** What the shell pushes (`browserReceive`). */
export type Message =
    | { type: "table"; table: TableInfo; selection: SelectionInfo; prompt: string }
    | { type: "selection"; selection: SelectionInfo }
    | { type: "refresh" }
    | { type: "sidebar"; sidebar: SidebarItem[] }
    | { type: "addonMenus"; menus: string[] }
    | { type: "key"; action: string };

export interface Init {
    table: TableInfo;
    selection: SelectionInfo;
    prompt: string;
    sidebar: SidebarItem[];
    history: string[];
    cardWidths: Record<string, number>;
    noteWidths: Record<string, number>;
    sidebarWidth: number;
    addonMenus: string[];
    shortcuts: Shortcut[];
    rowHeight: number;
    labels: Record<string, string>;
}

declare global {
    interface Window {
        browserReceive?: (msg: Message) => void;
        shellBrowserFlush?: () => void;
    }
}
