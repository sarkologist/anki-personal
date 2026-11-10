// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The Add Cards and Edit Current windows (`addcards.py`,
//! `editcurrent.py`) around the stock editor page, and the editor bridge
//! commands that need a window: choosers, dialogs, the pasteboard, file
//! pickers, recording. The note logic is in `editor.rs`.
//!
//! The page is the stock one plus the windows' own bars (Qt widgets
//! there): the notetype and deck choosers above the fields, the buttons
//! below, and a side panel for add-ons ([set_side_panel]). The editor CSP
//! blocks inline scripts, so the bars get their behaviour by eval.

use std::sync::Arc;

use anki_proto::notes::Note;
use anki_proto::notes::NoteId;
use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use crate::app::Content;
use crate::app::Ui;
use crate::app::EDITOR;
use crate::editor;
use crate::editor::AddCheck;
use crate::editor::Mode;
use crate::editor::Session;
use crate::main_window::Dialog;
use crate::pages;
use crate::pages::WebviewKind;

/// The Add Cards window; Edit Current is [EDITOR].
pub const ADD: &str = "add";
/// The card browser, whose page is the editor's (see `browser_window`).
pub const BROWSER: &str = "browser";
const LABELS: [&str; 3] = [ADD, EDITOR, BROWSER];

pub fn is_editor(label: &str) -> bool {
    LABELS.contains(&label)
}

/// The editor window with the focus, if one has it.
pub fn focused_editor(ui: &Ui) -> Option<&'static str> {
    LABELS.into_iter().find(|label| {
        ui.app
            .get_webview_window(label)
            .and_then(|w| w.is_focused().ok())
            .unwrap_or(false)
    })
}

/// The pasteboard editors read, if not the general one: test runs paste
/// from a scratch pasteboard, so they never touch the user's clipboard.
static PASTEBOARD: std::sync::Mutex<Option<String>> = std::sync::Mutex::new(None);

pub fn use_pasteboard(name: Option<String>) {
    *PASTEBOARD.lock().unwrap() = name;
}

fn pasteboard() -> Option<String> {
    PASTEBOARD.lock().unwrap().clone()
}

fn js(s: impl AsRef<str>) -> String {
    serde_json::to_string(s.as_ref()).unwrap()
}

// Windows
//////////////////////////////////////////////////////////////////////////

/// Edit Current on note `nid` (the reviewer's `e`): the window shows it
/// after saving the note shown before.
pub fn open_edit_current(ui: &Arc<Ui>, nid: i64, card_id: Option<i64>) -> anyhow::Result<()> {
    if ui.editors.note(EDITOR).is_some() && ui.app.get_webview_window(EDITOR).is_some() {
        if let Some(cid) = card_id {
            ui.editors.update(EDITOR, |s| s.card_id = Some(cid));
        }
        ui.after_saved(EDITOR, &format!("editorLoad:{nid}"));
        if let Some(w) = ui.app.get_webview_window(EDITOR) {
            if !ui.background {
                let _ = w.set_focus();
            }
        }
        return Ok(());
    }
    load_edit_current(ui, nid, card_id)
}

/// Show `nid` in Edit Current, without saving first.
pub fn load_edit_current(ui: &Arc<Ui>, nid: i64, card_id: Option<i64>) -> anyhow::Result<()> {
    let note: Note = ui.shell.api.call("get_note", &NoteId { nid })?;
    let card_id = card_id.or_else(|| ui.editors.session(EDITOR).and_then(|s| s.card_id));
    ui.editors.set_session(EDITOR, Session::edit(note, card_id));
    let title = ui.main.tr.editing_edit_current().into_owned();
    open_page(ui, EDITOR, &title, Mode::EditCurrent, Some(0))
}

/// Add Cards, or bring it to the front if it's open (`aqt.dialogs`).
pub fn open_add(ui: &Arc<Ui>) -> anyhow::Result<()> {
    let api = &ui.shell.api;
    let review_card = ui.main.reviewer.lock().unwrap().card_id();
    let review_deck = review_card
        .and_then(|cid| {
            api.call::<anki_proto::cards::Card>("get_card", &anki_proto::cards::CardId { cid })
                .ok()
        })
        .map(|c| {
            if c.original_deck_id != 0 {
                c.original_deck_id
            } else {
                c.deck_id
            }
        })
        .unwrap_or(0);
    let (ntid, did) = editor::defaults_for_adding(api, review_deck)?;
    if let Some(w) = ui.app.get_webview_window(ADD) {
        if let Some(session) = ui.editors.session(ADD) {
            // `AddCards.reopen`: with nothing typed, start again from the
            // defaults (the reviewed card's deck)
            if editor::session_fields_are_blank(api, &session) {
                let note = if session.note.notetype_id == ntid {
                    None
                } else {
                    Some(editor::switch_notetype(api, &session.note, ntid)?)
                };
                ui.editors.update(ADD, |s| {
                    if let Some(note) = note {
                        s.replace_note(note);
                    }
                    s.deck_id = did;
                });
                reload_note(ui, ADD, Some(0));
                update_choosers(ui);
            }
            if !ui.background {
                let _ = w.set_focus();
            }
            return Ok(());
        }
    }
    let note = editor::new_note(api, ntid)?;
    ui.editors.set_session(ADD, Session::add(note, did));
    let title = ui.main.tr.actions_add().into_owned();
    open_page(ui, ADD, &title, Mode::Add, Some(0))
}

/// Load (or reload) the editor page in `label` and show its session's
/// note.
fn open_page(
    ui: &Arc<Ui>,
    label: &str,
    title: &str,
    mode: Mode,
    focus_to: Option<u32>,
) -> anyhow::Result<()> {
    let note_js = load_js(ui, label, focus_to)?;
    ui.open_window(
        label,
        title,
        WebviewKind::Editor,
        Content::Html(page_html(ui, mode)),
    )?;
    ui.eval(label, format!("setupEditor({})", js(mode.page_mode())));
    ui.eval(label, chrome_js(ui, mode));
    ui.eval(label, format_buttons_js());
    ui.eval(label, note_js);
    if mode == Mode::Add {
        update_choosers(ui);
    }
    crate::plugins::editors::page_opened(ui, label);
    Ok(())
}

fn load_js(ui: &Ui, label: &str, focus_to: Option<u32>) -> anyhow::Result<String> {
    let prefs = ui.settings.lock().unwrap().editor.clone();
    let js = editor::load_note_js(&ui.shell, &ui.editors, label, focus_to, &prefs)?;
    if let Some(nid) = ui.editors.note(label).map(|n| n.id) {
        ui.editors.emit(
            &ui.shell,
            editor::EditorEvent::NoteLoaded {
                label: label.into(),
                nid,
            },
        );
    }
    Ok(js)
}

/// Show the session's note again, in the page already loaded
/// (`loadNote`), focusing `focus_to`.
pub fn reload_note(ui: &Ui, label: &str, focus_to: Option<u32>) {
    match load_js(ui, label, focus_to) {
        Ok(js) => ui.eval(label, js),
        Err(err) => tracing::error!(%err, "loading the note failed"),
    }
}

/// `loadNoteKeepingFocus`, for add-ons that changed the note.
pub fn reload_note_keeping_focus(ui: &Ui, label: &str) {
    let focus = ui.editors.session(label).and_then(|s| s.current_field);
    reload_note(ui, label, focus);
}

/// The browser's page: the editor in Browse mode, with the table (a
/// frame) beside it. It shows no note until one card is selected.
pub fn open_browse_page(ui: &Arc<Ui>, js: String) -> anyhow::Result<()> {
    ui.editors.clear(BROWSER);
    let title = ui.main.tr.qt_misc_browse().into_owned();
    ui.open_window(
        BROWSER,
        &title,
        WebviewKind::Editor,
        Content::Html(page_html(ui, Mode::Browse)),
    )?;
    ui.eval(
        BROWSER,
        format!("setupEditor({})", self::js(Mode::Browse.page_mode())),
    );
    ui.eval(BROWSER, chrome_js(ui, Mode::Browse));
    ui.eval(BROWSER, format_buttons_js());
    ui.eval(BROWSER, js);
    Ok(())
}

/// Show note `nid` in the browser's editor (without saving the one shown).
pub fn show_browse_note(ui: &Arc<Ui>, nid: i64, card_id: Option<i64>) -> anyhow::Result<()> {
    let note: Note = ui.shell.api.call("get_note", &NoteId { nid })?;
    let first = ui.editors.session(BROWSER).is_none();
    ui.editors
        .set_session(BROWSER, Session::browse(note, card_id));
    reload_note(ui, BROWSER, None);
    if first {
        crate::plugins::editors::page_opened(ui, BROWSER);
    }
    ui.eval(BROWSER, "shellBrowserNote(true);".into());
    Ok(())
}

