// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The card browser window (`aqt/browser/browser.py`): the stock editor
//! page in Browse mode, with the table, sidebar and search bar in a frame
//! beside it (`ts/routes/browser`). The editor CSP allows that frame
//! (`legacyPageData`), and the editor's evals keep working as in the other
//! editor windows. The table talks to the shell over the bridge only.
//!
//! Also here: the actions on the selected cards and notes, their dialogs
//! (find and replace, find duplicates, reposition, set due date, forget,
//! tags, decks), the sidebar's menus, the previewer, card info, and the
//! add-on browser hooks (`browser_did_fetch_columns`, `browser_will_search`,
//! `browser_did_search`, `browser_did_fetch_row`, the menus they add).

use std::collections::HashMap;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicU32;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;

use anki_proto::collection::OpChanges;
use anki_proto::generic;
use anki_proto::search::search_node::Filter;
use anki_proto::search::BrowserRow;
use prost::Message;
use serde::Deserialize;
use serde_json::json;
use serde_json::Value;
use tauri::menu::CheckMenuItemBuilder;
use tauri::menu::IsMenuItem;
use tauri::menu::Menu;
use tauri::menu::MenuItemBuilder;
use tauri::menu::PredefinedMenuItem;
use tauri::menu::SubmenuBuilder;
use tauri::Manager;
use tauri::Wry;

use crate::app::Content;
use crate::app::Ui;
use crate::browser;
use crate::browser::Hooks;
use crate::browser::SearchContext;
use crate::browser::State;
use crate::editor_window::BROWSER;
use crate::main_window::DialogAnswer;
use crate::pages;
use crate::pages::WebviewKind;

/// The table's page, in a frame of the browser's page.
pub const TABLE_PAGE: &str = "browser-table";
// The other windows aren't `browser-*`: the browser's page may frame its
// own `browser-*` pages (`legacyPageData`), and the previewer's has card JS.
const PREVIEW: &str = "browse-preview";
const PREVIEW_CARD: &str = "browse-preview-card";
const CARD_INFO: &str = "browse-card-info";
const DUPES: &str = "browse-dupes";
const CHANGE_NOTETYPE: &str = "browse-change-notetype";
/// Menu ids of the browser's popups.
pub const MENU_PREFIX: &str = "browser:";
/// How long the browser waits for add-ons while drawing rows or searching.
/// (rows: an add-on may render each card, as Suspended Card Priority does)
const HOOK_TIMEOUT: Duration = Duration::from_millis(1500);
const SEARCH_HOOK_TIMEOUT: Duration = Duration::from_secs(3);
const HISTORY_SIZE: usize = 30;

fn js(s: impl AsRef<str>) -> String {
    serde_json::to_string(s.as_ref()).unwrap()
}

/// Qt's menu texts carry `&` mnemonics.
fn plain(s: std::borrow::Cow<str>) -> String {
    s.replace('&', "")
}

/// The browser's keys (`forms/browser.ui`, Qt's Ctrl is Cmd): action,
/// key, and whether it also works while typing in the editor.
pub const SHORTCUTS: &[(&str, &str, bool)] = &[
    ("find", "Cmd+F", true),
    ("sidebarFilter", "Cmd+Shift+F", true),
    ("sidebar", "Cmd+Shift+R", true),
    ("cardList", "Cmd+Shift+L", true),
    ("nextCard", "Cmd+N", true),
    ("previousCard", "Cmd+P", true),
    ("selectAll", "Cmd+Alt+A", false),
    ("invertSelection", "Cmd+Alt+S", false),
    ("undo", "Cmd+Z", false),
    ("redo", "Cmd+Shift+Z", false),
    ("toggleMode", "Cmd+Alt+T", true),
    ("findReplace", "Cmd+Alt+F", true),
    ("changeNotetype", "Cmd+Shift+M", true),
    ("reposition", "Cmd+Shift+S", true),
    ("info", "Cmd+Shift+I", true),
    ("addTags", "Cmd+Shift+A", true),
    ("removeTags", "Cmd+Alt+Shift+A", true),
    ("suspend", "Cmd+J", true),
    ("bury", "Cmd+Shift+J", true),
    ("delete", "Cmd+Backspace", false),
    ("addNotes", "Cmd+E", true),
    ("changeDeck", "Cmd+D", true),
    ("mark", "Cmd+K", true),
    ("setDueDate", "Cmd+Shift+D", true),
    ("forget", "Cmd+Alt+N", true),
    ("preview", "Cmd+Shift+P", false),
    ("flag:1", "Cmd+1", true),
    ("flag:2", "Cmd+2", true),
    ("flag:3", "Cmd+3", true),
    ("flag:4", "Cmd+4", true),
    ("flag:5", "Cmd+5", true),
    ("flag:6", "Cmd+6", true),
    ("flag:7", "Cmd+7", true),
    ("close", "Cmd+W", true),
];

fn shortcuts_json(in_editor: bool) -> Value {
    Value::Array(
        SHORTCUTS
            .iter()
            .filter(|(_, _, editor)| !in_editor || *editor)
            .map(|(action, hotkey, _)| {
                let mut spec = if *hotkey == "Cmd+Backspace" {
                    json!({"code": "Backspace", "meta": true, "shift": false, "alt": false})
                } else {
                    crate::editor_window::key_spec(hotkey)
                };
                spec["action"] = json!(action);
                spec["key"] = json!(hotkey);
                spec
            })
            .collect(),
    )
}

// State
//////////////////////////////////////////////////////////////////////////

/// What add-ons asked for when the browser opened.
#[derive(Default, Clone)]
struct AddonUi {
    /// `browser_did_fetch_columns`
    columns: Vec<browser::Column>,
    /// Hooks with handlers, so the others needn't be asked.
    hooks: Vec<String>,
}

#[derive(Default)]
struct Preview {
    card: Option<i64>,
    answer: bool,
    /// The rendered card's sounds, for its play buttons.
    sounds: Vec<(Vec<String>, Vec<String>)>,
    /// Whether the answer is shown (also with "back side only").
    shown_answer: bool,
}

/// What the plugin host may ask about without waiting for the table.
#[derive(Default, Clone)]
struct Snapshot {
    search: String,
    notes_mode: bool,
    selected: Vec<i64>,
    current: Option<i64>,
    sort_column: String,
    columns: Vec<String>,
    count: usize,
}

#[derive(Default)]
pub struct BrowserUi {
    state: Mutex<Option<State>>,
    /// Whether `state` is set, without its lock (which is held while the
    /// table waits on add-ons).
    open: AtomicBool,
    snapshot: Mutex<Snapshot>,
    addons: Mutex<AddonUi>,
    /// Add-on menus (`browser_menus_did_init`): title -> items.
    menus: Mutex<Vec<(String, Vec<crate::plugins::ui::MenuItem>)>>,
    preview: Mutex<Preview>,
    /// The sidebar item a menu was opened on.
    sidebar_item: Mutex<Value>,
    dupes: Mutex<Vec<(String, Vec<i64>)>>,
    /// Notes the change notetype page changes.
    change_notetype: Mutex<Vec<i64>>,
    /// Runs add-ons' requests in order, off the host's request thread
    /// (they may send the host events, which it can't answer while it waits
    /// on us).
    worker: Mutex<Option<std::sync::mpsc::Sender<Job>>>,
    /// What changed since the last refresh (`OpChanges` flags).
    pending: AtomicU32,
    refresh_scheduled: AtomicBool,
}

type Job = Box<dyn FnOnce(&Arc<Ui>) + Send>;

/// Run `job` after the add-ons' earlier requests.
pub fn queue(ui: &Arc<Ui>, job: impl FnOnce(&Arc<Ui>) + Send + 'static) {
    let mut worker = ui.browser.worker.lock().unwrap();
    let sender = worker.get_or_insert_with(|| {
        let (tx, rx) = std::sync::mpsc::channel::<Job>();
        let u = Arc::downgrade(ui);
        std::thread::spawn(move || {
            for job in rx {
                match u.upgrade() {
                    Some(ui) => job(&ui),
                    None => break,
                }
            }
        });
        tx
    });
    let _ = sender.send(Box::new(job));
}

const CHANGED_TABLE: u32 = 1;
const CHANGED_SIDEBAR: u32 = 2;
const CHANGED_NOTE: u32 = 4;

/// The add-ons' browser menus: title -> items (for tests).
pub fn addon_menus(ui: &Ui) -> Vec<(String, Vec<crate::plugins::ui::MenuItem>)> {
    ui.browser.menus.lock().unwrap().clone()
}

pub fn is_open(ui: &Ui) -> bool {
    ui.browser.open.load(Ordering::SeqCst)
}

fn set_state(ui: &Ui, state: Option<State>) {
    let open = state.is_some();
    *ui.browser.state.lock().unwrap() = state;
    ui.browser.open.store(open, Ordering::SeqCst);
    if !open {
        *ui.browser.snapshot.lock().unwrap() = Snapshot::default();
    }
}

fn with_state<T>(ui: &Ui, f: impl FnOnce(&mut State) -> T) -> Option<T> {
    let mut guard = ui.browser.state.lock().unwrap();
    let out = guard.as_mut().map(f);
    if let Some(state) = guard.as_ref() {
        *ui.browser.snapshot.lock().unwrap() = Snapshot {
            search: state.search.clone(),
            notes_mode: state.notes_mode,
            selected: state.selected.clone(),
            current: state.current,
            sort_column: state.sort_column.clone(),
            columns: state.columns.clone(),
            count: state.items.len(),
        };
    }
    out
}

/// Push a message to the table page (`browserReceive`).
fn push(ui: &Ui, msg: Value) {
    if ui.app.get_webview_window(BROWSER).is_some() {
        ui.eval(
            BROWSER,
            format!("window.shellBrowser && shellBrowser({msg});"),
        );
    }
}

// Add-on hooks
//////////////////////////////////////////////////////////////////////////

struct HostHooks<'a> {
    ui: &'a Ui,
    hooks: Vec<String>,
}

impl HostHooks<'_> {
    fn new(ui: &Ui) -> HostHooks<'_> {
        let hooks = if ui.plugins.host.is_running() {
            ui.browser.addons.lock().unwrap().hooks.clone()
        } else {
            vec![]
        };
        HostHooks { ui, hooks }
    }

    fn has(&self, hook: &str) -> bool {
        self.hooks.iter().any(|h| h == hook)
    }
}

impl Hooks for HostHooks<'_> {
    fn will_search(&self, ctx: &mut SearchContext, notes: bool) {
        if !self.has("browser_will_search") {
            return;
        }
        let Some(out) = self.ui.plugins.host.event(
            "browser_will_search",
            json!({"search": ctx.search, "order": ctx.order, "reverse": ctx.reverse,
                   "notes_mode": notes}),
            SEARCH_HOOK_TIMEOUT,
        ) else {
            return;
        };
        if out["changed"] == true {
            ctx.changed_by_addons = true;
            ctx.search = out["search"].as_str().unwrap_or(&ctx.search).to_string();
            ctx.order = out["order"].as_str().map(str::to_string);
            ctx.reverse = out["reverse"].as_bool().unwrap_or(ctx.reverse);
            ctx.ids = serde_json::from_value(out["ids"].clone()).ok();
        }
    }

    fn did_search(&self, ctx: &mut SearchContext, notes: bool) {
        if !self.has("browser_did_search") {
            return;
        }
        let Some(out) = self.ui.plugins.host.event(
            "browser_did_search",
            json!({"search": ctx.search, "order": ctx.order, "reverse": ctx.reverse,
                   "notes_mode": notes, "ids": ctx.ids}),
            SEARCH_HOOK_TIMEOUT,
        ) else {
            return;
        };
        if let Ok(ids) = serde_json::from_value::<Vec<i64>>(out["ids"].clone()) {
            ctx.ids = Some(ids);
        }
    }

    fn fetched_rows(&self, rows: &mut [(i64, BrowserRow)], notes: bool, columns: &[String]) {
        if !self.has("browser_did_fetch_row") {
            return;
        }
        let sent: Vec<Value> = rows
            .iter()
            .map(|(id, row)| {
                json!({"id": id, "row": data_encoding::BASE64.encode(&row.encode_to_vec())})
            })
            .collect();
        let Some(out) = self.ui.plugins.host.event(
            "browser_did_fetch_row",
            json!({"rows": sent, "notes_mode": notes, "columns": columns}),
            HOOK_TIMEOUT,
        ) else {
            return;
        };
        let Some(changed) = out.as_array() else {
            return;
        };
        for ((_, row), new) in rows.iter_mut().zip(changed) {
            if let Some(new) = new
                .as_str()
                .and_then(|s| data_encoding::BASE64.decode(s.as_bytes()).ok())
                .and_then(|b| BrowserRow::decode(b.as_slice()).ok())
            {
                *row = new;
            }
        }
    }
}