/// The browser shows no note (none or several cards are selected).
pub fn clear_browse_note(ui: &Ui) {
    if ui.editors.session(BROWSER).is_some() {
        ui.editors.clear(BROWSER);
        crate::plugins::editors::closed(ui, BROWSER);
    }
    ui.eval(BROWSER, "shellBrowserNote(false);".into());
}

/// The editor page with the window's bars around it. The page mounts the
/// editor at the end of `body`; a grid puts it between the bars.
fn page_html(ui: &Ui, mode: Mode) -> String {
    let tr = &ui.main.tr;
    let esc = |s: std::borrow::Cow<str>| htmlescape::encode_minimal(&s);
    let (top, bottom) = match mode {
        Mode::Add => (
            format!(
                "<div id=shell-top><label>{}</label>\
                 <button type=button id=shell-notetype class=shell-chooser></button>\
                 <label>{}</label>\
                 <button type=button id=shell-deck class=shell-chooser></button></div>",
                esc(tr.notetypes_type()),
                esc(tr.decks_deck()),
            ),
            format!(
                "<div id=shell-bottom><span id=shell-history-wrap>\
                 <button type=button id=shell-history disabled title=\"Cmd+Shift+H\">{} &#9662;</button>\
                 <div id=shell-history-menu hidden></div></span>\
                 <span class=shell-spacer></span>\
                 <button type=button id=shell-close>{}</button>\
                 <button type=button id=shell-add class=shell-default title=\"Cmd+Enter\">{}</button></div>",
                esc(tr.adding_history()),
                esc(tr.actions_close()),
                esc(tr.actions_add()),
            ),
        ),
        Mode::EditCurrent => (
            String::new(),
            format!(
                "<div id=shell-bottom><span class=shell-spacer></span>\
                 <button type=button id=shell-close class=shell-default title=\"Cmd+Enter\">{}</button></div>",
                esc(tr.actions_close()),
            ),
        ),
        Mode::Browse => (
            format!(
                "<iframe id=shell-browser src=\"{}\"></iframe><div id=shell-split></div>",
                pages::legacy_page_url(crate::browser_window::TABLE_PAGE)
            ),
            String::new(),
        ),
    };
    let body = format!(
        "<style>{CHROME_CSS}</style>{top}<iframe id=shell-side hidden></iframe>\
         <div id=shell-side-split></div>{bottom}"
    );
    pages::std_html(
        &ui.shell.web,
        &body,
        &["css/editor.css"],
        &["js/editor.js"],
        false,
    )
    .replacen(
        "<body class=\"isMac\">",
        if mode == Mode::Browse {
            "<body class=\"isMac shell-editor shell-browse shell-no-note\">"
        } else {
            "<body class=\"isMac shell-editor\">"
        },
        1,
    )
}

const CHROME_CSS: &str = "\
body.shell-editor { display: grid; height: 100vh; margin: 0; \
  grid-template-columns: minmax(0, 1fr) auto; grid-template-rows: auto minmax(0, 1fr) auto; \
  grid-template-areas: 'top top' 'editor side' 'bottom bottom'; }
body.shell-editor > .note-editor { grid-area: editor; height: auto; min-height: 0; }
#shell-top { grid-area: top; display: flex; align-items: center; gap: 6px; padding: 6px 8px; \
  border-bottom: 1px solid var(--border, #ccc); font-size: 13px; }
#shell-top label { margin-left: 6px; }
.shell-chooser { min-width: 140px; max-width: 40vw; overflow: hidden; text-overflow: ellipsis; \
  white-space: nowrap; text-align: left; }
#shell-side { grid-area: side; width: var(--shell-side, 380px); \
  max-width: max(200px, calc(100vw - 320px)); border: 0; \
  border-left: 1px solid var(--border, #ccc); height: 100%; }
body.shell-browse:not(.shell-no-note) > #shell-side \
  { max-width: max(200px, calc(100vw - var(--shell-table, 60vw) - 325px)); }
#shell-side[hidden], #shell-side[hidden] + #shell-side-split { display: none; }
#shell-side-split { grid-area: side; justify-self: start; width: 6px; margin-left: -3px; \
  cursor: col-resize; z-index: 1; }
#shell-bottom { grid-area: bottom; display: flex; align-items: center; gap: 6px; padding: 6px 8px; \
  border-top: 1px solid var(--border, #ccc); font-size: 13px; }
.shell-spacer { flex: 1; }
#shell-history-wrap { position: relative; }
#shell-history-menu { position: absolute; bottom: 100%; left: 0; z-index: 1000; min-width: 260px; \
  max-height: 60vh; overflow-y: auto; background: var(--canvas-elevated, #fff); \
  border: 1px solid var(--border, #ccc); border-radius: 6px; padding: 4px 0; \
  box-shadow: 0 4px 12px rgba(0,0,0,0.15); }
#shell-history-menu div { padding: 3px 12px; white-space: nowrap; cursor: default; }
#shell-history-menu div:hover { background: var(--highlight-bg, #3478f6); color: var(--highlight-fg, #fff); }
#shell-history-menu div.deleted { color: var(--fg-subtle, #888); }
#shell-history-menu div.deleted:hover { background: none; color: var(--fg-subtle, #888); }
body.shell-editor.shell-browse { grid-template-rows: minmax(0, 1fr); \
  grid-template-columns: var(--shell-table, 60%) 5px minmax(0, 1fr) auto; \
  grid-template-areas: 'table split editor side'; }
body.shell-editor.shell-browse.shell-no-note { grid-template-columns: minmax(0, 1fr) 0 0 auto; }
body.shell-browse.shell-no-note > .note-editor, body.shell-browse.shell-no-note > #shell-split \
  { display: none; }
#shell-browser { grid-area: table; width: 100%; height: 100%; border: 0; min-width: 0; }
#shell-split { grid-area: split; cursor: col-resize; background: var(--border, #ccc); }
";

/// The bars' behaviour, and the window's keys: Cmd+Enter adds (or closes
/// Edit Current), Escape closes Add, Cmd+Shift+H opens the history, and the
/// format buttons' hotkeys. External drops go to the shell, as Qt's
/// `dropEvent`; drags inside the page stay WebKit's.
fn chrome_js(ui: &Ui, mode: Mode) -> String {
    let keys: Vec<Value> = editor::FORMAT_ACTIONS
        .iter()
        .map(|(name, _, hotkey, _)| {
            let mut spec = key_spec(hotkey);
            spec["cmd"] = json!(format!("format:{name}"));
            spec
        })
        .collect();
    format!(
        r#"(() => {{
    const add = {add};
    const browse = {browse};
    const $ = (id) => document.getElementById(id);
    const click = (id, cmd) => $(id)?.addEventListener("click", () => pycmd(cmd));
    click("shell-notetype", "chooseNotetype");
    click("shell-deck", "chooseDeck");
    click("shell-add", "addNote");
    click("shell-close", "closeEditor");
    const historyMenu = $("shell-history-menu");
    const deleted = {deleted};
    const showHistory = () => pycmd("addHistory", (entries) => {{
        if (!entries || !entries.length) return;
        historyMenu.replaceChildren(...entries.map(([nid, line]) => {{
            const div = document.createElement("div");
            div.textContent = line ?? deleted;
            if (line === null) div.className = "deleted";
            else div.addEventListener("click", () => {{ historyMenu.hidden = true; pycmd("editHistory:" + nid); }});
            return div;
        }}));
        historyMenu.hidden = false;
    }});
    $("shell-history")?.addEventListener("click", (e) => {{ e.stopPropagation(); historyMenu.hidden ? showHistory() : (historyMenu.hidden = true); }});
    document.addEventListener("click", () => {{ if (historyMenu) historyMenu.hidden = true; }});
    window.shellSetChoosers = (notetype, deck) => {{
        $("shell-notetype").textContent = notetype;
        $("shell-deck").textContent = deck;
    }};
    window.shellSetHistory = (any) => {{ $("shell-history").disabled = !any; }};
    // the side panel's splitter: the panel is the rightmost column
    const sideWidth = {side_width};
    if (sideWidth > 0) document.body.style.setProperty("--shell-side", sideWidth + "px");
    const sideSplit = $("shell-side-split");
    sideSplit.addEventListener("pointerdown", (e) => {{
        e.preventDefault();
        sideSplit.setPointerCapture(e.pointerId);
        const frames = [...document.querySelectorAll("iframe")];
        for (const f of frames) f.style.pointerEvents = "none";
        // the editor keeps 320px (in Browse, right of the table)
        const editor = document.querySelector(".note-editor");
        const editorLeft = editor && editor.offsetParent ? editor.getBoundingClientRect().left : 0;
        const most = Math.max(200, innerWidth - editorLeft - 320);
        const move = (ev) => {{
            const w = Math.min(Math.max(260, innerWidth - ev.clientX), most);
            document.body.style.setProperty("--shell-side", w + "px");
        }};
        const end = () => {{
            sideSplit.removeEventListener("pointermove", move);
            for (const t of ["pointerup", "pointercancel", "lostpointercapture"]) sideSplit.removeEventListener(t, end);
            for (const f of frames) f.style.pointerEvents = "";
            pycmd("editorSideWidth:" + Math.round($("shell-side").getBoundingClientRect().width));
        }};
        sideSplit.addEventListener("pointermove", move);
        for (const t of ["pointerup", "pointercancel", "lostpointercapture"]) sideSplit.addEventListener(t, end);
    }});
    const formatKeys = {keys};
    document.addEventListener("keydown", (e) => {{
        if (!browse && e.metaKey && !e.altKey && !e.ctrlKey && (e.key === "Enter" || e.code === "NumpadEnter")) {{
            e.preventDefault();
            pycmd(add ? "addNote" : "closeEditor");
            return;
        }}
        if (add && e.metaKey && e.shiftKey && e.code === "KeyH") {{
            e.preventDefault();
            showHistory();
            return;
        }}
        for (const k of formatKeys) {{
            if (e.code === k.code && e.metaKey === k.meta && e.shiftKey === k.shift && e.altKey === k.alt && !e.ctrlKey) {{
                e.preventDefault();
                e.stopPropagation();
                pycmd(k.cmd);
                return;
            }}
        }}
    }}, true);
    // Escape closes Add (not Edit Current, as in Qt), unless the page used
    // it (closing a popup)
    document.addEventListener("keydown", (e) => {{
        // (Escape also cancels an input method's composition)
        if (add && e.key === "Escape" && !e.defaultPrevented && !e.isComposing && e.keyCode !== 229) {{
            if (historyMenu && !historyMenu.hidden) {{ historyMenu.hidden = true; return; }}
            pycmd("closeEditor");
        }}
    }});
    let draggingHere = false;
    document.addEventListener("dragstart", () => {{ draggingHere = true; }}, true);
    document.addEventListener("dragend", () => {{ draggingHere = false; }}, true);
    document.addEventListener("dragover", (e) => {{ if (!draggingHere) e.preventDefault(); }}, true);
    document.addEventListener("drop", async (e) => {{
        if (draggingHere) return;
        e.preventDefault();
        e.stopPropagation();
        const dt = e.dataTransfer;
        const read = (f) => new Promise((resolve) => {{
            const r = new FileReader();
            r.onload = () => resolve({{ name: f.name, data: String(r.result).split(",")[1] || "" }});
            r.onerror = () => resolve(null);
            r.readAsDataURL(f);
        }});
        // big files would cross the bridge as base64
        const all = [...(dt.files || [])];
        const tooBig = all.filter((f) => f.size > {max_drop}).map((f) => f.name);
        const files = (await Promise.all(all.filter((f) => f.size <= {max_drop}).map(read))).filter(Boolean);
        const drop = {{
            files,
            tooBig,
            html: dt.getData("text/html"),
            text: dt.getData("text/plain"),
            uris: dt.getData("text/uri-list"),
            shift: e.shiftKey,
        }};
        if (focusIfField(e.clientX, e.clientY)) pycmd("drop:" + JSON.stringify(drop));
    }}, true);
}})();"#,
        add = mode == Mode::Add,
        browse = mode == Mode::Browse,
        max_drop = MAX_DROP_BYTES,
        deleted = js(ui.main.tr.adding_note_deleted()),
        keys = Value::Array(keys),
        side_width = ui.settings.lock().unwrap().editor.side_width,
    )
}

/// A Qt hotkey ("Cmd+Shift+[") as the page's keydown fields; `code` is the
/// physical key, since Option changes `key` on macOS.
pub(crate) fn key_spec(hotkey: &str) -> Value {
    let parts: Vec<&str> = hotkey.split('+').collect();
    let (mods, key) = match hotkey.strip_suffix("++") {
        Some(rest) => (rest.split('+').collect::<Vec<_>>(), "+"),
        None => (parts[..parts.len() - 1].to_vec(), parts[parts.len() - 1]),
    };
    let mut shift = mods.contains(&"Shift");
    let code = match key {
        "." => "Period".to_string(),
        "," => "Comma".to_string(),
        "[" => "BracketLeft".to_string(),
        "]" => "BracketRight".to_string(),
        "-" => "Minus".to_string(),
        // Shift+- on a US layout
        "_" => {
            shift = true;
            "Minus".to_string()
        }
        k if k.len() == 1 && k.chars().all(|c| c.is_ascii_digit()) => format!("Digit{k}"),
        k if k.len() == 1 => format!("Key{}", k.to_ascii_uppercase()),
        k => k.to_string(),
    };
    json!({
        "code": code,
        "meta": mods.contains(&"Cmd"),
        "shift": shift,
        "alt": mods.contains(&"Alt"),
    })
}

/// The Mini Format Pack buttons on the editor toolbar, as the add-on adds
/// them (`editor_did_init_buttons`).
fn format_buttons_js() -> String {
    let buttons: Vec<String> = editor::FORMAT_ACTIONS
        .iter()
        .map(|(name, tip, hotkey, _)| {
            format!(
                "<button tabindex=-1 class=\"anki-addon-button linkb\" type=\"button\" \
                 title=\"{}\" data-cantoggle=\"0\" data-command=\"format:{name}\">\
                 <svg viewBox=\"0 0 24 24\" style=\"width: 18px; height: 18px; fill: currentColor; \
                 vertical-align: middle;\"><path d=\"{}\"/></svg></button>",
                htmlescape::encode_attribute(&format!("{tip} ({hotkey})")),
                format_icon(name),
            )
        })
        .collect();
    // The plugin host's buttons go in this group too ([add_addon_buttons]):
    // every AddonButtons group handles the clicks of all the page's add-on
    // buttons, so with a second group each button would run twice.
    format!(
        "window.shellToolbarButtons = {{ drawn: require(\"anki/ui\").loaded.then(() => \
         require(\"anki/NoteEditor\").instances[0].toolbar.toolbar.append({{ \
         component: editorToolbar.AddonButtons, id: \"addons\", props: {{ buttons: {} }} }})) }};",
        json!(buttons)
    )
}