/// Ask the add-ons for their columns and menus, as the browser opens
/// (`browser_did_fetch_columns`, `browser_will_show`,
/// `browser_menus_did_init`).
fn fetch_addon_ui(ui: &Ui) {
    let mut addons = AddonUi::default();
    ui.browser.menus.lock().unwrap().clear();
    if ui.plugins.host.is_running() {
        let builtin: Vec<String> = browser::builtin_columns(&ui.shell.api)
            .into_iter()
            .map(|c| c.key)
            .collect();
        if let Some(out) = ui.plugins.host.event(
            "browser_will_show",
            json!({"columns": builtin}),
            SEARCH_HOOK_TIMEOUT,
        ) {
            addons.columns = serde_json::from_value(out["columns"].clone()).unwrap_or_default();
            addons.hooks = serde_json::from_value(out["hooks"].clone()).unwrap_or_default();
        }
    }
    *ui.browser.addons.lock().unwrap() = addons;
}

/// The add-ons (re)started with the browser open: get their columns and
/// menus now, and their order if the table is sorted by their column.
pub fn host_ready(ui: &Arc<Ui>) {
    if !is_open(ui) {
        return;
    }
    queue(ui, |ui| {
        fetch_addon_ui(ui);
        let columns = ui.browser.addons.lock().unwrap().columns.clone();
        let addon_sort = with_state(ui, |s| {
            s.set_addon_columns(columns.clone());
            columns.iter().any(|c| c.key == s.sort_column)
        });
        match addon_sort {
            Some(true) => research(ui),
            Some(false) => {
                if let Some(msg) = with_state(ui, |s| table_message(ui, s)) {
                    push(ui, msg);
                }
            }
            None => {}
        }
    });
}

/// The add-ons stopped: their menus go (their columns stay, unfilled).
pub fn host_stopped(ui: &Ui) {
    ui.browser.menus.lock().unwrap().clear();
    ui.browser.addons.lock().unwrap().hooks.clear();
    push(ui, json!({"type": "addonMenus", "menus": []}));
}

/// The plugin host's requests about the browser; false if `op` isn't one.
pub fn on_host(ui: &Arc<Ui>, op: &str, msg: &Value) -> Option<Option<Value>> {
    match op {
        "browser_state" => {
            let snap = ui.browser.snapshot.lock().unwrap().clone();
            let api = &ui.shell.api;
            let cards = if snap.notes_mode {
                browser::cards_of_notes(api, &snap.selected)
            } else {
                snap.selected.clone()
            };
            let current_card = snap.current.and_then(|item| {
                if snap.notes_mode {
                    first_card_of_note(api, item)
                } else {
                    Some(item)
                }
            });
            Some(Some(json!({
                "open": is_open(ui),
                "count": snap.count,
                "search": snap.search,
                "notes_mode": snap.notes_mode,
                "selected": snap.selected,
                "selected_cards": cards,
                "current": snap.current,
                "current_card": current_card,
                "sort_column": snap.sort_column,
                "columns": snap.columns,
            })))
        }
        "browser_research" => {
            queue(ui, research);
            Some(None)
        }
        // here, so the add-on's next look at the columns has it
        // the add-on's next look at the columns has it at once; the table
        // changes in order with its other requests
        "browser_column" => {
            let key = msg["key"].as_str().unwrap_or_default().to_string();
            let shown = msg["shown"].as_bool().unwrap_or(true);
            {
                let mut snap = ui.browser.snapshot.lock().unwrap();
                if shown && !snap.columns.contains(&key) {
                    snap.columns.push(key.clone());
                } else if !shown {
                    snap.columns.retain(|c| *c != key);
                }
            }
            queue(ui, move |ui| {
                let api = &ui.shell.api;
                let out = with_state(ui, |s| {
                    if s.columns.contains(&key) == shown {
                        return None;
                    }
                    s.toggle_column(api, &key, &ui.main.tr)
                        .ok()
                        .map(|()| table_message(ui, s))
                });
                if let Some(Some(msg)) = out {
                    push(ui, msg);
                }
            });
            Some(None)
        }
        "browser_sort" => {
            let key = msg["key"].as_str().unwrap_or_default().to_string();
            let descending = msg["descending"].as_bool().unwrap_or(false);
            queue(ui, move |ui| {
                let hooks = HostHooks::new(ui);
                let api = &ui.shell.api;
                let out = with_state(ui, |s| {
                    s.sync_backend_columns(api);
                    s.sort_to(api, &hooks, &key, descending, &ui.main.tr)
                        .map(|()| table_message(ui, s))
                });
                if let Some(Ok(msg)) = out {
                    push(ui, msg);
                    selection_changed(ui);
                }
            });
            Some(None)
        }
        "menu" => {
            let name = msg["menu"].as_str().unwrap_or_default();
            let title = name.strip_prefix("browser:")?;
            let items: Vec<crate::plugins::ui::MenuItem> =
                serde_json::from_value(msg["items"].clone()).unwrap_or_default();
            let mut menus = ui.browser.menus.lock().unwrap();
            match menus.iter_mut().find(|(t, _)| t == title) {
                Some(menu) => menu.1 = items,
                None => menus.push((title.to_string(), items)),
            }
            let titles: Vec<String> = menus.iter().map(|(t, _)| t.clone()).collect();
            drop(menus);
            push(ui, json!({"type": "addonMenus", "menus": titles}));
            Some(None)
        }
        _ => None,
    }
}

// Opening
//////////////////////////////////////////////////////////////////////////

/// The browser, searching for `filter` (`aqt.dialogs.open("Browser",
/// search=...)`).
pub fn open_with_search(ui: &Arc<Ui>, filter: Filter) {
    match browser::normalize(&ui.shell.api, browser::node(filter)) {
        Ok(search) => open(ui, Some(search), None),
        Err(err) => crate::main_window::message(ui, &err.message),
    }
}

/// Open the browser (or bring it to the front), searching for `search`
/// (normalized) or the default search, and selecting `card` if it's found.
pub fn open(ui: &Arc<Ui>, search: Option<String>, card: Option<i64>) {
    if ui.shell.open_profile_paths().is_none() {
        return;
    }
    if is_open(ui) && ui.app.get_webview_window(BROWSER).is_some() {
        if let Some(w) = ui.app.get_webview_window(BROWSER) {
            if !ui.background {
                let _ = w.set_focus();
            }
        }
        if let Some(search) = search {
            search_for(ui, &search, None);
        } else if let Some(card) = card {
            // `Browser.reopen`: the default search, then the card
            let api = &ui.shell.api;
            let typed = browser::get_config(api, "defaultSearchText")
                .and_then(|v| v.as_str().map(str::to_string))
                .unwrap_or_default();
            search_for(ui, &browser::default_search(api), Some(&typed));
            select_card(ui, card);
        }
        return;
    }
    fetch_addon_ui(ui);
    let api = &ui.shell.api;
    let tr = &ui.main.tr;
    let addon_columns = ui.browser.addons.lock().unwrap().columns.clone();
    let mut state = State::load(api, addon_columns);
    let (search, prompt) = match search {
        Some(search) => (search.clone(), search),
        None => {
            let default = browser::default_search(api);
            let typed = browser::get_config(api, "defaultSearchText")
                .and_then(|v| v.as_str().map(str::to_string))
                .unwrap_or_default();
            (default, typed)
        }
    };
    let hooks = HostHooks::new(ui);
    if let Err(err) = state.search(api, &hooks, &search, tr) {
        tracing::warn!(%err, "browser search failed");
    }
    set_state(ui, Some(state));
    with_state(ui, |s| {
        s.search_prompt = prompt;
    });
    if let Some(card) = card {
        with_state(ui, |s| select_card_in(s, api, card));
    }
    ui.shell
        .pages
        .set(TABLE_PAGE, WebviewKind::Editor, Some(table_html(ui)));
    if let Err(err) = crate::editor_window::open_browse_page(ui, browse_js(ui)) {
        tracing::error!(%err, "opening the browser failed");
        set_state(ui, None);
        return;
    }
    selection_changed(ui);
}

fn table_html(ui: &Ui) -> String {
    pages::std_html(
        &ui.shell.web,
        "<div id=browser></div>",
        &["pages/browser.css"],
        &["pages/browser.js"],
        true,
    )
}

/// The main frame's part: passing messages to the table's frame, the
/// splitter, and the browser's keys while typing in the editor.
fn browse_js(ui: &Ui) -> String {
    let width = ui.settings.lock().unwrap().browser.split;
    format!(
        r#"(() => {{
    const frame = document.getElementById("shell-browser");
    const queue = [];
    const receiver = () => frame.contentWindow && frame.contentWindow.browserReceive;
    window.shellBrowser = (msg) => {{ const r = receiver(); if (r) r(msg); else queue.push(msg); }};
    // after the page's start, which had the sidebar and menus as they were
    window.shellBrowserFlush = () => {{ const r = receiver(); for (const m of queue.splice(0)) if (r && m.type !== "sidebar" && m.type !== "addonMenus") r(m); }};
    window.shellBrowserNote = (has) => document.body.classList.toggle("shell-no-note", !has);
    const width = {width};
    if (width > 0) document.body.style.setProperty("--shell-table", width + "px");
    const split = document.getElementById("shell-split");
    split.addEventListener("pointerdown", (e) => {{
        e.preventDefault();
        split.setPointerCapture(e.pointerId);
        frame.style.pointerEvents = "none";
        const move = (ev) => {{
            const w = Math.max(320, Math.min(ev.clientX, innerWidth - 260));
            document.body.style.setProperty("--shell-table", w + "px");
        }};
        split.addEventListener("pointermove", move);
        split.addEventListener("pointerup", () => {{
            split.removeEventListener("pointermove", move);
            frame.style.pointerEvents = "";
            pycmd("browserSplit:" + Math.round(frame.getBoundingClientRect().width));
        }}, {{ once: true }});
    }});
    const keys = {keys};
    document.addEventListener("keydown", (e) => {{
        for (const k of keys) {{
            if (e.code === k.code && e.metaKey === k.meta && e.shiftKey === k.shift && e.altKey === k.alt && !e.ctrlKey) {{
                e.preventDefault();
                e.stopPropagation();
                if (k.action === "find" || k.action === "sidebarFilter" || k.action === "cardList"
                    || k.action === "nextCard" || k.action === "previousCard" || k.action === "sidebar") {{
                    shellBrowser({{ type: "key", action: k.action }});
                }} else {{
                    pycmd("browserAction:" + k.action);
                }}
                return;
            }}
        }}
    }}, true);
}})();"#,
        keys = shortcuts_json(true),
    )
}

/// The browser's page again (after a MathJax toggle), keeping its state.
pub fn reload_page(ui: &Arc<Ui>) {
    if crate::editor_window::open_browse_page(ui, browse_js(ui)).is_ok() {
        selection_changed(ui);
    }
}

pub fn close(ui: &Arc<Ui>) {
    crate::editor_window::close(ui, BROWSER);
}

/// The window went away.
pub fn destroyed(ui: &Arc<Ui>) {
    set_state(ui, None);
    ui.shell.pages.remove(TABLE_PAGE);
    ui.shell.pages.remove(PREVIEW_CARD);
    *ui.browser.preview.lock().unwrap() = Preview::default();
    ui.browser.dupes.lock().unwrap().clear();
    ui.browser.change_notetype.lock().unwrap().clear();
    for label in [PREVIEW, CARD_INFO, DUPES, CHANGE_NOTETYPE] {
        if let Some(w) = ui.app.get_webview_window(label) {
            let _ = w.destroy();
        }
    }
    if ui.plugins.host.is_running() {
        crate::plugins::ui::notify(ui, "browser_closed", json!({}));
    }
}