/// Material Design icons (`@mdi/svg`, as the toolbar's own) for the format
/// buttons; the add-on's were PNGs.
fn format_icon(name: &str) -> &'static str {
    match name {
        "strikeThrough" => "M7.2 9.8C6 7.5 7.7 4.8 10.1 4.3C13.2 3.3 17.7 4.7 17.6 8.5H14.6C14.6 8.2 14.5 7.9 14.5 7.7C14.3 7.1 13.9 6.8 13.3 6.6C12.5 6.3 11.2 6.4 10.5 6.9C9 8.2 10.4 9.5 12 10H7.4C7.3 9.9 7.3 9.8 7.2 9.8M21 13V11H3V13H12.6C12.8 13.1 13 13.1 13.2 13.2C13.8 13.5 14.3 13.7 14.5 14.3C14.6 14.7 14.7 15.2 14.5 15.6C14.3 16.1 13.9 16.3 13.4 16.5C11.6 17 9.4 16.3 9.5 14.1H6.5C6.4 16.7 8.6 18.5 11 18.8C14.8 19.6 19.3 17.2 17.3 12.9L21 13Z",
        "abbr" => "M20.06,18C20,17.83 19.91,17.54 19.86,17.11C19.19,17.81 18.38,18.16 17.45,18.16C16.62,18.16 15.93,17.92 15.4,17.45C14.87,17 14.6,16.39 14.6,15.66C14.6,14.78 14.93,14.1 15.6,13.61C16.27,13.12 17.21,12.88 18.43,12.88H19.83V12.24C19.83,11.75 19.68,11.36 19.38,11.07C19.08,10.78 18.63,10.64 18.05,10.64C17.53,10.64 17.1,10.76 16.75,11C16.4,11.25 16.23,11.54 16.23,11.89H14.77C14.77,11.46 14.92,11.05 15.22,10.65C15.5,10.25 15.93,9.94 16.44,9.71C16.95,9.5 17.5,9.36 18.13,9.36C19.11,9.36 19.87,9.6 20.42,10.09C20.97,10.58 21.26,11.25 21.28,12.11V16C21.28,16.8 21.38,17.42 21.58,17.88V18H20.06M17.66,16.88C18.11,16.88 18.54,16.77 18.95,16.56C19.35,16.35 19.65,16.07 19.83,15.73V14.16H18.7C16.93,14.16 16.04,14.63 16.04,15.57C16.04,16 16.19,16.3 16.5,16.53C16.8,16.76 17.18,16.88 17.66,16.88M5.46,13.71H9.53L7.5,8.29L5.46,13.71M6.64,6H8.36L13.07,18H11.14L10.17,15.43H4.82L3.86,18H1.93L6.64,6Z",
        "formatBlockPre" => "M5.59 3.41L7 4.82L3.82 8L7 11.18L5.59 12.6L1 8L5.59 3.41M11.41 3.41L16 8L11.41 12.6L10 11.18L13.18 8L10 4.82L11.41 3.41M22 6V18C22 19.11 21.11 20 20 20H4C2.9 20 2 19.11 2 18V14H4V18H20V6H17.03V4H20C21.11 4 22 4.89 22 6Z",
        "formatInlineCode" => "M14.6,16.6L19.2,12L14.6,7.4L16,6L22,12L16,18L14.6,16.6M9.4,16.6L4.8,12L9.4,7.4L8,6L2,12L8,18L9.4,16.6Z",
        "insertHorizontalRule" => "M19,13H5V11H19V13Z",
        "insertUnorderedList" => "M7,5H21V7H7V5M7,13V11H21V13H7M4,4.5A1.5,1.5 0 0,1 5.5,6A1.5,1.5 0 0,1 4,7.5A1.5,1.5 0 0,1 2.5,6A1.5,1.5 0 0,1 4,4.5M4,10.5A1.5,1.5 0 0,1 5.5,12A1.5,1.5 0 0,1 4,13.5A1.5,1.5 0 0,1 2.5,12A1.5,1.5 0 0,1 4,10.5M7,19V17H21V19H7M4,16.5A1.5,1.5 0 0,1 5.5,18A1.5,1.5 0 0,1 4,19.5A1.5,1.5 0 0,1 2.5,18A1.5,1.5 0 0,1 4,16.5Z",
        "insertOrderedList" => "M7,13V11H21V13H7M7,19V17H21V19H7M7,7V5H21V7H7M3,8V5H2V4H4V8H3M2,17V16H5V20H2V19H4V18.5H3V17.5H4V17H2M4.25,10A0.75,0.75 0 0,1 5,10.75C5,10.95 4.92,11.14 4.79,11.27L3.12,13H5V14H2V13.08L4,11H2V10H4.25Z",
        "indent" => "M11,13H21V11H11M11,9H21V7H11M3,3V5H21V3M11,17H21V15H11M3,8V16L7,12M3,21H21V19H3V21Z",
        "outdent" => "M11,13H21V11H11M11,9H21V7H11M3,3V5H21V3M3,21H21V19H3M3,12L7,16V8M11,17H21V15H11V17Z",
        "justifyCenter" => "M3,3H21V5H3V3M7,7H17V9H7V7M3,11H21V13H3V11M7,15H17V17H7V15M3,19H21V21H3V19Z",
        "justifyLeft" => "M3,3H21V5H3V3M3,7H15V9H3V7M3,11H21V13H3V11M3,15H15V17H3V15M3,19H21V21H3V19Z",
        "justifyRight" => "M3,3H21V5H3V3M9,7H21V9H9V7M3,11H21V13H3V11M9,15H21V17H9V15M3,19H21V21H3V19Z",
        _ => "M3,3H21V5H3V3M3,7H21V9H3V7M3,11H21V13H3V11M3,15H21V17H3V15M3,19H21V21H3V19Z",
    }
}

/// The Add window's chooser buttons show its notetype and deck.
fn update_choosers(ui: &Ui) {
    let Some(session) = ui.editors.session(ADD) else {
        return;
    };
    let api = &ui.shell.api;
    let notetype = api
        .call::<anki_proto::generic::Json>(
            "get_notetype_legacy",
            &anki_proto::notetypes::NotetypeId {
                ntid: session.note.notetype_id,
            },
        )
        .ok()
        .and_then(|j| serde_json::from_slice::<Value>(&j.json).ok())
        .and_then(|nt| nt["name"].as_str().map(str::to_string))
        .unwrap_or_default();
    let deck = api
        .call::<anki_proto::decks::Deck>(
            "get_deck",
            &anki_proto::decks::DeckId {
                did: session.deck_id,
            },
        )
        .map(|d| d.name.replace('\x1f', "::"))
        .unwrap_or_default();
    ui.eval(
        ADD,
        format!(
            "window.shellSetChoosers?.({}, {}); window.shellSetHistory?.({});",
            js(notetype),
            js(deck),
            !session.history.is_empty()
        ),
    );
}

/// An add-on's side panel beside the fields, or none. The panel is its own
/// document in a frame (`/_anki/legacyPageData`), so it can have scripts;
/// it shares the editor's bridge and API. It loads hidden or shown, and
/// [show_side_panel] shows or hides it.
pub fn set_side_panel(ui: &Ui, label: &str, html: Option<String>, visible: bool) {
    let id = format!("{label}-side");
    let js = match html {
        Some(html) => {
            ui.shell.pages.set(&id, WebviewKind::Dialog, Some(html));
            format!(
                "(() => {{ const f = document.getElementById(\"shell-side\"); \
                 f.src = {}; f.hidden = {}; }})();",
                js(pages::legacy_page_url(&id)),
                !visible
            )
        }
        None => {
            ui.shell.pages.remove(&id);
            "(() => { const f = document.getElementById(\"shell-side\"); \
             f.hidden = true; f.removeAttribute(\"src\"); })();"
                .to_string()
        }
    };
    ui.eval(label, js);
}

pub fn show_side_panel(ui: &Ui, label: &str, visible: bool) {
    ui.eval(
        label,
        format!(
            "(() => {{ const f = document.getElementById(\"shell-side\"); \
             if (f && f.src) f.hidden = {}; }})();",
            !visible
        ),
    );
}

/// Add-ons' toolbar buttons (`Editor.addButton`'s HTML) and their keys
/// (Qt's names: Ctrl is Cmd), as `editor_did_init_buttons` adds them.
pub fn add_addon_buttons(ui: &Ui, label: &str, buttons: &[String], keys: &[(String, String)]) {
    let keys: Vec<Value> = keys
        .iter()
        .map(|(hotkey, cmd)| {
            let hotkey = hotkey
                .split('+')
                .map(|part| match part {
                    "Ctrl" => "Cmd",
                    "Meta" => "Ctrl",
                    other => other,
                })
                .collect::<Vec<_>>()
                .join("+");
            let mut spec = key_spec(&hotkey);
            spec["ctrl"] = json!(hotkey.split('+').any(|p| p == "Ctrl"));
            spec["cmd"] = json!(cmd);
            spec
        })
        .collect();
    let mut js = String::new();
    if !buttons.is_empty() {
        // after the format buttons, in their group, with the clicks
        // AddonButtons would give them; again after a host restart
        js += &format!(
            r#"window.shellToolbarButtons.drawn.then(() => {{
    const format = document.querySelector(".anki-addon-button[data-command^='format:']");
    const group = format?.parentElement?.parentElement;
    if (!group) return;
    group.querySelectorAll(".shell-plugin-button").forEach((w) => w.remove());
    const last = group.lastElementChild;
    if (last) last.style.cssText = "--border-left-radius: 0; --border-right-radius: 0;";
    const buttons = {buttons};
    buttons.forEach((html, i) => {{
        const wrap = document.createElement("div");
        wrap.className = "shell-plugin-button";
        wrap.style.cssText = i === buttons.length - 1
            ? "--border-left-radius: 0; --border-right-radius: 5px;"
            : "--border-left-radius: 0; --border-right-radius: 0;";
        wrap.innerHTML = html;
        const button = wrap.firstElementChild;
        button.addEventListener("mousedown", (e) => {{ e.preventDefault(); e.stopPropagation(); }});
        button.addEventListener("click", () => {{
            const command = button.getAttribute("data-command");
            if (command) pycmd(command);
            if (button.getAttribute("data-cantoggle") === "1") button.classList.toggle("active");
        }});
        group.append(wrap);
    }});
}});"#,
            buttons = json!(buttons)
        );
    }
    if !keys.is_empty() {
        // one listener per page, for the latest keys (a restarted host
        // sends them again)
        js += &format!(
            "window.shellAddonKeys = {}; if (!window.shellAddonKeysListen) {{ \
             window.shellAddonKeysListen = true; document.addEventListener(\"keydown\", (e) => {{ \
             for (const k of window.shellAddonKeys) {{ if (e.code === k.code && e.metaKey === k.meta && \
             e.shiftKey === k.shift && e.altKey === k.alt && e.ctrlKey === k.ctrl) {{ \
             e.preventDefault(); e.stopPropagation(); pycmd(k.cmd); return; }} }} }}, true); }}",
            Value::Array(keys)
        );
    }
    ui.eval(label, js);
}

/// `Editor.doPaste` for an add-on: filtered as Qt does, then `pasteHTML`.
pub fn paste_from_addon(ui: &Arc<Ui>, label: &str, html: &str, internal: bool, extended: bool) {
    let media = crate::paste::ShellMedia::new(&ui.shell);
    let html = crate::paste::pre_filter(html, internal, &media);
    let warnings = media.take_warnings();
    if !warnings.is_empty() {
        crate::main_window::message(ui, &warnings.join("\n"));
    }
    ui.eval(
        label,
        format!("pasteHTML({}, {internal}, {extended});", js(&html)),
    );
}

/// `Editor.checkValid`: the duplicate mark.
pub fn check_valid(ui: &Ui, label: &str) {
    show_duplicates(ui, label);
}

/// `activateWindow` on an editor's window.
pub fn raise(ui: &Ui, label: &str) {
    if let Some(w) = ui.app.get_webview_window(label) {
        if !ui.background {
            let _ = w.set_focus();
        }
    }
}

pub fn tooltip(ui: &Ui, label: &str, msg: &str) {
    editor_tooltip(ui, label, msg);
}

// Closing
//////////////////////////////////////////////////////////////////////////

/// The window's close button, Escape or Close: save, then close, asking
/// first if the Add window has unsaved input (`ifCanClose`).
pub fn close(ui: &Arc<Ui>, label: &str) {
    if ui.editors.session(label).is_some() {
        ui.after_saved(label, "editorClose");
    } else if let Some(w) = ui.app.get_webview_window(label) {
        let _ = w.destroy();
    }
}

fn close_saved(ui: &Arc<Ui>, label: &str) {
    let destroy = |ui: &Arc<Ui>, label: &str| {
        if let Some(w) = ui.app.get_webview_window(label) {
            let _ = w.destroy();
        }
    };
    let Some(session) = ui.editors.session(label) else {
        return destroy(ui, label);
    };
    if session.mode != Mode::Add || editor::session_fields_are_blank(&ui.shell.api, &session) {
        return destroy(ui, label);
    }
    let tr = &ui.main.tr;
    let label = label.to_string();
    crate::main_window::ask(
        ui,
        Dialog {
            title: tr.actions_add().into_owned(),
            message_html: htmlescape::encode_minimal(&tr.adding_discard_current_input()),
            fields: vec![],
            buttons: vec![
                tr.actions_discard().into_owned(),
                tr.adding_keep_editing().into_owned(),
            ],
            default: 1,
        },
        move |ui, answer| {
            if answer.is_some_and(|a| a.button == 0) {
                ui.editors.clear(&label);
                destroy(ui, &label);
            }
        },
    );
}

/// Editor windows still saving before the app quits.
static EXIT_WAITING: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);

/// The app is quitting (with `code`): whether to wait, because editors
/// must save first. Then it quits, unless the Add window's input would be
/// lost and the user keeps editing.
pub fn exit_requested(ui: &Arc<Ui>, code: i32) -> bool {
    use std::sync::atomic::Ordering;
    if ui.exiting.load(Ordering::SeqCst) || EXIT_WAITING.load(Ordering::SeqCst) > 0 {
        return EXIT_WAITING.load(Ordering::SeqCst) > 0;
    }
    let open: Vec<&str> = LABELS
        .into_iter()
        .filter(|l| ui.editors.session(l).is_some() && ui.app.get_webview_window(l).is_some())
        .collect();
    if open.is_empty() {
        return false;
    }
    // e.g. a test's result, to exit with once the editors are saved
    ui.exit_code.store(code, Ordering::SeqCst);
    EXIT_WAITING.store(open.len(), Ordering::SeqCst);
    for label in open {
        ui.after_saved(label, "editorExit");
    }
    true
}

fn exit_saved(ui: &Arc<Ui>) {
    use std::sync::atomic::Ordering;
    if EXIT_WAITING.fetch_sub(1, Ordering::SeqCst) != 1 {
        return;
    }
    let quit = |ui: &Arc<Ui>| {
        ui.exiting.store(true, Ordering::SeqCst);
        ui.app.exit(ui.exit_code.load(Ordering::SeqCst));
    };
    let unsaved = ui
        .editors
        .session(ADD)
        .is_some_and(|s| !editor::session_fields_are_blank(&ui.shell.api, &s));
    if !unsaved || ui.test_mode {
        return quit(ui);
    }
    let tr = &ui.main.tr;
    crate::main_window::ask(
        ui,
        Dialog {
            title: tr.actions_add().into_owned(),
            message_html: htmlescape::encode_minimal(&tr.adding_discard_current_input()),
            fields: vec![],
            buttons: vec![
                tr.actions_discard().into_owned(),
                tr.adding_keep_editing().into_owned(),
            ],
            default: 1,
        },
        move |ui, answer| {
            if answer.is_some_and(|a| a.button == 0) {
                quit(ui);
            }
        },
    );
}

/// Whether an editor window with a note is open.
pub fn has_open_editors(ui: &Ui) -> bool {
    LABELS
        .into_iter()
        .any(|l| ui.editors.session(l).is_some() && ui.app.get_webview_window(l).is_some())
}

/// The collection changed outside Edit Current (an undo, the reviewer's
/// mark, a sync): show the note as stored, or close if it's gone
/// (`EditCurrent.on_operation_did_execute`).
pub fn note_changed_elsewhere(ui: &Arc<Ui>) {
    for label in [EDITOR, BROWSER] {
        note_changed_in(ui, label);
    }
}

fn note_changed_in(ui: &Arc<Ui>, label: &str) {
    let Some(session) = ui.editors.session(label) else {
        return;
    };
    let stored = ui.shell.api.call::<Note>(
        "get_note",
        &NoteId {
            nid: session.note.id,
        },
    );
    match stored {
        // the browser stays, with no note
        Err(_) if label == BROWSER => clear_browse_note(ui),
        Err(_) => {
            ui.editors.clear(label);
            if let Some(w) = ui.app.get_webview_window(label) {
                let _ = w.destroy();
            }
        }
        Ok(note)
            if note.fields != session.note.fields
                || note.tags != session.note.tags
                || note.notetype_id != session.note.notetype_id =>
        {
            ui.editors.update(label, |s| s.note = note);
            reload_note_keeping_focus(ui, label);
        }
        Ok(_) => {}
    }
}

/// An editor window went away.
pub fn destroyed(ui: &Arc<Ui>, label: &str) {
    ui.editors.clear(label);
    if label == BROWSER {
        crate::browser_window::destroyed(ui);
    }
    ui.shell.pages.remove(&format!("{label}-side"));
    crate::main_window::editor_closed(ui);
}

// Adding
//////////////////////////////////////////////////////////////////////////

/// Add / Cmd+Enter: save the fields, then add (`add_current_note`) the
/// note shown now.
fn add_note(ui: &Arc<Ui>) {
    let Some(generation) = ui.editors.session(ADD).map(|s| s.generation) else {
        return;
    };
    // image occlusion: the masks go into their field first
    ui.eval(
        ADD,
        "(() => { try { if (globalThis.canvas) saveOcclusions(); } catch (e) { console.error(e); } })();"
            .into(),
    );
    ui.after_saved(ADD, &format!("editorAdd:{generation}"));
}

fn add_saved(ui: &Arc<Ui>, generation: i64) {
    // added already (a double click), or another note since
    let Some(session) = ui
        .editors
        .session(ADD)
        .filter(|s| s.generation == generation)
    else {
        return;
    };
    let tr = &ui.main.tr;
    match editor::add_check(&ui.shell.api, &session.note) {
        AddCheck::Ok => really_add(ui, generation),
        AddCheck::ConfirmNoCloze(_) if session.cloze_confirmed == Some(generation) => {
            really_add(ui, generation)
        }
        AddCheck::Problem(msg) => crate::main_window::ask(
            ui,
            Dialog {
                title: tr.actions_add().into_owned(),
                message_html: htmlescape::encode_minimal(&msg),
                fields: vec![],
                buttons: vec!["OK".into()],
                default: 0,
            },
            |_, _| {},
        ),
        AddCheck::ConfirmNoCloze(msg) => crate::main_window::ask(
            ui,
            Dialog {
                title: tr.actions_add().into_owned(),
                message_html: htmlescape::encode_minimal(&msg),
                fields: vec![],
                buttons: vec![
                    tr.actions_add().into_owned(),
                    tr.actions_cancel().into_owned(),
                ],
                default: 0,
            },
            move |ui, answer| {
                if answer.is_some_and(|a| a.button == 0) {
                    // the window kept working meanwhile: save and check
                    // again, then add
                    ui.editors.update(ADD, |s| {
                        if s.generation == generation {
                            s.cloze_confirmed = Some(generation);
                        }
                    });
                    add_note(ui);
                }
            },
        ),
    }
}