// Searching and selecting
//////////////////////////////////////////////////////////////////////////

fn table_message(ui: &Ui, state: &State) -> Value {
    json!({
        "type": "table",
        "table": state.table_json(&ui.main.tr),
        "selection": state.selection_json(),
        "prompt": state.search_prompt,
    })
}

/// Search for `search` (normalized), showing `prompt` in the search bar
/// (`Browser.search_for`).
pub fn search_for(ui: &Arc<Ui>, search: &str, prompt: Option<&str>) -> Value {
    let hooks = HostHooks::new(ui);
    let api = &ui.shell.api;
    let out = with_state(ui, |s| {
        s.sync_backend_columns(api);
        match s.search(api, &hooks, search, &ui.main.tr) {
            Ok(()) => {
                s.search_prompt = prompt.unwrap_or(search).to_string();
                Ok(table_message(ui, s))
            }
            Err(err) => Err(err.message),
        }
    });
    match out {
        Some(Ok(msg)) => {
            push(ui, msg.clone());
            selection_changed(ui);
            msg
        }
        Some(Err(err)) => {
            crate::main_window::message(ui, &err);
            json!({"error": err})
        }
        None => Value::Null,
    }
}

/// Search again (`Browser.search`, from add-ons and after notes are
/// added or removed).
pub fn research(ui: &Arc<Ui>) {
    let search = ui.browser.snapshot.lock().unwrap().search.clone();
    let prompt = with_state(ui, |s| s.search_prompt.clone());
    if prompt.is_some() {
        search_for(ui, &search, prompt.as_deref());
    }
}

fn select_card_in(state: &mut State, api: &crate::api::Api, card: i64) {
    let item = if state.notes_mode {
        state.note_of(api, card)
    } else {
        Some(card)
    };
    if let Some(row) = item.and_then(|i| state.row_of(i)) {
        let gen = state.gen;
        state.set_selection(gen, &[(row, row + 1)], Some(row));
    }
}

/// `Table.select_single_card`.
fn select_card(ui: &Arc<Ui>, card: i64) {
    let api = &ui.shell.api;
    if let Some(sel) = with_state(ui, |s| {
        select_card_in(s, api, card);
        s.selection_json()
    }) {
        push(ui, json!({"type": "selection", "selection": sel}));
        selection_changed(ui);
    }
}

/// After the selection or the current row changed
/// (`on_all_or_selected_rows_changed`, `on_current_row_changed`): the
/// title, the editor, the previewer, card info and add-ons.
fn selection_changed(ui: &Arc<Ui>) {
    let api = &ui.shell.api;
    let Some((selected, total, notes, single_card, current_card)) = with_state(ui, |s| {
        (
            s.selected.len(),
            s.items.len(),
            s.notes_mode,
            s.single_card(api),
            s.current_card(api),
        )
    }) else {
        return;
    };
    let tr = &ui.main.tr;
    let title = if notes {
        tr.browsing_window_title_notes(total as u32, selected as u32)
    } else {
        tr.browsing_window_title(total as u32, selected as u32)
    };
    if let Some(w) = ui.app.get_webview_window(BROWSER) {
        let _ = w.set_title(&title);
    }
    // the editor, once its note is saved (`ensure_editor_saved`)
    let single_note = single_card.and_then(|cid| note_of_card(api, cid));
    let shown = ui.editors.session(BROWSER).map(|s| s.note.id);
    if single_note != shown {
        ui.after_saved(BROWSER, &format!("editorLoad:{}", single_note.unwrap_or(0)));
    } else if single_card.is_some() {
        ui.editors.update(BROWSER, |s| s.card_id = single_card);
    }
    if let Some(cid) = current_card {
        update_preview(ui, cid);
        update_card_info(ui, cid);
    }
}

fn first_card_of_note(api: &crate::api::Api, nid: i64) -> Option<i64> {
    api.query(
        "select id from cards where nid = ? order by ord limit 1",
        &[json!(nid)],
    )
    .ok()?
    .first()?
    .first()?
    .as_i64()
}

fn note_of_card(api: &crate::api::Api, cid: i64) -> Option<i64> {
    api.call::<anki_proto::cards::Card>("get_card", &anki_proto::cards::CardId { cid })
        .ok()
        .map(|c| c.note_id)
}

/// The editor saved its note: show the single selected card's note now,
/// or none.
pub fn editor_saved(ui: &Arc<Ui>) {
    let api = &ui.shell.api;
    let Some(card) = with_state(ui, |s| s.single_card(api)) else {
        return;
    };
    let note = card.and_then(|cid| note_of_card(api, cid));
    match (note, card) {
        (Some(nid), card) => {
            if ui.editors.session(BROWSER).map(|s| s.note.id) == Some(nid) {
                ui.editors.update(BROWSER, |s| s.card_id = card);
                return;
            }
            if let Err(err) = crate::editor_window::show_browse_note(ui, nid, card) {
                tracing::warn!(%err, "showing the note failed");
                crate::editor_window::clear_browse_note(ui);
            }
        }
        _ => crate::editor_window::clear_browse_note(ui),
    }
}

// Changes from elsewhere
//////////////////////////////////////////////////////////////////////////

/// Every backend call that changed something (the shell's own; add-ons'
/// operations come through [plugin_op_executed]).
pub fn changes_observed(ui: &Arc<Ui>, changes: &OpChanges) {
    let mut flags = 0;
    if changes.browser_table || changes.note_text || changes.card || changes.notetype {
        flags |= CHANGED_TABLE;
    }
    if changes.browser_sidebar {
        flags |= CHANGED_SIDEBAR;
    }
    // an editor's own save needn't reload it
    if (changes.note_text || changes.notetype) && !crate::editor::in_editor_save() {
        flags |= CHANGED_NOTE;
    }
    if flags == 0 || !is_open(ui) {
        return;
    }
    ui.browser.pending.fetch_or(flags, Ordering::SeqCst);
    if ui.browser.refresh_scheduled.swap(true, Ordering::SeqCst) {
        return;
    }
    // after the call that changed it, and the ones right after (typing)
    let u = ui.clone();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(40));
        u.browser.refresh_scheduled.store(false, Ordering::SeqCst);
        let flags = u.browser.pending.swap(0, Ordering::SeqCst);
        refresh(&u, flags);
    });
}

pub fn plugin_op_executed(ui: &Arc<Ui>, changes: &OpChanges) {
    changes_observed(ui, changes);
}

fn refresh(ui: &Arc<Ui>, flags: u32) {
    if !is_open(ui) {
        return;
    }
    if flags & CHANGED_TABLE != 0 {
        push(ui, json!({"type": "refresh"}));
    }
    if flags & CHANGED_SIDEBAR != 0 {
        if let Ok(tree) = browser::sidebar(&ui.shell.api, &ui.main.tr) {
            push(ui, json!({"type": "sidebar", "sidebar": tree}));
        }
    }
    if flags & CHANGED_NOTE != 0 {
        crate::editor_window::note_changed_elsewhere(ui);
    }
}

// Bridge commands
//////////////////////////////////////////////////////////////////////////

#[derive(Deserialize)]
struct Rows {
    gen: u64,
    start: usize,
    end: usize,
}

#[derive(Deserialize)]
struct Selection {
    gen: u64,
    ranges: Vec<(usize, usize)>,
    current: Option<usize>,
}

#[derive(Deserialize)]
struct Popup {
    x: f64,
    y: f64,
    #[serde(default)]
    item: Value,
    #[serde(default)]
    menu: String,
}

pub fn register(ui: &Arc<Ui>) {
    let bridge = &ui.shell.bridge;
    // only the browser's page (and its table frame) may send these
    let reg = |name: &str, f: fn(&Arc<Ui>, &str) -> Value| {
        let u = ui.clone();
        bridge.register(name, move |_, label, args| {
            if label == BROWSER && is_open(&u) {
                f(&u, args)
            } else {
                tracing::warn!(label, "browser command not from the browser");
                Value::Null
            }
        });
    };
    reg("browserReady", |ui, _| init_json(ui));
    reg("browserSearch", |ui, text| {
        let text = text.replace('\n', " ");
        let api = &ui.shell.api;
        let (search, prompt) = if text.trim().is_empty() {
            (browser::default_search(api), String::new())
        } else {
            match browser::normalize(api, browser::text_node(&text)) {
                Ok(s) => (s.clone(), s),
                Err(err) => {
                    crate::main_window::message(ui, &err.message);
                    return json!({"error": err.message});
                }
            }
        };
        if !prompt.is_empty() {
            remember_search(ui, &prompt);
        }
        let out = search_for(ui, &search, Some(&prompt));
        json!({"ok": out.get("error").is_none(), "history": history(ui)})
    });
    reg("browserRows", |ui, args| {
        let Ok(req) = serde_json::from_str::<Rows>(args) else {
            return Value::Null;
        };
        let hooks = HostHooks::new(ui);
        let api = &ui.shell.api;
        with_state(ui, |s| {
            if s.gen != req.gen {
                return json!({"gen": s.gen, "stale": true});
            }
            s.sync_backend_columns(api);
            json!({"gen": s.gen, "start": req.start, "rows": s.rows(api, &hooks, req.start, req.end)})
        })
        .unwrap_or(Value::Null)
    });
    reg("browserSelect", |ui, args| {
        let Ok(sel) = serde_json::from_str::<Selection>(args) else {
            return json!(false);
        };
        let changed =
            with_state(ui, |s| s.set_selection(sel.gen, &sel.ranges, sel.current)).unwrap_or(false);
        if changed {
            selection_changed(ui);
        }
        json!(changed)
    });
    reg("browserSort", |ui, key| {
        let hooks = HostHooks::new(ui);
        let api = &ui.shell.api;
        let out = with_state(ui, |s| {
            s.sync_backend_columns(api);
            s.sort_by(api, &hooks, key, &ui.main.tr)
                .map(|()| table_message(ui, s))
        });
        match out {
            Some(Ok(msg)) => {
                push(ui, msg);
                selection_changed(ui);
                json!(true)
            }
            Some(Err(err)) => {
                crate::main_window::message(ui, &err);
                json!(false)
            }
            None => Value::Null,
        }
    });
    reg("browserAction", |ui, action| {
        if ui.editors.session(BROWSER).is_some() {
            // `ensure_editor_saved`: a late save mustn't undo the action
            ui.after_saved(BROWSER, &format!("browserActionSaved:{action}"));
        } else {
            let (u, action) = (ui.clone(), action.to_string());
            // actions may open dialogs and wait; not on the bridge
            std::thread::spawn(move || run_action(&u, &action));
        }
        Value::Null
    });
    let u = ui.clone();
    bridge.register("browserActionSaved", move |shell, label, args| {
        let key = crate::app::pending_key(label, &format!("browserActionSaved:{args}"));
        if shell.bridge.take_pending(&key) {
            // `<action>:<token>`; actions may have a colon (`flag:1`)
            let action = args.rsplit_once(':').map_or(args, |(a, _)| a).to_string();
            let u = u.clone();
            std::thread::spawn(move || run_action(&u, &action));
        }
        Value::Null
    });
    reg("browserTable", |ui, _| {
        with_state(ui, |s| table_message(ui, s)).unwrap_or(Value::Null)
    });
    reg("browserPopup", |ui, args| {
        if let Ok(popup) = serde_json::from_str::<Popup>(args) {
            popup_menu(ui, popup);
        }
        Value::Null
    });
    reg("browserSidebarClick", |ui, args| {
        let v: Value = serde_json::from_str(args).unwrap_or(Value::Null);
        let item_search = v["search"].as_str().unwrap_or_default();
        if item_search.is_empty() {
            return Value::Null;
        }
        let mods: browser::Modifiers =
            serde_json::from_value(v["mods"].clone()).unwrap_or_default();
        let current = ui.browser.snapshot.lock().unwrap().search.clone();
        match browser::combine_search(&ui.shell.api, &current, item_search, mods) {
            Ok(search) => search_for(ui, &search, None),
            Err(err) => {
                crate::main_window::message(ui, &err.message);
                Value::Null
            }
        }
    });
    reg("browserSidebarExpand", |ui, args| {
        let v: Value = serde_json::from_str(args).unwrap_or(Value::Null);
        set_expanded(ui, &v["item"], v["expanded"].as_bool().unwrap_or(true));
        Value::Null
    });
    reg("browserColumnWidths", |ui, args| {
        if let Ok(widths) = serde_json::from_str::<HashMap<String, f64>>(args) {
            let notes = ui.browser.snapshot.lock().unwrap().notes_mode;
            {
                let mut settings = ui.settings.lock().unwrap();
                let saved = if notes {
                    &mut settings.browser.note_widths
                } else {
                    &mut settings.browser.card_widths
                };
                saved.extend(widths);
            }
            ui.save_settings_now();
        }
        Value::Null
    });
    reg("browserSidebarWidth", |ui, width| {
        if let Ok(width) = width.parse() {
            ui.settings.lock().unwrap().browser.sidebar = width;
            ui.save_settings_now();
        }
        Value::Null
    });
    reg("browserSplit", |ui, width| {
        if let Ok(width) = width.parse() {
            ui.settings.lock().unwrap().browser.split = width;
            ui.save_settings_now();
        }
        Value::Null
    });
    // the duplicates window
    let u = ui.clone();
    bridge.route_prefix(DUPES, move |_, _, cmd| {
        // test scripts in these windows report through the test channel
        if u.test_mode && cmd.starts_with("test") {
            return None;
        }
        let (name, rest) = cmd.split_once(':').unwrap_or((cmd, ""));
        match name {
            "dupeSearch" => {
                let nids = rest
                    .parse::<usize>()
                    .ok()
                    .and_then(|i| u.browser.dupes.lock().unwrap().get(i).map(|d| d.1.clone()));
                if let Some(nids) = nids {
                    open_with_search(
                        &u,
                        Filter::Nids(anki_proto::search::search_node::IdList { ids: nids }),
                    );
                }
            }
            "dupeTag" => {
                let u = u.clone();
                std::thread::spawn(move || tag_dupes(&u));
            }
            _ => return None,
        }
        Some(Value::Null)
    });
    // the previewer's page and card; card JS gets nothing else
    let u = ui.clone();
    bridge.route_prefix(PREVIEW, move |_, _, cmd| {
        // test scripts in these windows report through the test channel
        if u.test_mode && cmd.starts_with("test") {
            return None;
        }
        let (name, rest) = cmd.split_once(':').unwrap_or((cmd, ""));
        match name {
            "previewReady" => render_preview(&u),
            "previewNext" => preview_step(&u, true),
            "previewBack" => preview_step(&u, false),
            "previewBoth" => {
                browser::set_config(&u.shell.api, "previewBothSides", &json!(rest == "true"));
                render_preview(&u);
            }
            "previewClose" => {
                if let Some(w) = u.app.get_webview_window(PREVIEW) {
                    let _ = w.destroy();
                }
            }
            "play" => play_preview_sound(&u, rest),
            "previewReplay" => {
                let side = if u.browser.preview.lock().unwrap().shown_answer {
                    "a"
                } else {
                    "q"
                };
                play_preview_sound(&u, &format!("{side}:0"));
            }
            _ => {}
        }
        Some(Value::Null)
    });
    let u = ui.clone();
    ui.shell
        .register_frontend("search_in_browser", move |_, _, input| {
            let node = anki_proto::search::SearchNode::decode(input).map_err(|e| e.to_string())?;
            let search = browser::normalize(&u.shell.api, node).map_err(|e| e.message)?;
            let u = u.clone();
            std::thread::spawn(move || open(&u, Some(search), None));
            Ok(vec![])
        });
    let u = ui.clone();
    ui.shell
        .register_frontend("change_notetype", move |_, label, input| {
            if label != CHANGE_NOTETYPE {
                return Err("not from the change notetype window".into());
            }
            let mut request = anki_proto::notetypes::ChangeNotetypeRequest::decode(input)
                .map_err(|e| e.to_string())?;
            request.note_ids = u.browser.change_notetype.lock().unwrap().clone();
            let u = u.clone();
            std::thread::spawn(move || change_notetype_saved(&u, request));
            Ok(vec![])
        });
    let u = ui.clone();
    ui.shell
        .api
        .add_changes_observer(Box::new(move |_, changes| {
            if let Ok(changes) = OpChanges::decode(changes) {
                changes_observed(&u, &changes);
            }
        }));
}