fn really_add(ui: &Arc<Ui>, generation: i64) {
    match editor::add_current_note(&ui.shell, &ui.editors, ADD, generation) {
        Ok(Some((_, cards))) => {
            let tr = &ui.main.tr;
            reload_note(ui, ADD, Some(0));
            ui.eval(
                ADD,
                "(() => { try { resetIOImageLoaded(); } catch (e) {} })();".into(),
            );
            update_choosers(ui);
            editor_tooltip(ui, ADD, &tr.importing_cards_added(cards));
            // new cards in the deck list's counts; the reviewer picks them
            // up with its next card, as in Qt
            let reviewing = *ui.main.state.lock().unwrap() == crate::main_window::MainState::Review;
            if !reviewing {
                crate::main_window::refresh(ui);
            }
            crate::menus::refresh(ui);
        }
        Ok(None) => {}
        Err(err) => crate::main_window::message(ui, &err.message),
    }
}

/// A short message over the window (Qt's `tooltip`).
fn editor_tooltip(ui: &Ui, label: &str, msg: &str) {
    ui.eval(
        label,
        format!(
            r#"(() => {{
    const d = document.createElement("div");
    d.textContent = {};
    d.style.cssText = "position:fixed;left:50%;bottom:52px;transform:translateX(-50%);z-index:2000;padding:6px 14px;border-radius:6px;background:rgba(40,40,40,0.9);color:#fff;font-size:13px;pointer-events:none";
    document.body.appendChild(d);
    setTimeout(() => d.remove(), 1200);
}})();"#,
            js(msg)
        ),
    );
}

fn choose_notetype(ui: &Arc<Ui>) {
    ui.after_saved(ADD, "editorChooseNotetype");
}

fn choose_notetype_saved(ui: &Arc<Ui>) {
    let Some(session) = ui.editors.session(ADD) else {
        return;
    };
    let Ok(names) = ui.shell.api.call::<anki_proto::notetypes::NotetypeNames>(
        "get_notetype_names",
        &anki_proto::generic::Empty {},
    ) else {
        return;
    };
    let items: Vec<(String, String)> = names
        .entries
        .into_iter()
        .map(|e| (e.id.to_string(), e.name))
        .collect();
    let title = ui.main.tr.qt_misc_choose_note_type().into_owned();
    let current = session.note.notetype_id.to_string();
    crate::main_window::pick(ui, &title, &items, &current, |ui, id| {
        let Some(ntid) = id.and_then(|id| id.parse::<i64>().ok()) else {
            return;
        };
        let Some(session) = ui.editors.session(ADD) else {
            return;
        };
        if ntid == session.note.notetype_id {
            return;
        }
        let api = &ui.shell.api;
        let note = match editor::switch_notetype(api, &session.note, ntid) {
            Ok(note) => note,
            Err(err) => return crate::main_window::message(ui, &err.message),
        };
        let deck = editor::default_deck_for_notetype(api, ntid);
        let fields = note.fields.len() as u32;
        ui.editors.update(ADD, |s| {
            s.replace_note(note);
            s.last_added = None;
            if let Some(did) = deck {
                s.deck_id = did;
            }
        });
        let focus = session
            .last_field_index
            .unwrap_or(0)
            .min(fields.saturating_sub(1));
        reload_note(ui, ADD, Some(focus));
        update_choosers(ui);
    });
}

fn choose_deck(ui: &Arc<Ui>) {
    let Some(session) = ui.editors.session(ADD) else {
        return;
    };
    let Ok(names) = ui.shell.api.call::<anki_proto::decks::DeckNames>(
        "get_deck_names",
        &anki_proto::decks::GetDeckNamesRequest {
            skip_empty_default: true,
            include_filtered: false,
        },
    ) else {
        return;
    };
    let items: Vec<(String, String)> = names
        .entries
        .into_iter()
        .map(|d| (d.id.to_string(), d.name))
        .collect();
    let title = ui.main.tr.qt_misc_choose_deck().into_owned();
    crate::main_window::pick(
        ui,
        &title,
        &items,
        &session.deck_id.to_string(),
        |ui, id| {
            if let Some(did) = id.and_then(|id| id.parse::<i64>().ok()) {
                ui.editors.update(ADD, |s| s.deck_id = did);
                update_choosers(ui);
            }
        },
    );
}

// Media
//////////////////////////////////////////////////////////////////////////

/// File types the attach button offers (`pics` and `audio`).
fn media_extensions() -> Vec<&'static str> {
    crate::paste::PICS
        .iter()
        .chain(crate::paste::AUDIO.iter())
        .copied()
        .collect()
}

/// Add a file to the media folder; its `<img>` or `[sound:]`.
fn add_media_file(ui: &Ui, path: &std::path::Path) -> anyhow::Result<String> {
    crate::paste::add_media(&crate::paste::ShellMedia::new(&ui.shell), path)
}

/// The attach button (`onAddMedia`).
fn attach(ui: &Arc<Ui>, label: &str) {
    let title = ui.main.tr.editing_add_media().into_owned();
    let Some(path) = crate::pasteboard::choose_file(&ui.app, &title, &media_extensions()) else {
        // the button waits for an answer
        ui.eval(label, editor::resolve_media_js(""));
        return;
    };
    match add_media_file(ui, &path) {
        Ok(html) => ui.eval(label, editor::resolve_media_js(&html)),
        Err(err) => {
            ui.eval(label, editor::resolve_media_js(""));
            crate::main_window::message(ui, &format!("{err:#}"));
        }
    }
}

/// The record button (`onRecSound`): a small window that records with
/// the page's `MediaRecorder`, then the file goes to the media folder.
fn record(ui: &Arc<Ui>, label: &str) {
    let label = label.to_string();
    let tr = &ui.main.tr;
    crate::main_window::ask_html(
        ui,
        &tr.editing_record_audio(),
        RECORD_HTML
            .replace("{save}", &htmlescape::encode_minimal(&tr.actions_save()))
            .replace(
                "{cancel}",
                &htmlescape::encode_minimal(&tr.actions_cancel()),
            ),
        260.0,
        move |ui, answer| {
            let recorded = answer.and_then(|a| {
                let data = data_encoding::BASE64
                    .decode(a.fields.get("data")?.as_bytes())
                    .ok()?;
                let ext = a.fields.get("ext").cloned().unwrap_or_else(|| "m4a".into());
                Some((data, ext))
            });
            let html = recorded
                .and_then(|(data, ext)| {
                    let media = crate::paste::ShellMedia::new(&ui.shell);
                    let name = format!("rec{}.{ext}", chrono::Local::now().format("%Y%m%d%H%M%S"));
                    crate::paste::Media::write_data(&media, &name, &data).ok()
                })
                .map(|name| crate::paste::fname_to_link(&name))
                .unwrap_or_default();
            ui.eval(&label, editor::resolve_media_js(&html));
        },
    );
}

const RECORD_HTML: &str = r#"<style>body { margin: 0; padding: 18px 22px; font-size: 13px; }
.buttons { text-align: right; margin-top: 16px; } button { margin-left: 6px; }
#status { font-variant-numeric: tabular-nums; }</style>
<div id=status>Starting...</div>
<div class=buttons><button type=button id=cancel>{cancel}</button><button type=button id=save autofocus disabled>{save}</button></div>
<script>(async () => {
  const status = document.getElementById("status");
  const answer = (fields) => pycmd("dialogAnswer:" + JSON.stringify({ button: fields ? 0 : 1, fields: fields || {} }));
  document.getElementById("cancel").addEventListener("click", () => answer(null));
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (e) {
    status.textContent = "Can't record: " + e;
    return;
  }
  const type = ["audio/mp4", "audio/webm", "audio/ogg"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
  const ext = type.includes("webm") ? "webm" : type.includes("ogg") ? "ogg" : "m4a";
  const rec = new MediaRecorder(stream, type ? { mimeType: type } : {});
  const chunks = [];
  rec.ondataavailable = (e) => chunks.push(e.data);
  const started = Date.now();
  const tick = setInterval(() => { status.textContent = "Recording... " + ((Date.now() - started) / 1000).toFixed(1) + "s"; }, 100);
  rec.onstop = () => {
    clearInterval(tick);
    stream.getTracks().forEach((t) => t.stop());
    const reader = new FileReader();
    reader.onload = () => answer({ data: String(reader.result).split(",")[1] || "", ext });
    reader.readAsDataURL(new Blob(chunks, { type: rec.mimeType }));
  };
  rec.start();
  const save = document.getElementById("save");
  save.disabled = false;
  save.focus();
  save.addEventListener("click", () => rec.stop());
  document.addEventListener("keydown", (e) => { if (e.key === "Enter") rec.stop(); if (e.key === "Escape") answer(null); });
})();</script>"#;

/// `paste` (the page cancelled the paste event): the pasteboard, filtered
/// as Qt does, then `pasteHTML`.
fn paste(ui: &Arc<Ui>, label: &str) {
    if ui.editors.session(label).is_none() {
        return;
    }
    let tracker = ui.paste_tracker(label);
    if let Some(p) = crate::paste::paste_html(&ui.shell, &tracker, pasteboard().as_deref()) {
        if !p.warnings.is_empty() {
            crate::main_window::message(ui, &p.warnings.join("\n"));
        }
        ui.eval(
            label,
            format!(
                "pasteHTML({}, {}, {});",
                js(&p.html),
                p.internal,
                p.extended
            ),
        );
    }
}

/// Dropped files bigger than this aren't read (100 MB).
const MAX_DROP_BYTES: u64 = 100 * 1024 * 1024;

/// Whether a dropped file goes into the media folder: pictures and audio,
/// as Qt's `urlToLink`.
fn is_media_file(name: &str) -> bool {
    let ext = name.rsplit_once('.').map(|(_, e)| e.to_ascii_lowercase());
    ext.is_some_and(|ext| {
        crate::paste::PICS.contains(&ext.as_str()) || crate::paste::AUDIO.contains(&ext.as_str())
    })
}

/// A drop from outside the page (Finder files, another app's HTML).
fn drop(ui: &Arc<Ui>, label: &str, args: &str) {
    let Ok(drop) = serde_json::from_str::<Value>(args) else {
        return;
    };
    let shift = drop["shift"].as_bool().unwrap_or(false);
    let extended = crate::paste::wants_extended_for(&ui.shell, shift);
    let media = crate::paste::ShellMedia::new(&ui.shell);
    let mut html = String::new();
    let files: Vec<(String, Vec<u8>)> = drop["files"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|f| {
            let data = data_encoding::BASE64
                .decode(f["data"].as_str()?.as_bytes())
                .ok()?;
            Some((f["name"].as_str()?.to_string(), data))
        })
        .collect();
    let io_picker = ui.editors.session(label).is_some_and(|s| s.state == 1);
    let mut skipped: Vec<String> = drop["tooBig"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|n| n.as_str().map(|n| format!("{n} (too big)")))
        .collect();
    for (name, data) in &files {
        if !is_media_file(name) {
            skipped.push(name.clone());
            continue;
        }
        let Ok(saved) = crate::paste::Media::write_data(&media, name, data) else {
            continue;
        };
        if io_picker && crate::paste::is_picture(&saved) {
            // dropping an image on the occlusion picker starts the masks
            return setup_mask_editor_for_media(ui, label, &saved);
        }
        html += &crate::paste::fname_to_link(&saved);
    }
    if !skipped.is_empty() {
        crate::main_window::message(
            ui,
            &format!(
                "Only pictures and audio go into the media folder. Not added: {}",
                skipped.join(", ")
            ),
        );
    }
    let mut internal = false;
    if html.is_empty() && !files.is_empty() {
        return;
    }
    if html.is_empty() {
        let contents = crate::pasteboard::Contents {
            html: drop["html"]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(Into::into),
            text: drop["text"]
                .as_str()
                .filter(|s| !s.is_empty())
                .map(Into::into),
            urls: drop["uris"]
                .as_str()
                .unwrap_or_default()
                .lines()
                .filter(|l| !l.is_empty() && !l.starts_with('#'))
                .map(Into::into)
                .collect(),
            image: None,
        };
        match crate::paste::mime_to_html(&contents, extended, &media) {
            Some((h, i)) => {
                html = h;
                internal = i;
            }
            None => return,
        }
    }
    let html = crate::paste::pre_filter(&html, internal, &media);
    let warnings = media.take_warnings();
    if !warnings.is_empty() {
        crate::main_window::message(ui, &warnings.join("\n"));
    }
    ui.eval(
        label,
        format!("pasteHTML({}, {internal}, {extended});", js(&html)),
    );
}

// Image occlusion
//////////////////////////////////////////////////////////////////////////

/// `setup_mask_editor` for a file already in the media folder.
fn setup_mask_editor_for_media(ui: &Arc<Ui>, label: &str, name: &str) {
    let Some(session) = ui.editors.session(label) else {
        return;
    };
    let Some(folder) = ui.shell.media_folder() else {
        return;
    };
    let path = folder.join(name);
    let html = crate::paste::fname_to_link(name);
    ui.eval(
        label,
        editor::mask_editor_js(
            session.mode,
            session.note.id,
            &path.to_string_lossy(),
            &html,
        ),
    );
}

fn setup_mask_editor(ui: &Arc<Ui>, label: &str, path: &std::path::Path) {
    let media = crate::paste::ShellMedia::new(&ui.shell);
    match crate::paste::Media::add_file(&media, path) {
        Ok(name) => setup_mask_editor_for_media(ui, label, &name),
        Err(err) => crate::main_window::message(ui, &format!("{err:#}")),
    }
}

/// `addImageForOcclusion` (`select_image_and_occlude`).
fn occlude_file(ui: &Arc<Ui>, label: &str) {
    let title = ui.main.tr.editing_add_media().into_owned();
    if let Some(path) = crate::pasteboard::choose_file(&ui.app, &title, crate::paste::PICS) {
        setup_mask_editor(ui, label, &path);
    }
}

/// `addImageForOcclusionFromClipboard`.
fn occlude_pasteboard(ui: &Arc<Ui>, label: &str) {
    match crate::paste::occlusion_image_from_pasteboard(&ui.shell, pasteboard().as_deref()) {
        Some(path) => setup_mask_editor(ui, label, &path),
        None => crate::main_window::message(ui, &ui.main.tr.editing_no_image_found_on_clipboard()),
    }
}

// Bridge
//////////////////////////////////////////////////////////////////////////

pub fn register(ui: &Arc<Ui>) {
    let bridge = &ui.shell.bridge;
    // commands only the editor windows may send; card JS in the main
    // window reaches the same bridge
    let reg = |name: &str, f: fn(&Arc<Ui>, &str, &str) -> Value| {
        let u = ui.clone();
        bridge.register(name, move |_, label, args| {
            if is_editor(label) {
                f(&u, label, args)
            } else {
                tracing::warn!(label, name = "editor command", "not from an editor");
                Value::Null
            }
        });
    };
    // fields
    reg("key", |ui, label, args| {
        if editor::save_field(&ui.shell, &ui.editors, label, args, false) {
            show_duplicates(ui, label);
        }
        Value::Null
    });
    reg("blur", |ui, label, args| {
        if editor::save_field(&ui.shell, &ui.editors, label, args, true) {
            show_duplicates(ui, label);
        }
        Value::Null
    });
    reg("saveFields", |ui, label, args| {
        let saved = editor::save_fields(&ui.shell, &ui.editors, label, args);
        if saved {
            show_duplicates(ui, label);
        }
        json!(saved)
    });
    reg("saveTags", |ui, label, args| {
        editor::save_tags(&ui.shell, &ui.editors, label, args);
        Value::Null
    });
    reg("focus", |ui, label, args| {
        editor::focus_field(&ui.shell, &ui.editors, label, args);
        Value::Null
    });
    reg("editorState", |ui, label, args| {
        editor::editor_state(&ui.shell, &ui.editors, label, args);
        Value::Null
    });
    reg("noteLoaded", |_, _, _| Value::Null);
    reg("editorSideWidth", |ui, _, width| {
        if let Ok(width) = width.parse() {
            ui.settings.lock().unwrap().editor.side_width = width;
            ui.save_settings_now();
        }
        Value::Null
    });
    reg("ioImageLoaded", |_, _, _| Value::Null);
    // options
    reg("toggleSticky", |ui, label, args| {
        editor::toggle_sticky(&ui.shell, &ui.editors, label, args.parse().ok())
    });
    reg("toggleStickyAll", |ui, label, _| {
        editor::toggle_sticky(&ui.shell, &ui.editors, label, None)
    });
    for cmd in ["toggleMathjax", "toggleShrinkImages", "toggleCloseHTMLTags"] {
        let u = ui.clone();
        bridge.register(cmd, move |shell, label, _| {
            if is_editor(label) && editor::toggle_config(shell, cmd) == Some("renderMathjax") {
                // as Qt, redraw the page so MathJax turns on or off
                let focus = u.editors.session(label).and_then(|s| s.current_field);
                let mode = u.editors.session(label).map(|s| s.mode);
                if let Some(mode) = mode {
                    let title = match mode {
                        Mode::Add => u.main.tr.actions_add().into_owned(),
                        Mode::EditCurrent => u.main.tr.editing_edit_current().into_owned(),
                        Mode::Browse => {
                            crate::browser_window::reload_page(&u);
                            return Value::Null;
                        }
                    };
                    let _ = open_page(&u, label, &title, mode, focus);
                }
            }
            Value::Null
        });
    }
    reg("lastTextColor", |ui, _, color| {
        ui.settings.lock().unwrap().editor.last_text_color = Some(color.to_string());
        ui.save_settings_now();
        Value::Null
    });
    reg("lastHighlightColor", |ui, _, color| {
        ui.settings.lock().unwrap().editor.last_highlight_color = Some(color.to_string());
        ui.save_settings_now();
        Value::Null
    });
    reg("setTagsCollapsed", |ui, label, args| {
        let collapsed = args == "true";
        {
            let mut settings = ui.settings.lock().unwrap();
            if label == ADD {
                settings.editor.add_tags_collapsed = collapsed;
            } else {
                settings.editor.edit_tags_collapsed = collapsed;
            }
        }
        ui.save_settings_now();
        Value::Null
    });
    // links
    reg("dupes", |ui, label, _| {
        show_duplicate_list(ui, label);
        Value::Null
    });
    // Cards...: the card layout, once the note's edits are saved
    reg("cards", |ui, label, _| {
        ui.after_saved(label, "editorCards");
        Value::Null
    });
    reg("fields", |ui, _, _| {
        crate::main_window::message(ui, "Fields isn't in the shell yet; use Anki for it.");
        Value::Null
    });
    // the browser's editor only
    reg("preview", |ui, label, _| {
        if label == BROWSER {
            crate::browser_window::toggle_preview(ui);
        }
        Value::Null
    });
    reg("paste", |ui, label, _| {
        paste(ui, label);
        Value::Null
    });
    reg("cutOrCopy", |ui, label, _| {
        let tracker = ui.paste_tracker(label);
        tracker.on_cut_or_copy();
        // capture what the field wrote, once WebKit has surely written it
        std::thread::spawn(move || {
            let board = pasteboard();
            tracker.settle(|| {
                let count = crate::pasteboard::change_count(board.as_deref());
                (count, crate::pasteboard::read_html(board.as_deref()))
            });
        });
        Value::Null
    });
    reg("drop", |ui, label, args| {
        drop(ui, label, args);
        Value::Null
    });
    reg("attach", |ui, label, _| {
        let (u, label) = (ui.clone(), label.to_string());
        // the file picker waits for the main thread; not on the bridge
        std::thread::spawn(move || attach(&u, &label));
        Value::Null
    });
    reg("record", |ui, label, _| {
        record(ui, label);
        Value::Null
    });
    reg("addImageForOcclusion", |ui, label, _| {
        let (u, label) = (ui.clone(), label.to_string());
        std::thread::spawn(move || occlude_file(&u, &label));
        Value::Null
    });
    reg("addImageForOcclusionFromClipboard", |ui, label, _| {
        occlude_pasteboard(ui, label);
        Value::Null
    });
    reg("renderLatexPreview", |ui, label, args| {
        let Some(request) = editor::latex_request(&ui.shell, &ui.editors, label, args) else {
            return json!({"status": "pending"});
        };
        if let Some(result) =
            editor::cached_latex_preview(&ui.editors, &request.key, &request.request_id)
        {
            return json!({"status": "ready", "result": result});
        }
        let (u, label) = (ui.clone(), label.to_string());
        std::thread::spawn(move || {
            let result = editor::render_latex_preview(&u.shell, &request);
            if result["ok"] == json!(true) {
                editor::remember_latex_preview(&u.editors, request.key.clone(), result.clone());
            }
            u.eval(
                &label,
                format!("globalThis.receiveLatexPreviewResult?.({result});"),
            );
        });
        json!({"status": "pending"})
    });
    reg("format", |ui, label, name| {
        if name == "abbr" {
            ask_abbr(ui, label);
        } else if let Some(js) = editor::format_js(name, None) {
            ui.eval(label, js);
        }
        Value::Null
    });
    // the windows' bars
    reg("closeEditor", |ui, label, _| {
        close(ui, label);
        Value::Null
    });
    reg("addNote", |ui, label, _| {
        if label == ADD {
            add_note(ui);
        }
        Value::Null
    });
    reg("chooseNotetype", |ui, label, _| {
        if label == ADD {
            choose_notetype(ui);
        }
        Value::Null
    });
    reg("chooseDeck", |ui, label, _| {
        if label == ADD {
            choose_deck(ui);
        }
        Value::Null
    });
    reg("addHistory", |ui, label, _| {
        let history = ui
            .editors
            .session(label)
            .map(|s| s.history)
            .unwrap_or_default();
        json!(editor::history_entries(&ui.shell.api, &history))
    });
    reg("editHistory", |ui, _, nid| {
        // the browser on the note, as `AddCards.editHistory`
        if let Ok(nid) = nid.parse::<i64>() {
            crate::browser_window::open_with_search(
                ui,
                anki_proto::search::search_node::Filter::Nid(nid),
            );
        }
        Value::Null
    });
    // the second halves of after_saved
    let after = |name: &'static str, f: fn(&Arc<Ui>, &str, &str)| {
        let u = ui.clone();
        bridge.register(name, move |shell, label, args| {
            // only the editor that was asked; the token says which
            if shell
                .bridge
                .take_pending(&crate::app::pending_key(label, &format!("{name}:{args}")))
            {
                f(&u, label, args);
            }
            Value::Null
        });
    };
    after("editorLoad", |ui, label, args| {
        let nid = args.split(':').next().unwrap_or_default();
        let result = nid
            .parse()
            .map_err(anyhow::Error::from)
            .and_then(|nid| match label {
                ADD => Ok(()),
                BROWSER => {
                    crate::browser_window::editor_saved(ui);
                    Ok(())
                }
                _ => load_edit_current(ui, nid, None),
            });
        if let Err(err) = result {
            tracing::warn!(%err, "opening editor failed");
        }
    });
    after("editorClose", |ui, label, _| close_saved(ui, label));
    after("editorAdd", |ui, _, args| {
        if let Some(generation) = args.split(':').next().and_then(|g| g.parse().ok()) {
            add_saved(ui, generation);
        }
    });
    after("editorChooseNotetype", |ui, _, _| choose_notetype_saved(ui));
    after("editorExit", |ui, _, _| exit_saved(ui));
    after("editorCards", |ui, label, _| {
        crate::card_layout::open_from_editor(ui, label)
    });
}