fn history(ui: &Ui) -> Vec<String> {
    ui.settings.lock().unwrap().browser.history.clone()
}

/// `Browser.update_history`.
fn remember_search(ui: &Ui, search: &str) {
    {
        let mut settings = ui.settings.lock().unwrap();
        let history = &mut settings.browser.history;
        history.retain(|s| s != search);
        history.insert(0, search.to_string());
        history.truncate(HISTORY_SIZE);
    }
    ui.save_settings_now();
}

/// What the table's page starts with.
fn init_json(ui: &Arc<Ui>) -> Value {
    let api = &ui.shell.api;
    let tr = &ui.main.tr;
    let sidebar = browser::sidebar(api, tr).unwrap_or_default();
    let settings = ui.settings.lock().unwrap().browser.clone();
    let menus: Vec<String> = ui
        .browser
        .menus
        .lock()
        .unwrap()
        .iter()
        .map(|(t, _)| t.clone())
        .collect();
    with_state(ui, |s| {
        json!({
            "table": s.table_json(tr),
            "selection": s.selection_json(),
            "prompt": s.search_prompt,
            "sidebar": sidebar,
            "history": settings.history,
            "cardWidths": settings.card_widths,
            "noteWidths": settings.note_widths,
            "sidebarWidth": settings.sidebar,
            "addonMenus": menus,
            "shortcuts": shortcuts_json(false),
            "rowHeight": row_height(api),
            "labels": {
                "search": tr.browsing_search_bar_hint(),
                "sidebarFilter": tr.browsing_sidebar_filter(),
                "cards": tr.browsing_cards(),
                "notes": tr.browsing_notes(),
                "toggleMode": tr.browsing_toggle_showing_cards_notes(),
            },
        })
    })
    .unwrap_or(Value::Null)
}