fn show_duplicates(ui: &Ui, label: &str) {
    if let Some(js) = editor::check_duplicates_js(&ui.shell, &ui.editors, label) {
        ui.eval(label, js);
    }
}

/// "Show Duplicates": the browser on the notes with the same first field
/// (`Editor.showDupes`).
fn show_duplicate_list(ui: &Arc<Ui>, label: &str) {
    let Some(note) = ui.editors.note(label) else {
        return;
    };
    let first = note.fields.first().cloned().unwrap_or_default();
    crate::browser_window::open_with_search(
        ui,
        anki_proto::search::search_node::Filter::Dupe(anki_proto::search::search_node::Dupe {
            notetype_id: note.notetype_id,
            first_field: first,
        }),
    );
}

/// Mini Format Pack's `abbr`: ask for the full text, then wrap.
fn ask_abbr(ui: &Arc<Ui>, label: &str) {
    let label = label.to_string();
    crate::main_window::ask(
        ui,
        Dialog {
            title: "Abbreviation".into(),
            message_html: String::new(),
            fields: vec![(
                "title".into(),
                "Full text for the abbreviation:".into(),
                false,
            )],
            buttons: vec!["OK".into(), ui.main.tr.actions_cancel().into_owned()],
            default: 0,
        },
        move |ui, answer| {
            let Some(answer) = answer.filter(|a| a.button == 0) else {
                return;
            };
            let title = answer.fields.get("title").cloned().unwrap_or_default();
            if let Some(js) = editor::format_js("abbr", Some(&title)) {
                ui.eval(&label, js);
            }
        },
    );
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn hotkeys_become_physical_keys() {
        assert_eq!(
            key_spec("Cmd+Shift+["),
            json!({"code": "BracketLeft", "meta": true, "shift": true, "alt": false})
        );
        assert_eq!(
            key_spec("Alt+Shift+5"),
            json!({"code": "Digit5", "meta": false, "shift": true, "alt": true})
        );
        assert_eq!(
            key_spec("Cmd+Shift+Alt+_"),
            json!({"code": "Minus", "meta": true, "shift": true, "alt": true})
        );
        assert_eq!(key_spec("Cmd+,")["code"], json!("Comma"));
        assert_eq!(key_spec("Cmd+Shift+Alt+A")["code"], json!("KeyA"));
    }

    #[test]
    fn every_format_action_has_a_button_and_a_page_call() {
        let buttons = format_buttons_js();
        for (name, ..) in editor::FORMAT_ACTIONS {
            assert!(buttons.contains(&format!("format:{name}")), "{name}");
            if *name != "abbr" {
                assert!(editor::format_js(name, None).is_some(), "{name}");
            }
        }
    }
}