/// `Table._update_font`: rows tall enough for any template's browser font.
fn row_height(api: &crate::api::Api) -> u32 {
    let names: anki_proto::notetypes::NotetypeNames = api
        .call("get_notetype_names", &generic::Empty {})
        .unwrap_or_default();
    let biggest = names
        .entries
        .iter()
        .filter_map(|e| crate::editor::notetype_json(api, e.id).ok())
        .flat_map(|nt| {
            nt["tmpls"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .map(|t| t["bsize"].as_u64().unwrap_or(0))
        })
        .max()
        .unwrap_or(0);
    biggest.max(16) as u32 + 6
}

// Menus
//////////////////////////////////////////////////////////////////////////

fn menu_item(
    ui: &Ui,
    action: &str,
    title: impl Into<String>,
) -> tauri::Result<Box<dyn IsMenuItem<Wry>>> {
    let key = SHORTCUTS
        .iter()
        .find(|(a, _, _)| *a == action)
        .map(|(_, key, _)| key.replace("Cmd", "CmdOrCtrl"));
    let mut b = MenuItemBuilder::with_id(format!("{MENU_PREFIX}{action}"), title.into());
    if let Some(key) = key {
        // shown, not a key equivalent (the page handles the keys)
        b = b.accelerator(key);
    }
    let item = b.build(&ui.app)?;
    Ok(Box::new(item))
}

/// The Cards and Notes menus, as the table's context menu
/// (`Table._on_context_menu`).
fn actions_menu(ui: &Ui, flag: u32) -> tauri::Result<Menu<Wry>> {
    let tr = &ui.main.tr;
    let app = &ui.app;
    let names = browser::flag_names(&ui.shell.api, tr);
    let mut flags = SubmenuBuilder::new(app, tr.studying_flag_card());
    for (i, name) in names.iter().enumerate() {
        let n = i as u32 + 1;
        flags = flags.item(
            &CheckMenuItemBuilder::with_id(format!("{MENU_PREFIX}flag:{n}"), name)
                .checked(flag == n)
                .accelerator(format!("CmdOrCtrl+{n}"))
                .build(app)?,
        );
    }
    let flags = flags.build()?;
    let cards: Vec<Box<dyn IsMenuItem<Wry>>> = vec![
        menu_item(ui, "changeDeck", tr.browsing_change_deck2())?,
        menu_item(ui, "setDueDate", plain(tr.qt_accel_set_due_date()))?,
        menu_item(ui, "forget", plain(tr.qt_accel_forget()))?,
        menu_item(ui, "reposition", tr.browsing_reposition())?,
        menu_item(ui, "suspend", tr.browsing_toggle_suspend())?,
        menu_item(ui, "bury", tr.browsing_toggle_bury())?,
        menu_item(ui, "info", plain(tr.qt_accel_info()))?,
        menu_item(ui, "preview", tr.actions_preview())?,
    ];
    let notes: Vec<Box<dyn IsMenuItem<Wry>>> = vec![
        menu_item(ui, "addNotes", tr.browsing_add_notes())?,
        menu_item(ui, "addTags", tr.browsing_add_tags2())?,
        menu_item(ui, "removeTags", tr.browsing_remove_tags())?,
        menu_item(ui, "clearUnusedTags", tr.browsing_clear_unused_tags())?,
        menu_item(ui, "mark", tr.browsing_toggle_mark())?,
        menu_item(ui, "changeNotetype", tr.browsing_change_notetype())?,
        menu_item(ui, "findDupes", plain(tr.qt_accel_find_duplicates()))?,
        menu_item(ui, "findReplace", plain(tr.qt_accel_find_and_replace()))?,
        menu_item(ui, "delete", tr.actions_delete())?,
    ];
    let other: Vec<Box<dyn IsMenuItem<Wry>>> = vec![
        menu_item(ui, "selectNotes", plain(tr.qt_accel_select_notes()))?,
        menu_item(ui, "toggleMode", tr.browsing_toggle_showing_cards_notes())?,
    ];
    let notes_menu = SubmenuBuilder::new(app, plain(tr.qt_accel_notes())).build()?;
    for item in &notes {
        notes_menu.append(item.as_ref())?;
    }
    let menu = Menu::new(app)?;
    menu.append(&flags)?;
    for item in &cards {
        menu.append(item.as_ref())?;
    }
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    menu.append(&notes_menu)?;
    menu.append(&PredefinedMenuItem::separator(app)?)?;
    for item in &other {
        menu.append(item.as_ref())?;
    }
    // add-ons' (`browser_will_show_context_menu` isn't needed by any)
    Ok(menu)
}

/// The header's menu: every column, checked if shown
/// (`Table._on_header_context`).
fn columns_menu(ui: &Ui) -> Option<tauri::Result<Menu<Wry>>> {
    let tr = &ui.main.tr;
    let items = with_state(ui, |s| {
        let mut columns: Vec<(String, String, bool)> = s
            .known_columns()
            .iter()
            .map(|c| {
                (
                    c.key.clone(),
                    c.label(s.notes_mode).to_string(),
                    s.columns.contains(&c.key),
                )
            })
            .collect();
        // shown add-on columns nobody describes now
        for key in &s.columns {
            if !columns.iter().any(|c| &c.0 == key) {
                columns.push((
                    key.clone(),
                    s.column(key, tr).label(s.notes_mode).into(),
                    true,
                ));
            }
        }
        columns.sort_by_key(|c| c.1.to_lowercase());
        columns
    })?;
    Some((|| {
        let menu = Menu::new(&ui.app)?;
        for (key, label, active) in items {
            menu.append(
                &CheckMenuItemBuilder::with_id(format!("{MENU_PREFIX}column:{key}"), label)
                    .checked(active)
                    .build(&ui.app)?,
            )?;
        }
        Ok(menu)
    })())
}

/// A sidebar item's menu (`SidebarTreeView.show_context_menu`).
fn sidebar_menu(ui: &Ui, item: &Value) -> tauri::Result<Menu<Wry>> {
    let tr = &ui.main.tr;
    let app = &ui.app;
    let menu = Menu::new(app)?;
    let add = |id: &str, title: String| -> tauri::Result<()> {
        menu.append(
            &MenuItemBuilder::with_id(format!("{MENU_PREFIX}sidebar:{id}"), title).build(app)?,
        )
    };
    let kind = item["kind"].as_str().unwrap_or_default();
    if !item["search"].as_str().unwrap_or_default().is_empty() {
        add("search", tr.actions_search().into_owned())?;
        add("searchAnd", format!("{} (Cmd+click)", tr.actions_search()))?;
        add(
            "searchOr",
            format!("{} OR (Shift+click)", tr.actions_search()),
        )?;
        add(
            "searchNot",
            format!("{} NOT (Option+click)", tr.actions_search()),
        )?;
    }
    match kind {
        "savedSearch" => {
            add("renameSaved", tr.actions_rename().into_owned())?;
            add("deleteSaved", tr.actions_delete().into_owned())?;
        }
        "deck" | "filteredDeck" | "tag" => {
            add("rename", tr.actions_rename().into_owned())?;
            add("delete", tr.actions_delete().into_owned())?;
        }
        _ => {}
    }
    if kind == "savedSearch" || kind == "savedSearchRoot" {
        add(
            "save",
            tr.browsing_sidebar_save_current_search().into_owned(),
        )?;
    }
    Ok(menu)
}

/// An add-on's menu (`browser.form.menubar.addMenu`).
fn addon_menu(ui: &Ui, title: &str) -> Option<tauri::Result<Menu<Wry>>> {
    let items = ui
        .browser
        .menus
        .lock()
        .unwrap()
        .iter()
        .find(|(t, _)| t == title)
        .map(|(_, items)| items.clone())?;
    Some((|| {
        let menu = Menu::new(&ui.app)?;
        for item in items {
            menu.append(
                &MenuItemBuilder::with_id(format!("addon:{}", item.id), &item.text)
                    .enabled(item.enabled)
                    .build(&ui.app)?,
            )?;
        }
        Ok(menu)
    })())
}

fn popup_menu(ui: &Arc<Ui>, popup: Popup) {
    let menu = match popup.menu.as_str() {
        "actions" => {
            let api = &ui.shell.api;
            let flag = with_state(ui, |s| s.current_card(api))
                .flatten()
                .and_then(|cid| {
                    api.call::<anki_proto::cards::Card>(
                        "get_card",
                        &anki_proto::cards::CardId { cid },
                    )
                    .ok()
                })
                .map(|c| c.flags & 0b111)
                .unwrap_or(0);
            Some(actions_menu(ui, flag))
        }
        "columns" => columns_menu(ui),
        "sidebar" => {
            *ui.browser.sidebar_item.lock().unwrap() = popup.item.clone();
            Some(sidebar_menu(ui, &popup.item))
        }
        title => addon_menu(ui, title.strip_prefix("addon:").unwrap_or(title)),
    };
    let Some(menu) = menu.and_then(|m| m.ok()) else {
        return;
    };
    let Some(window) = ui.app.get_webview_window(BROWSER) else {
        return;
    };
    let w = window.clone();
    let pos = tauri::LogicalPosition::new(popup.x, popup.y);
    // AppKit only on the main thread
    let _ = window.run_on_main_thread(move || {
        if let Err(err) = w.popup_menu_at(&menu, pos) {
            tracing::warn!(%err, "showing a browser menu failed");
        }
    });
}

/// A browser menu item was chosen (`browser:<action>`).
pub fn menu_event(ui: &Arc<Ui>, id: &str) {
    if let Some(key) = id.strip_prefix("column:") {
        let api = &ui.shell.api;
        let out = with_state(ui, |s| {
            s.toggle_column(api, key, &ui.main.tr)
                .map(|()| table_message(ui, s))
        });
        match out {
            Some(Ok(msg)) => push(ui, msg),
            Some(Err(err)) => crate::main_window::message(ui, &err),
            None => {}
        }
    } else if let Some(op) = id.strip_prefix("sidebar:") {
        let item = ui.browser.sidebar_item.lock().unwrap().clone();
        sidebar_action(ui, op, &item);
    } else {
        run_action(ui, id);
    }
}

// Sidebar
//////////////////////////////////////////////////////////////////////////

fn set_expanded(ui: &Ui, item: &Value, expanded: bool) {
    let api = &ui.shell.api;
    let kind = item["kind"].as_str().unwrap_or_default();
    if let Some((_, key)) = browser::SECTION_KEYS.iter().find(|(k, _)| *k == kind) {
        browser::set_config(api, key, &json!(!expanded));
        return;
    }
    match kind {
        "deck" | "filteredDeck" => {
            let _: Result<OpChanges, _> = api.call(
                "set_deck_collapsed",
                &anki_proto::decks::SetDeckCollapsedRequest {
                    deck_id: item["id"].as_i64().unwrap_or(0),
                    collapsed: !expanded,
                    scope: anki_proto::decks::set_deck_collapsed_request::Scope::Browser as i32,
                },
            );
        }
        "tag" => {
            let _: Result<OpChanges, _> = api.call(
                "set_tag_collapsed",
                &anki_proto::tags::SetTagCollapsedRequest {
                    name: item["fullName"].as_str().unwrap_or_default().into(),
                    collapsed: !expanded,
                },
            );
        }
        _ => {}
    }
}

fn sidebar_action(ui: &Arc<Ui>, op: &str, item: &Value) {
    let api = &ui.shell.api;
    let tr = &ui.main.tr;
    let search = item["search"].as_str().unwrap_or_default();
    let name = item["fullName"].as_str().unwrap_or_default().to_string();
    let kind = item["kind"].as_str().unwrap_or_default().to_string();
    let mods = |cmd, shift, alt| browser::Modifiers { cmd, shift, alt };
    let combine = |m: browser::Modifiers| {
        let current = ui.browser.snapshot.lock().unwrap().search.clone();
        match browser::combine_search(api, &current, search, m) {
            Ok(s) => {
                search_for(ui, &s, None);
            }
            Err(err) => crate::main_window::message(ui, &err.message),
        }
    };
    match op {
        "search" => combine(mods(false, false, false)),
        "searchAnd" => combine(mods(true, false, false)),
        "searchOr" => combine(mods(false, true, false)),
        "searchNot" => combine(mods(true, false, true)),
        "save" => {
            let current = ui.browser.snapshot.lock().unwrap().search.clone();
            form(
                ui,
                &tr.browsing_sidebar_save_current_search(),
                "",
                vec![FormField::text(
                    "name",
                    &tr.browsing_please_give_your_filter_a_name(),
                    "",
                )],
                move |ui, answer| {
                    let name = answer.fields.get("name").cloned().unwrap_or_default();
                    let name = name.trim();
                    if name.is_empty() {
                        return;
                    }
                    let name = name.to_string();
                    confirm_overwrite(ui, &name.clone(), move |ui| {
                        let api = &ui.shell.api;
                        let mut saved = browser::saved_searches(api);
                        saved.retain(|(n, _)| *n != name);
                        saved.push((name, current));
                        browser::set_saved_searches(api, &saved);
                        refresh(ui, CHANGED_SIDEBAR);
                    });
                },
            );
        }
        "renameSaved" => form(
            ui,
            &tr.actions_rename(),
            "",
            vec![FormField::text("name", &tr.actions_new_name(), &name)],
            move |ui, answer| {
                let new = answer.fields.get("name").cloned().unwrap_or_default();
                let new = new.trim();
                if new.is_empty() || new == name {
                    return;
                }
                let api = &ui.shell.api;
                let saved = browser::saved_searches(api);
                let Some(search) = saved.iter().find(|(n, _)| *n == name).map(|e| e.1.clone())
                else {
                    return;
                };
                let (old, new) = (name.clone(), new.to_string());
                confirm_overwrite(ui, &new.clone(), move |ui| {
                    let api = &ui.shell.api;
                    let mut saved = browser::saved_searches(api);
                    saved.retain(|(n, _)| *n != old && *n != new);
                    saved.push((new, search));
                    browser::set_saved_searches(api, &saved);
                    refresh(ui, CHANGED_SIDEBAR);
                });
            },
        ),
        "deleteSaved" => {
            let mut saved = browser::saved_searches(api);
            saved.retain(|(n, _)| *n != name);
            browser::set_saved_searches(api, &saved);
            refresh(ui, CHANGED_SIDEBAR);
        }
        "rename" => {
            let id = item["id"].as_i64().unwrap_or(0);
            let title = if kind == "tag" {
                tr.actions_rename_tag()
            } else {
                tr.actions_rename_deck()
            };
            form(
                ui,
                &title,
                "",
                vec![FormField::text("name", &tr.actions_new_name(), &name)],
                move |ui, answer| {
                    let new = answer.fields.get("name").cloned().unwrap_or_default();
                    let new = new.trim().to_string();
                    if new.is_empty() || new == name {
                        return;
                    }
                    let api = &ui.shell.api;
                    let result = if kind == "tag" {
                        api.call::<anki_proto::collection::OpChangesWithCount>(
                            "rename_tags",
                            &anki_proto::tags::RenameTagsRequest {
                                current_prefix: name.clone(),
                                new_prefix: new,
                            },
                        )
                        .map(|out| out.changes.unwrap_or_default())
                    } else {
                        api.call::<OpChanges>(
                            "rename_deck",
                            &anki_proto::decks::RenameDeckRequest {
                                deck_id: id,
                                new_name: new,
                            },
                        )
                    };
                    op_done(ui, result.map(|c| (c, None)));
                },
            );
        }
        "delete" => {
            let result = if kind == "tag" {
                api.call::<anki_proto::collection::OpChangesWithCount>(
                    "remove_tags",
                    &generic::String { val: name },
                )
                .map(|out| {
                    let n = out.count;
                    (
                        out.changes.unwrap_or_default(),
                        Some(tr.browsing_notes_updated(n).into_owned()),
                    )
                })
            } else {
                api.call::<anki_proto::collection::OpChangesWithCount>(
                    "remove_decks",
                    &anki_proto::decks::DeckIds {
                        dids: vec![item["id"].as_i64().unwrap_or(0)],
                    },
                )
                .map(|out| {
                    let n = out.count;
                    (
                        out.changes.unwrap_or_default(),
                        Some(tr.browsing_cards_deleted(n).into_owned()),
                    )
                })
            };
            op_done(ui, result);
        }
        _ => {}
    }
}

/// Run `then` if no saved search is called `name`, or the user agrees to
/// replace it.
fn confirm_overwrite(ui: &Arc<Ui>, name: &str, then: impl FnOnce(&Arc<Ui>) + Send + 'static) {
    if !browser::saved_searches(&ui.shell.api)
        .iter()
        .any(|(n, _)| n == name)
    {
        return then(ui);
    }
    let tr = &ui.main.tr;
    crate::main_window::ask(
        ui,
        crate::main_window::Dialog {
            title: tr.browsing_sidebar_saved_searches().into_owned(),
            message_html: htmlescape::encode_minimal(
                &tr.browsing_confirm_saved_search_overwrite(name),
            ),
            fields: vec![],
            buttons: vec!["OK".into(), tr.actions_cancel().into_owned()],
            default: 1,
        },
        move |ui, answer| {
            if answer.is_some_and(|a| a.button == 0) {
                then(ui);
            }
        },
    );
}

// Actions
//////////////////////////////////////////////////////////////////////////

fn tooltip(ui: &Ui, msg: &str) {
    crate::editor_window::tooltip(ui, BROWSER, msg);
}

/// After an operation of the browser's: tooltip, menus, the main window
/// and the add-ons (as `CollectionOp` does), then the table.
fn op_done(ui: &Arc<Ui>, result: Result<(OpChanges, Option<String>), crate::api::ApiError>) {
    match result {
        Ok((changes, msg)) => {
            if let Some(msg) = msg {
                tooltip(ui, &msg);
            }
            crate::main_window::plugin_op_executed(ui, &changes);
        }
        Err(err) => crate::main_window::message(ui, &err.message),
    }
}

fn selection(ui: &Ui) -> Option<(Vec<i64>, Vec<i64>, Option<i64>)> {
    let api = &ui.shell.api;
    with_state(ui, |s| {
        (
            s.selected_card_ids(api),
            s.selected_note_ids(api),
            s.current_card(api),
        )
    })
}

fn current_card(ui: &Ui) -> Option<anki_proto::cards::Card> {
    let (_, _, current) = selection(ui)?;
    ui.shell
        .api
        .call("get_card", &anki_proto::cards::CardId { cid: current? })
        .ok()
}

pub fn run_action(ui: &Arc<Ui>, action: &str) {
    let api = &ui.shell.api;
    let tr = &ui.main.tr;
    let Some((cards, notes, _)) = selection(ui) else {
        return;
    };
    let with_count = |method: &str,
                      msg: Option<fn(&anki::prelude::I18n, u32) -> String>,
                      input: &dyn Fn() -> Vec<u8>| {
        api.raw(method, &input())
            .and_then(|out| {
                anki_proto::collection::OpChangesWithCount::decode(out.as_slice()).map_err(|e| {
                    crate::api::ApiError {
                        kind: -1,
                        message: e.to_string(),
                    }
                })
            })
            .map(|out| {
                let text = msg.map(|f| f(tr, out.count));
                (out.changes.unwrap_or_default(), text)
            })
    };
    match action {
        "close" => close(ui),
        "undo" | "redo" => {
            let redo = action == "redo";
            let out =
                api.call::<anki_proto::collection::OpChangesAfterUndo>(action, &generic::Empty {});
            op_done(
                ui,
                out.map(|out| {
                    let msg = if redo {
                        tr.undo_redo_action(out.operation).into_owned()
                    } else {
                        tr.undo_action_undone(out.operation).into_owned()
                    };
                    (out.changes.unwrap_or_default(), Some(msg))
                }),
            );
            crate::editor_window::note_changed_elsewhere(ui);
        }
        "toggleMode" => {
            let hooks = HostHooks::new(ui);
            let out = with_state(ui, |s| {
                s.toggle_mode(api, &hooks, tr)
                    .map(|()| table_message(ui, s))
            });
            match out {
                Some(Ok(msg)) => {
                    push(ui, msg);
                    selection_changed(ui);
                }
                Some(Err(err)) => crate::main_window::message(ui, &err.message),
                None => {}
            }
        }
        "selectNotes" => {
            if notes.is_empty() {
                return;
            }
            let search = browser::normalize(
                api,
                browser::node(Filter::Nids(anki_proto::search::search_node::IdList {
                    ids: notes,
                })),
            );
            if let Ok(search) = search {
                search_for(ui, &search, None);
                if let Some(sel) = with_state(ui, |s| {
                    let gen = s.gen;
                    let len = s.items.len();
                    s.set_selection(gen, &[(0, len)], Some(0));
                    s.selection_json()
                }) {
                    push(ui, json!({"type": "selection", "selection": sel}));
                    selection_changed(ui);
                }
            }
        }
        "addNotes" => {
            if let Err(err) = crate::editor_window::open_add(ui) {
                crate::main_window::message(ui, &format!("{err:#}"));
            }
        }
        _ if cards.is_empty() => {}
        "suspend" | "bury" => {
            let Some(card) = current_card(ui) else {
                return;
            };
            let (queue, mode) = if action == "suspend" {
                (
                    -1,
                    anki_proto::scheduler::bury_or_suspend_cards_request::Mode::Suspend,
                )
            } else {
                (
                    -3,
                    anki_proto::scheduler::bury_or_suspend_cards_request::Mode::BuryUser,
                )
            };
            // `queue == -2` is buried by the scheduler, also lifted by unburying
            let on = card.queue == queue || (action == "bury" && card.queue == -2);
            let result = if on {
                api.call::<OpChanges>(
                    "restore_buried_and_suspended_cards",
                    &anki_proto::cards::CardIds { cids: cards },
                )
                .map(|c| (c, None))
            } else {
                with_count("bury_or_suspend_cards", None, &|| {
                    anki_proto::scheduler::BuryOrSuspendCardsRequest {
                        card_ids: cards.clone(),
                        note_ids: vec![],
                        mode: mode as i32,
                    }
                    .encode_to_vec()
                })
            };
            op_done(ui, result);
        }
        flag if flag.starts_with("flag:") => {
            let mut flag: u32 = flag[5..].parse().unwrap_or(0);
            // the current card's flag again turns it off
            if current_card(ui).is_some_and(|c| c.flags & 0b111 == flag) {
                flag = 0;
            }
            op_done(
                ui,
                with_count("set_flag", None, &|| {
                    anki_proto::cards::SetFlagRequest {
                        card_ids: cards.clone(),
                        flag,
                    }
                    .encode_to_vec()
                }),
            );
        }
        "mark" => {
            let Some(card) = current_card(ui) else {
                return;
            };
            let note: anki_proto::notes::Note =
                match api.call("get_note", &anki_proto::notes::NoteId { nid: card.note_id }) {
                    Ok(n) => n,
                    Err(_) => return,
                };
            let marked = note.tags.iter().any(|t| t.eq_ignore_ascii_case("marked"));
            let method = if marked {
                "remove_note_tags"
            } else {
                "add_note_tags"
            };
            op_done(
                ui,
                with_count(method, None, &|| {
                    anki_proto::tags::NoteIdsAndTagsRequest {
                        note_ids: notes.clone(),
                        tags: "marked".into(),
                    }
                    .encode_to_vec()
                }),
            );
        }
        "delete" => {
            // `to_row_of_unselected_note` happens as the rows go grey;
            // the table keeps them until the next search, as in Qt
            op_done(
                ui,
                with_count(
                    "remove_notes",
                    Some(|tr, n| tr.browsing_cards_deleted(n).into_owned()),
                    &|| {
                        anki_proto::notes::RemoveNotesRequest {
                            note_ids: notes.clone(),
                            card_ids: vec![],
                        }
                        .encode_to_vec()
                    },
                ),
            );
        }
        "clearUnusedTags" => op_done(
            ui,
            with_count(
                "clear_unused_tags",
                Some(|tr, n| tr.browsing_removed_unused_tags_count(n).into_owned()),
                &|| generic::Empty {}.encode_to_vec(),
            ),
        ),
        "addTags" | "removeTags" => {
            let add = action == "addTags";
            let title = if add {
                tr.browsing_add_tags2()
            } else {
                tr.browsing_remove_tags()
            };
            form(
                ui,
                &title.replace("...", ""),
                "",
                vec![FormField::text("tags", &tr.editing_tags(), "")],
                move |ui, answer| {
                    let tags = answer.fields.get("tags").cloned().unwrap_or_default();
                    if tags.trim().is_empty() {
                        return;
                    }
                    let method = if add {
                        "add_note_tags"
                    } else {
                        "remove_note_tags"
                    };
                    let out = ui
                        .shell
                        .api
                        .call::<anki_proto::collection::OpChangesWithCount>(
                            method,
                            &anki_proto::tags::NoteIdsAndTagsRequest {
                                note_ids: notes,
                                tags,
                            },
                        )
                        .map(|out| {
                            let n = out.count;
                            (
                                out.changes.unwrap_or_default(),
                                Some(ui.main.tr.browsing_notes_updated(n).into_owned()),
                            )
                        });
                    op_done(ui, out);
                },
            );
        }
        "changeDeck" => change_deck(ui, cards),
        "setDueDate" => set_due_date(ui, cards),
        "forget" => forget(ui, cards),
        "reposition" => reposition(ui, cards),
        "findReplace" => find_replace(ui, notes),
        "findDupes" => find_dupes_dialog(ui),
        "changeNotetype" => change_notetype(ui, notes),
        "info" => {
            if let Some(card) = current_card(ui) {
                open_card_info(ui, card.id);
            }
        }
        "preview" => toggle_preview(ui),
        other => tracing::warn!(other, "unknown browser action"),
    }
}

fn change_deck(ui: &Arc<Ui>, cards: Vec<i64>) {
    let api = &ui.shell.api;
    let Ok(names) = api.call::<anki_proto::decks::DeckNames>(
        "get_deck_names",
        &anki_proto::decks::GetDeckNamesRequest {
            skip_empty_default: true,
            include_filtered: false,
        },
    ) else {
        return;
    };
    let decks: Vec<(String, String)> = names
        .entries
        .into_iter()
        .map(|d| (d.id.to_string(), d.name))
        .collect();
    let current = current_card(ui)
        .map(|c| c.deck_id.to_string())
        .unwrap_or_default();
    let title = ui.main.tr.browsing_change_deck2().replace("...", "");
    crate::main_window::pick(ui, &title, &decks, &current, move |ui, did| {
        let Some(did) = did.and_then(|d| d.parse::<i64>().ok()) else {
            return;
        };
        let out = ui
            .shell
            .api
            .call::<anki_proto::collection::OpChangesWithCount>(
                "set_deck",
                &anki_proto::cards::SetDeckRequest {
                    card_ids: cards,
                    deck_id: did,
                },
            )
            .map(|out| (out.changes.unwrap_or_default(), None));
        op_done(ui, out);
    });
}

fn set_due_date(ui: &Arc<Ui>, cards: Vec<i64>) {
    let tr = &ui.main.tr;
    let api = &ui.shell.api;
    let default = browser::get_config(api, "setDueBrowser")
        .and_then(|v| v.as_str().map(str::to_string))
        .unwrap_or_default();
    form(
        ui,
        &tr.actions_set_due_date(),
        &htmlescape::encode_minimal(&tr.scheduling_set_due_date_prompt(cards.len() as u32)),
        vec![FormField::text(
            "days",
            &tr.scheduling_set_due_date_prompt_hint(),
            &default,
        )],
        move |ui, answer| {
            let days = answer.fields.get("days").cloned().unwrap_or_default();
            if days.trim().is_empty() {
                return;
            }
            let out = ui
                .shell
                .api
                .call::<OpChanges>(
                    "set_due_date",
                    &anki_proto::scheduler::SetDueDateRequest {
                        card_ids: cards,
                        days,
                        config_key: Some(anki_proto::config::OptionalStringConfigKey {
                            key: anki_proto::config::config_key::String::SetDueBrowser as i32,
                        }),
                    },
                )
                .map(|c| (c, None));
            op_done(ui, out);
        },
    );
}

fn forget(ui: &Arc<Ui>, cards: Vec<i64>) {
    let tr = &ui.main.tr;
    let defaults: anki_proto::scheduler::ScheduleCardsAsNewDefaultsResponse = ui
        .shell
        .api
        .call(
            "schedule_cards_as_new_defaults",
            &anki_proto::scheduler::ScheduleCardsAsNewDefaultsRequest {
                context: anki_proto::scheduler::schedule_cards_as_new_request::Context::Browser
                    as i32,
            },
        )
        .unwrap_or_default();
    form(
        ui,
        &plain(tr.qt_accel_forget()),
        "",
        vec![
            FormField::check(
                "restore",
                &tr.scheduling_restore_position(),
                defaults.restore_position,
            ),
            FormField::check(
                "reset",
                &tr.scheduling_reset_counts(),
                defaults.reset_counts,
            ),
        ],
        move |ui, answer| {
            let on = |k: &str| answer.fields.get(k).is_some_and(|v| v == "true");
            let out = ui
                .shell
                .api
                .call::<OpChanges>(
                    "schedule_cards_as_new",
                    &anki_proto::scheduler::ScheduleCardsAsNewRequest {
                        card_ids: cards,
                        log: true,
                        restore_position: on("restore"),
                        reset_counts: on("reset"),
                        context: Some(
                            anki_proto::scheduler::schedule_cards_as_new_request::Context::Browser
                                as i32,
                        ),
                    },
                )
                .map(|c| (c, None));
            op_done(ui, out);
        },
    );
}

fn reposition(ui: &Arc<Ui>, cards: Vec<i64>) {
    let tr = &ui.main.tr;
    let api = &ui.shell.api;
    // only new cards (`reposition_new_cards_dialog`)
    let new = api
        .query(
            &format!(
                "select count() from cards where type = 0 and id in ({})",
                cards
                    .iter()
                    .map(i64::to_string)
                    .collect::<Vec<_>>()
                    .join(",")
            ),
            &[],
        )
        .ok()
        .and_then(|rows| rows.first().and_then(|r| r.first()).and_then(Value::as_u64))
        .unwrap_or(0);
    if new == 0 {
        // rslib leaves cards that aren't new where they are
        return;
    }
    let defaults: anki_proto::scheduler::RepositionDefaultsResponse = api
        .call("reposition_defaults", &generic::Empty {})
        .unwrap_or_default();
    let bounds = api
        .query(
            "select min(due), max(due) from cards where type = 0 and odid = 0",
            &[],
        )
        .ok()
        .and_then(|rows| rows.into_iter().next())
        .unwrap_or_default();
    let bound = |i: usize| {
        bounds
            .get(i)
            .and_then(Value::as_i64)
            .map(|v| v.to_string())
            .unwrap_or_default()
    };
    let message = format!(
        "{}<br>{}",
        htmlescape::encode_minimal(&tr.browsing_queue_top(bound(0))),
        htmlescape::encode_minimal(&tr.browsing_queue_bottom(bound(1)))
    );
    form(
        ui,
        &tr.browsing_reposition().replace("...", ""),
        &message,
        vec![
            FormField::text("start", &tr.browsing_start_position(), "0"),
            FormField::text("step", &tr.browsing_step(), "1"),
            FormField::check("random", &tr.browsing_randomize_order(), defaults.random),
            FormField::check(
                "shift",
                &tr.browsing_shift_position_of_existing_cards(),
                defaults.shift,
            ),
        ],
        move |ui, answer| {
            let num = |k: &str, d: u32| {
                answer
                    .fields
                    .get(k)
                    .and_then(|v| v.trim().parse().ok())
                    .unwrap_or(d)
            };
            let on = |k: &str| answer.fields.get(k).is_some_and(|v| v == "true");
            let out = ui
                .shell
                .api
                .call::<anki_proto::collection::OpChangesWithCount>(
                    "sort_cards",
                    &anki_proto::scheduler::SortCardsRequest {
                        card_ids: cards,
                        starting_from: num("start", 0),
                        step_size: num("step", 1).max(1),
                        randomize: on("random"),
                        shift_existing: on("shift"),
                    },
                )
                .map(|out| (out.changes.unwrap_or_default(), None));
            op_done(ui, out);
        },
    );
}

/// `FindAndReplaceDialog`.
fn find_replace(ui: &Arc<Ui>, notes: Vec<i64>) {
    let tr = &ui.main.tr;
    let api = &ui.shell.api;
    let fields: anki_proto::generic::StringList = api
        .call(
            "field_names_for_notes",
            &anki_proto::notes::FieldNamesForNotesRequest {
                nids: notes.clone(),
            },
        )
        .unwrap_or_default();
    let mut options = vec![
        ("__all".to_string(), tr.browsing_all_fields().into_owned()),
        ("__tags".to_string(), tr.editing_tags().into_owned()),
    ];
    options.extend(fields.vals.into_iter().map(|f| (f.clone(), f)));
    let strip = |s: std::borrow::Cow<str>| s.replace("<b>", "").replace("</b>", "");
    form(
        ui,
        &plain(tr.qt_accel_find_and_replace()).replace("...", ""),
        "",
        vec![
            FormField::text("find", &strip(tr.browsing_find()), ""),
            FormField::text("replace", &strip(tr.browsing_replace_with()), ""),
            FormField::select("field", &strip(tr.browsing_in()), options, "__all"),
            FormField::check("selected", &tr.browsing_selected_notes_only(), true),
            FormField::check("ignoreCase", &tr.browsing_ignore_case(), true),
            FormField::check(
                "regex",
                &tr.browsing_treat_input_as_regular_expression(),
                false,
            ),
        ],
        move |ui, answer| {
            let get = |k: &str| answer.fields.get(k).cloned().unwrap_or_default();
            let on = |k: &str| get(k) == "true";
            let find = get("find");
            if find.is_empty() {
                return;
            }
            let nids = if on("selected") { notes } else { vec![] };
            let api = &ui.shell.api;
            let field = get("field");
            let out = if field == "__tags" {
                api.call::<anki_proto::collection::OpChangesWithCount>(
                    "find_and_replace_tag",
                    &anki_proto::tags::FindAndReplaceTagRequest {
                        note_ids: nids.clone(),
                        search: find,
                        replacement: get("replace"),
                        regex: on("regex"),
                        match_case: !on("ignoreCase"),
                    },
                )
            } else {
                api.call::<anki_proto::collection::OpChangesWithCount>(
                    "find_and_replace",
                    &anki_proto::search::FindAndReplaceRequest {
                        nids: nids.clone(),
                        search: find,
                        replacement: get("replace"),
                        regex: on("regex"),
                        match_case: !on("ignoreCase"),
                        field_name: if field == "__all" {
                            String::new()
                        } else {
                            field
                        },
                    },
                )
            };
            let total = if nids.is_empty() {
                api.query("select count() from notes", &[])
                    .ok()
                    .and_then(|r| r.first().and_then(|r| r.first()).and_then(Value::as_u64))
                    .unwrap_or(0) as u32
            } else {
                nids.len() as u32
            };
            op_done(
                ui,
                out.map(|out| {
                    let msg = ui
                        .main
                        .tr
                        .findreplace_notes_updated(out.count, total)
                        .into_owned();
                    (out.changes.unwrap_or_default(), Some(msg))
                }),
            );
        },
    );
}

/// `FindDuplicatesDialog`: ask for the field, then list the groups.
fn find_dupes_dialog(ui: &Arc<Ui>) {
    let tr = &ui.main.tr;
    let fields: Vec<(String, String)> = browser::all_field_names(&ui.shell.api)
        .into_iter()
        .map(|f| (f.clone(), f))
        .collect();
    let first = fields.first().map(|f| f.0.clone()).unwrap_or_default();
    form(
        ui,
        &plain(tr.qt_accel_find_duplicates()).replace("...", ""),
        "",
        vec![
            FormField::select("field", &tr.browsing_search_in(), fields, &first),
            FormField::text("search", &tr.browsing_optional_filter(), ""),
        ],
        |ui, answer| {
            let get = |k: &str| answer.fields.get(k).cloned().unwrap_or_default();
            match browser::find_dupes(&ui.shell.api, &get("field"), &get("search")) {
                Ok(dupes) => show_dupes(ui, dupes),
                Err(err) => crate::main_window::message(ui, &err.message),
            }
        },
    );
}

fn show_dupes(ui: &Arc<Ui>, dupes: Vec<(String, Vec<i64>)>) {
    let tr = &ui.main.tr;
    let rows: String = dupes
        .iter()
        .enumerate()
        .map(|(i, (value, nids))| {
            format!(
                "<li><a href=# data-i={i}>{}</a>: {}</li>",
                htmlescape::encode_minimal(&tr.browsing_note_count(nids.len() as u32)),
                htmlescape::encode_minimal(value)
            )
        })
        .collect();
    let any = !dupes.is_empty();
    *ui.browser.dupes.lock().unwrap() = dupes;
    let body = format!(
        "<style>body {{ margin: 0; padding: 12px 16px; font-size: 13px; }} \
         ul {{ padding-left: 18px; }} li {{ margin: 4px 0; }}</style>\
         <p><button id=tag {disabled}>{tag}</button></p><ul>{rows}</ul>\
         <script>{{\
           document.querySelectorAll('a[data-i]').forEach((a) => a.addEventListener('click', (e) => {{\
             e.preventDefault(); pycmd('dupeSearch:' + a.dataset.i); }}));\
           document.getElementById('tag').addEventListener('click', () => pycmd('dupeTag'));\
         }}</script>",
        disabled = if any { "" } else { "disabled" },
        tag = htmlescape::encode_minimal(&tr.browsing_tag_duplicates()),
    );
    let html = pages::std_html(&ui.shell.web, &body, &[], &[], true);
    let _ = ui.open_window(
        DUPES,
        &plain(tr.qt_accel_find_duplicates()).replace("...", ""),
        WebviewKind::Dialog,
        Content::Html(html),
    );
}

fn tag_dupes(ui: &Arc<Ui>) {
    let nids: Vec<i64> = ui
        .browser
        .dupes
        .lock()
        .unwrap()
        .iter()
        .flat_map(|d| d.1.iter().copied())
        .collect();
    if nids.is_empty() {
        return;
    }
    let out = ui
        .shell
        .api
        .call::<anki_proto::collection::OpChangesWithCount>(
            "add_note_tags",
            &anki_proto::tags::NoteIdsAndTagsRequest {
                note_ids: nids,
                tags: ui.main.tr.browsing_duplicate().into_owned(),
            },
        )
        .map(|out| {
            let n = out.count;
            (
                out.changes.unwrap_or_default(),
                Some(ui.main.tr.browsing_notes_updated(n).into_owned()),
            )
        });
    op_done(ui, out);
}

/// `change_notetype_dialog`: the stock page, for notes of one notetype.
fn change_notetype(ui: &Arc<Ui>, notes: Vec<i64>) {
    let api = &ui.shell.api;
    let ntid = match api.call::<anki_proto::notetypes::NotetypeId>(
        "get_single_notetype_of_notes",
        &anki_proto::notes::NoteIds {
            note_ids: notes.clone(),
        },
    ) {
        Ok(id) => id.ntid,
        Err(err) => return crate::main_window::message(ui, &err.message),
    };
    *ui.browser.change_notetype.lock().unwrap() = notes;
    let _ = ui.open_window(
        CHANGE_NOTETYPE,
        &ui.main.tr.browsing_change_notetype(),
        WebviewKind::ChangeNotetype,
        Content::Route(format!("change-notetype/{ntid}")),
    );
}

fn change_notetype_saved(ui: &Arc<Ui>, request: anki_proto::notetypes::ChangeNotetypeRequest) {
    let count = request.note_ids.len() as u32;
    let run = move |ui: &Arc<Ui>| {
        let out = ui
            .shell
            .api
            .call::<OpChanges>("change_notetype", &request)
            .map(|c| {
                (
                    c,
                    Some(ui.main.tr.browsing_notes_updated(count).into_owned()),
                )
            });
        if out.is_ok() {
            if let Some(w) = ui.app.get_webview_window(CHANGE_NOTETYPE) {
                let _ = w.destroy();
            }
        }
        op_done(ui, out);
    };
    // `confirm_schema_modification`
    let modified = ui
        .shell
        .api
        .query("select scm > ls from col", &[])
        .ok()
        .and_then(|r| r.first().and_then(|r| r.first()).and_then(Value::as_i64))
        .unwrap_or(0)
        != 0;
    if modified {
        return run(ui);
    }
    let tr = &ui.main.tr;
    crate::main_window::ask(
        ui,
        crate::main_window::Dialog {
            title: tr.browsing_change_notetype().into_owned(),
            message_html: htmlescape::encode_minimal(
                &tr.qt_misc_the_requested_change_will_require_a(),
            ),
            fields: vec![],
            buttons: vec!["OK".into(), tr.actions_cancel().into_owned()],
            default: 1,
        },
        move |ui, answer| {
            if answer.is_some_and(|a| a.button == 0) {
                run(ui);
            }
        },
    );
}

// Forms
//////////////////////////////////////////////////////////////////////////

pub enum FormField {
    Text {
        key: String,
        label: String,
        value: String,
    },
    Select {
        key: String,
        label: String,
        options: Vec<(String, String)>,
        value: String,
    },
    Check {
        key: String,
        label: String,
        value: bool,
    },
}

impl FormField {
    pub fn text(key: &str, label: &str, value: &str) -> Self {
        FormField::Text {
            key: key.into(),
            label: label.into(),
            value: value.into(),
        }
    }

    pub fn select(key: &str, label: &str, options: Vec<(String, String)>, value: &str) -> Self {
        FormField::Select {
            key: key.into(),
            label: label.into(),
            options,
            value: value.into(),
        }
    }

    pub fn check(key: &str, label: &str, value: bool) -> Self {
        FormField::Check {
            key: key.into(),
            label: label.into(),
            value,
        }
    }
}

fn form_html(message_html: &str, fields: &[FormField], ok: &str, cancel: &str) -> String {
    let attr = |s: &str| htmlescape::encode_attribute(s);
    let text = |s: &str| htmlescape::encode_minimal(s);
    let fields: String = fields
        .iter()
        .map(|f| match f {
            FormField::Text { key, label, value } => format!(
                "<label>{}<input name=\"{}\" value=\"{}\" autocomplete=off spellcheck=false></label>",
                text(label),
                attr(key),
                attr(value)
            ),
            FormField::Select {
                key,
                label,
                options,
                value,
            } => format!(
                "<label>{}<select name=\"{}\">{}</select></label>",
                text(label),
                attr(key),
                options
                    .iter()
                    .map(|(v, l)| format!(
                        "<option value=\"{}\"{}>{}</option>",
                        attr(v),
                        if v == value { " selected" } else { "" },
                        text(l)
                    ))
                    .collect::<String>()
            ),
            FormField::Check { key, label, value } => format!(
                "<label class=check><input type=checkbox name=\"{}\"{}> {}</label>",
                attr(key),
                if *value { " checked" } else { "" },
                text(label)
            ),
        })
        .collect();
    format!(
        "<style>body {{ margin: 0; padding: 18px 22px; font-size: 13px; }} \
         .msg {{ margin-bottom: 12px; line-height: 1.4; }} \
         label {{ display: block; margin: 8px 0; }} \
         label input:not([type=checkbox]), label select {{ display: block; width: 100%; \
           box-sizing: border-box; margin-top: 3px; }} \
         .buttons {{ text-align: right; margin-top: 16px; }} button {{ margin-left: 6px; }}</style>\
         <form><div class=msg>{message_html}</div>{fields}\
         <div class=buttons><button type=button data-i=1>{cancel}</button>\
         <button type=submit data-i=0 class=default>{ok}</button></div></form>\
         <script>{{\
           const send = (i) => {{ const fields = {{}};\
             document.querySelectorAll('[name]').forEach((e) => {{\
               fields[e.name] = e.type === 'checkbox' ? String(e.checked) : e.value; }});\
             pycmd('dialogAnswer:' + JSON.stringify({{ button: i, fields }})); }};\
           document.querySelector('[data-i=\"1\"]').addEventListener('click', () => send(1));\
           document.querySelector('form').addEventListener('submit', (e) => {{ e.preventDefault(); send(0); }});\
           document.addEventListener('keydown', (e) => {{ if (e.key === 'Escape') send(1); }});\
           const first = document.querySelector('input:not([type=checkbox]), select');\
           if (first) {{ first.focus(); if (first.select) first.select(); }}\
         }}</script>",
        ok = text(ok),
        cancel = text(cancel),
    )
}

/// A dialog with these fields; `done` gets the answer if OK was chosen.
pub fn form(
    ui: &Arc<Ui>,
    title: &str,
    message_html: &str,
    fields: Vec<FormField>,
    done: impl FnOnce(&Arc<Ui>, DialogAnswer) + Send + 'static,
) {
    let tr = &ui.main.tr;
    let height = 130.0
        + fields
            .iter()
            .map(|f| {
                if matches!(f, FormField::Check { .. }) {
                    30.0
                } else {
                    56.0
                }
            })
            .sum::<f64>()
        + if message_html.is_empty() { 0.0 } else { 40.0 };
    let html = form_html(message_html, &fields, "OK", &tr.actions_cancel());
    crate::main_window::ask_html(ui, title, html, height, move |ui, answer| {
        if let Some(answer) = answer.filter(|a| a.button == 0) {
            done(ui, answer);
        }
    });
}

// Previewer and card info
//////////////////////////////////////////////////////////////////////////

fn open_card_info(ui: &Ui, cid: i64) {
    let _ = ui.open_window(
        CARD_INFO,
        &ui.main.tr.actions_card_info(),
        WebviewKind::CardInfo,
        Content::Route(format!("card-info/{cid}")),
    );
}

/// Card info follows the current card while it's open (`BrowserCardInfo`).
fn update_card_info(ui: &Ui, cid: i64) {
    if ui.app.get_webview_window(CARD_INFO).is_some() {
        open_card_info(ui, cid);
    }
}

/// The browser's previewer (`BrowserPreviewer`), or close it.
pub fn toggle_preview(ui: &Arc<Ui>) {
    if let Some(w) = ui.app.get_webview_window(PREVIEW) {
        let _ = w.destroy();
        return;
    }
    let Some(card) = with_state(ui, |s| s.current_card(&ui.shell.api)).flatten() else {
        return;
    };
    *ui.browser.preview.lock().unwrap() = Preview {
        card: Some(card),
        ..Default::default()
    };
    ui.shell.pages.set(
        PREVIEW_CARD,
        WebviewKind::Previewer,
        Some(pages::reviewer_html(&ui.shell.web)),
    );
    let both = browser::get_config(&ui.shell.api, "previewBothSides")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let tr = &ui.main.tr;
    let body = format!(
        "<style>body {{ margin: 0; height: 100vh; display: flex; flex-direction: column; }} \
         #card {{ flex: 1; border: 0; width: 100%; }} \
         #bar {{ display: flex; align-items: center; gap: 8px; padding: 6px 10px; \
           border-top: 1px solid var(--border, #ccc); font-size: 13px; }} \
         #bar .spacer {{ flex: 1; }}</style>\
         <iframe id=card src=\"{src}\"></iframe>\
         <div id=bar><label><input type=checkbox id=both{checked}> {both}</label>\
         <span class=spacer></span>\
         <button id=back title=\"Left\">&lt;</button><button id=next title=\"Right\">&gt;</button></div>\
         <script>{{\
           const card = document.getElementById('card');\
           card.addEventListener('load', () => pycmd('previewReady'));\
           document.getElementById('back').addEventListener('click', () => pycmd('previewBack'));\
           document.getElementById('next').addEventListener('click', () => pycmd('previewNext'));\
           document.getElementById('both').addEventListener('change', (e) => pycmd('previewBoth:' + e.target.checked));\
           const keys = (e) => {{\
             if (e.target && (e.target.tagName === 'INPUT' && e.target.type !== 'checkbox' || e.target.isContentEditable)) return;\
             if (e.key === 'ArrowRight' || e.key === ' ' || e.key === 'Enter') {{ e.preventDefault(); pycmd('previewNext'); }}\
             else if (e.key === 'ArrowLeft') {{ e.preventDefault(); pycmd('previewBack'); }}\
             else if (e.key === 'Escape') pycmd('previewClose');\
             else if ((e.key === 'b' || e.key === 'B') && !e.metaKey) {{ const b = document.getElementById('both'); b.checked = !b.checked; pycmd('previewBoth:' + b.checked); }}\
             else if ((e.key === 'r' || e.key === 'R') && !e.metaKey) pycmd('previewReplay');\
           }};\
           document.addEventListener('keydown', keys);\
           card.addEventListener('load', () => card.contentWindow.document.addEventListener('keydown', keys));\
         }}</script>",
        src = pages::legacy_page_url(PREVIEW_CARD),
        checked = if both { " checked" } else { "" },
        both = htmlescape::encode_minimal(&tr.qt_misc_back_side_only()),
    );
    let html = pages::std_html(&ui.shell.web, &body, &[], &[], true);
    if let Err(err) = ui.open_window(
        PREVIEW,
        &tr.card_templates_preview_box().replace(':', ""),
        WebviewKind::Previewer,
        Content::Html(html),
    ) {
        tracing::error!(%err, "opening the previewer failed");
    }
}

fn update_preview(ui: &Arc<Ui>, cid: i64) {
    if ui.app.get_webview_window(PREVIEW).is_none() {
        return;
    }
    let changed = {
        let mut p = ui.browser.preview.lock().unwrap();
        let changed = p.card != Some(cid);
        if changed {
            p.card = Some(cid);
            p.answer = false;
        }
        changed
    };
    if changed {
        render_preview(ui);
    }
}

/// Back/Next: the other side, then the previous or next card
/// (`BrowserPreviewer._on_prev`/`_on_next`).
fn preview_step(ui: &Arc<Ui>, forward: bool) {
    let both = browser::get_config(&ui.shell.api, "previewBothSides")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let flipped = {
        let mut p = ui.browser.preview.lock().unwrap();
        if !both && p.answer != forward {
            p.answer = forward;
            true
        } else {
            false
        }
    };
    if flipped {
        render_preview(ui);
    } else {
        push(
            ui,
            json!({"type": "key", "action": if forward { "nextCard" } else { "previousCard" }}),
        );
    }
}

fn render_preview(ui: &Arc<Ui>) {
    let api = &ui.shell.api;
    let (cid, answer) = {
        let p = ui.browser.preview.lock().unwrap();
        (p.card, p.answer)
    };
    let Some(cid) = cid else {
        return;
    };
    let both = browser::get_config(api, "previewBothSides")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let answer = answer || both;
    let Ok(card) =
        api.call::<anki_proto::cards::Card>("get_card", &anki_proto::cards::CardId { cid })
    else {
        return;
    };
    let media = ui.shell.media_folder().unwrap_or_default();
    let note: anki_proto::notes::Note = api
        .call("get_note", &anki_proto::notes::NoteId { nid: card.note_id })
        .unwrap_or_default();
    let latex = crate::latex::latex_config(api, note.notetype_id).unwrap_or_default();
    let rendered = crate::card_text::render(api, cid, |text| {
        let (html, errors) = crate::latex::render_latex(api, &media, text, &latex);
        if errors.is_empty() {
            html
        } else {
            html + &errors.join("\n")
        }
    });
    let Ok(rendered) = rendered else {
        return;
    };
    // the previewer has no answer box
    let strip = |s: &str| {
        static TYPE: std::sync::LazyLock<regex::Regex> =
            std::sync::LazyLock::new(|| regex::Regex::new(r"\[\[type:[^]]+\]\]").unwrap());
        TYPE.replace_all(s, "").into_owned()
    };
    let hide = browser::get_config(api, "hideAudioPlayButtons")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);
    let prepare =
        |s: &str| crate::card_text::prepare_for_display(api, &strip(s), hide).unwrap_or_default();
    let sound = |tags: &[anki_proto::card_rendering::AvTag]| -> Vec<String> {
        tags.iter()
            .map(|t| match &t.value {
                Some(anki_proto::card_rendering::av_tag::Value::SoundOrVideo(f)) => f.clone(),
                _ => String::new(),
            })
            .collect()
    };
    {
        let mut p = ui.browser.preview.lock().unwrap();
        p.sounds = vec![(sound(&rendered.question_av), sound(&rendered.answer_av))];
        p.shown_answer = answer;
    }
    // `_update_flag_and_mark_icons`
    let marked = note.tags.iter().any(|t| t.eq_ignore_ascii_case("marked"));
    let icons = format!(
        "w._drawFlag({}); w._drawMark({marked});",
        card.flags & 0b111
    );
    let q = prepare(&rendered.question_and_style());
    let a = prepare(&rendered.answer_and_style());
    let body_class = format!("card card{} isMac", card.template_idx + 1);
    let show = if answer {
        format!(
            "w._showQuestion({q}, {a}, {c}); w._showAnswer({a}); {icons}",
            q = js(&q),
            a = js(&a),
            c = js(&body_class)
        )
    } else {
        format!(
            "w._showQuestion({q}, {a}, {c}); {icons}",
            q = js(&q),
            a = js(&a),
            c = js(&body_class)
        )
    };
    ui.eval(
        PREVIEW,
        format!(
            "(() => {{ const w = document.getElementById('card').contentWindow; \
             if (w && w._showQuestion) {{ {show} }} }})();"
        ),
    );
}

/// A play button in the previewer: the file in the card's frame.
fn play_preview_sound(ui: &Ui, args: &str) {
    let Some((question, idx)) = crate::card_text::parse_play_ref(args) else {
        return;
    };
    let file = ui
        .browser
        .preview
        .lock()
        .unwrap()
        .sounds
        .first()
        .and_then(|(q, a)| if question { q } else { a }.get(idx).cloned())
        .filter(|f| !f.is_empty());
    if let Some(file) = file {
        ui.eval(
            PREVIEW,
            format!(
                "(() => {{ const w = document.getElementById('card').contentWindow; \
                 if (window._shellAudio) window._shellAudio.pause(); \
                 window._shellAudio = new w.Audio(encodeURIComponent({})); \
                 window._shellAudio.play().catch(() => {{}}); }})();",
                js(&file)
            ),
        );
    }
}
