// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The plugin host and the editor windows: the editors' events go to the
//! add-ons' `gui_hooks.editor_*` (with the note as the window has it), and
//! the host's `aqt.editor` acts on the windows: evals (with results),
//! loading or changing the note, pastes, toolbar buttons and their keys,
//! and a dock as the window's side panel.
//!
//! A dock's widgets are drawn by `widgets.js`, a page in the side panel's
//! frame, which asks the host for the widget tree and sends it the user's
//! input (`addonWidget:`); the host's changes come as `widget_update`s.
//! Web views among the widgets are frames of their own, with their HTML
//! (`widget_page`) served for the editor's webview only.

use std::collections::HashMap;
use std::collections::HashSet;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::Weak;
use std::time::Duration;

use prost::Message;
use serde_json::json;
use serde_json::Value;

use crate::app::Ui;
use crate::editor::EditorEvent;
use crate::editor_window;
use crate::pages;
use crate::pages::WebviewKind;

/// How long a widget's page waits for the add-on to handle its input.
const WIDGET_TIMEOUT: Duration = Duration::from_secs(5);
const WIDGETS_JS: &str = include_str!("widgets.js");
const WIDGETS_CSS: &str = include_str!("widgets.css");

type Buttons = (Vec<String>, Vec<(String, String)>);

#[derive(Default)]
pub struct State {
    /// Editor label -> the dock (its root widget) in its side panel, and
    /// whether it's shown.
    docks: Mutex<HashMap<String, (u64, bool)>>,
    /// Editor label -> add-ons' toolbar buttons and their keys.
    buttons: Mutex<HashMap<String, Buttons>>,
    /// Web view widget -> its page's HTML.
    pages: Mutex<HashMap<u64, String>>,
    /// The editor windows with the focus.
    focused: Mutex<HashSet<String>>,
    /// Editor label -> the widget last given the focus while its side
    /// panel's page was loading, until the page asks for its tree. The
    /// page misses updates sent before then (and its tree has their seq),
    /// so the add-on's `setFocus` on showing a new dock would be lost.
    loading: Mutex<HashMap<String, Option<u64>>>,
}

/// The page id a web view widget's HTML is served as, in editor `label`;
/// a webview may load only its own pages (`label-*`).
fn page_id(label: &str, widget: u64) -> String {
    format!("{label}-side-w{widget}")
}

fn b64(data: &[u8]) -> String {
    data_encoding::BASE64.encode(data)
}

/// What every editor event tells the host: which window, and its note.
fn payload(ui: &Ui, label: &str, with_note: bool) -> Option<Value> {
    let session = ui.editors.session(label)?;
    let mut out = json!({
        "label": label,
        "page_id": session.page_id(),
        "card_id": session.card_id.unwrap_or(0),
        "deck_id": session.deck_id,
        "focused": ui.plugins.editors.focused.lock().unwrap().contains(label),
    });
    if with_note {
        out["note"] = json!(b64(&session.note.encode_to_vec()));
    }
    Some(out)
}

/// Bridge commands and the editors' events; once, at startup.
pub fn register(ui: &Arc<Ui>) {
    let weak: Weak<Ui> = Arc::downgrade(ui);
    ui.editors.listen(Box::new(move |_, event| {
        if let Some(ui) = weak.upgrade() {
            forward(&ui, event);
        }
    }));
    let bridge = &ui.shell.bridge;
    // a side panel's widgets
    let u = ui.clone();
    bridge.register("addonWidget", move |_, label, args| {
        let drawn = editor_window::is_editor(label) || super::windows::is_window(label);
        if !drawn || !u.plugins.host.is_running() {
            return Value::Null;
        }
        let Ok(msg) = serde_json::from_str::<Value>(args) else {
            return Value::Null;
        };
        let tree = msg["op"] == "tree";
        let mut out = u
            .plugins
            .host
            .event("widget_event", msg, WIDGET_TIMEOUT)
            .unwrap_or(Value::Null);
        if tree {
            let focus = u.plugins.editors.loading.lock().unwrap().remove(label);
            if let (Some(Some(id)), Some(out)) = (focus, out.as_object_mut()) {
                out.insert("focus".into(), json!(id));
            }
        }
        out
    });
    // the second half of `call_after_note_saved`
    let u = ui.clone();
    bridge.register("addonAfterSaved", move |shell, label, args| {
        let key = crate::app::pending_key(label, &format!("addonAfterSaved:{args}"));
        if shell.bridge.take_pending(&key) {
            let callback: u64 = args.split(':').next().unwrap_or("").parse().unwrap_or(0);
            u.plugins
                .host
                .notify("editor_callback", json!({"callback": callback}));
        }
        Value::Null
    });
}

fn forward(ui: &Ui, event: &EditorEvent) {
    if !ui.plugins.host.is_running() {
        return;
    }
    let (name, label, extra) = match event {
        EditorEvent::NoteLoaded { label, .. } => ("editor_did_load_note", label, json!({})),
        EditorEvent::FieldFocused { label, ord } => {
            ("editor_did_focus_field", label, json!({"ord": ord}))
        }
        EditorEvent::FieldUnfocused { label, ord } => {
            ("editor_did_unfocus_field", label, json!({"ord": ord}))
        }
        EditorEvent::TypingTimer { label } => ("editor_did_fire_typing_timer", label, json!({})),
        EditorEvent::TagsUpdated { label } => ("editor_did_update_tags", label, json!({})),
        EditorEvent::StateChanged { label, new, old } => (
            "editor_state_did_change",
            label,
            json!({"new": new, "old": old}),
        ),
        EditorEvent::NoteAdded { label, nid } => {
            ("add_cards_did_add_note", label, json!({"nid": nid}))
        }
    };
    // after Add, the window has the next note, which its load sends
    let with_note = !matches!(event, EditorEvent::NoteAdded { .. });
    if let Some(mut msg) = payload(ui, label, with_note) {
        if let (Some(msg), Value::Object(extra)) = (msg.as_object_mut(), extra) {
            msg.extend(extra);
        }
        ui.plugins.host.notify(name, msg);
    }
}

/// An editor window gained or lost the focus.
pub fn window_focused(ui: &Ui, label: &str, focused: bool) {
    {
        let mut set = ui.plugins.editors.focused.lock().unwrap();
        if focused {
            set.insert(label.to_string());
        } else {
            set.remove(label);
        }
    }
    if let Some(msg) = payload(ui, label, false) {
        ui.plugins.host.notify("editor_focus", msg);
    }
}

/// An editor window went away.
pub fn closed(ui: &Ui, label: &str) {
    let state = &ui.plugins.editors;
    state.focused.lock().unwrap().remove(label);
    state.buttons.lock().unwrap().remove(label);
    state.loading.lock().unwrap().remove(label);
    if state.docks.lock().unwrap().remove(label).is_some() {
        for widget in state.pages.lock().unwrap().keys() {
            ui.shell.pages.remove(&page_id(label, *widget));
        }
    }
    ui.plugins
        .host
        .notify("editor_closed", json!({"label": label}));
}

/// The host stopped: its docks and pages go with it.
pub fn host_stopped(ui: &Ui) {
    let docks: Vec<String> = ui
        .plugins
        .editors
        .docks
        .lock()
        .unwrap()
        .drain()
        .map(|d| d.0)
        .collect();
    for label in docks {
        editor_window::set_side_panel(ui, &label, None, false);
    }
    for (widget, _) in ui.plugins.editors.pages.lock().unwrap().drain() {
        for label in editor_window_labels(&[]) {
            ui.shell.pages.remove(&page_id(label, widget));
        }
    }
    ui.plugins.editors.buttons.lock().unwrap().clear();
}

/// Editor `label`'s page was (re)drawn: the add-ons' buttons and side
/// panel go in it again.
pub fn page_opened(ui: &Ui, label: &str) {
    let state = &ui.plugins.editors;
    if let Some((buttons, keys)) = state.buttons.lock().unwrap().get(label) {
        editor_window::add_addon_buttons(ui, label, buttons, keys);
    }
    let dock = state.docks.lock().unwrap().get(label).copied();
    if let Some((root, visible)) = dock {
        load_side_panel(ui, label, root, visible);
    }
}

/// Unknown commands from an editor's page (an add-on's toolbar button):
/// the add-ons' `_links`, then `webview_did_receive_js_message`.
pub fn bridge_command(ui: &Ui, label: &str, cmd: &str, timeout: Duration) -> Option<Value> {
    let out = ui.plugins.host.event(
        "editor_bridge_cmd",
        json!({"label": label, "cmd": cmd}),
        timeout,
    )?;
    (out["handled"] == true).then(|| out["value"].clone())
}

/// The host's requests about editors; false if `op` isn't one.
pub fn on_host(ui: &Arc<Ui>, op: &str, msg: &Value) -> bool {
    let label = msg["label"].as_str().unwrap_or_default();
    let in_editor = editor_window::is_editor(label) && ui.editors.session(label).is_some();
    match op {
        "editor_eval" if in_editor => {
            let js = msg["js"].as_str().unwrap_or_default().to_string();
            match msg["callback"].as_u64() {
                Some(callback) => {
                    let host = ui.plugins.host.clone();
                    ui.eval_with_result(
                        label,
                        js,
                        crate::app::EvalResult::new(move |value| {
                            host.notify(
                                "editor_callback",
                                json!({"callback": callback, "value": value}),
                            )
                        }),
                    );
                }
                None => ui.eval(label, js),
            }
        }
        "editor_load_note" if in_editor => {
            if note_changed(ui, label, msg) {
                let focus = msg["focus_to"].as_u64().map(|f| f as u32);
                editor_window::reload_note(ui, label, focus);
            }
        }
        "editor_note_changed" if in_editor => {
            note_changed(ui, label, msg);
        }
        "widget_pages_closed" => pages_closed(ui, msg),
        "editor_after_saved" if in_editor => {
            let callback = msg["callback"].as_u64().unwrap_or(0);
            ui.after_saved(label, &format!("addonAfterSaved:{callback}"));
        }
        "editor_paste" if in_editor => editor_window::paste_from_addon(
            ui,
            label,
            msg["html"].as_str().unwrap_or_default(),
            msg["internal"] == true,
            msg["extended"] == true,
        ),
        "editor_check_valid" if in_editor => editor_window::check_valid(ui, label),
        "editor_raise" if in_editor => editor_window::raise(ui, label),
        "editor_buttons" if in_editor => {
            let buttons: Vec<String> =
                serde_json::from_value(msg["buttons"].clone()).unwrap_or_default();
            let keys: Vec<(String, String)> = msg["keys"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(|k| Some((k["keys"].as_str()?.into(), k["cmd"].as_str()?.into())))
                .collect();
            editor_window::add_addon_buttons(ui, label, &buttons, &keys);
            ui.plugins
                .editors
                .buttons
                .lock()
                .unwrap()
                .insert(label.to_string(), (buttons, keys));
        }
        "editor_side_panel" if in_editor => side_panel(ui, label, msg),
        "widget_update" => widget_update(ui, msg),
        "widget_page" => widget_page(ui, msg),
        op if op.starts_with("editor_") => {
            tracing::debug!(op, label, "editor request for a closed window");
        }
        _ => return false,
    }
    true
}

/// Fields and tags an add-on changed in its copy of the window's note: the
/// window's next save (or Add) has them, as Qt's would. Only those, so what
/// the user typed since stays. False if the window shows another note now.
fn note_changed(ui: &Ui, label: &str, msg: &Value) -> bool {
    let page_id = msg["page_id"].as_i64();
    ui.editors
        .update(label, |s| {
            if page_id != Some(s.page_id()) {
                tracing::debug!(label, "add-on's change is for a note no longer shown");
                return false;
            }
            if let Some(fields) = msg["fields"].as_object() {
                for (ord, html) in fields {
                    let (Ok(ord), Some(html)) = (ord.parse::<usize>(), html.as_str()) else {
                        continue;
                    };
                    if let Some(field) = s.note.fields.get_mut(ord) {
                        *field = html.to_string();
                    }
                }
            }
            if let Some(tags) = msg["tags"].as_array() {
                s.note.tags = tags
                    .iter()
                    .filter_map(|t| t.as_str().map(str::to_string))
                    .collect();
            }
            true
        })
        .unwrap_or(false)
}

/// A closed window's web views: their pages aren't needed any more.
fn pages_closed(ui: &Ui, msg: &Value) {
    let state = &ui.plugins.editors;
    let labels: Vec<String> = state.docks.lock().unwrap().keys().cloned().collect();
    let mut pages = state.pages.lock().unwrap();
    for widget in msg["widgets"].as_array().into_iter().flatten() {
        let Some(widget) = widget.as_u64() else {
            continue;
        };
        pages.remove(&widget);
        for label in editor_window_labels(&labels) {
            ui.shell.pages.remove(&page_id(label, widget));
        }
        for label in super::windows::labels(ui) {
            ui.shell.pages.remove(&page_id(&label, widget));
        }
    }
}

/// Both editors' labels (a closed window's pages were served for it too).
fn editor_window_labels(open: &[String]) -> Vec<&str> {
    let mut labels = vec![editor_window::ADD, crate::app::EDITOR];
    labels.extend(open.iter().map(String::as_str));
    labels.dedup();
    labels
}

/// A dock added to the window, shown or hidden. Its page loads with the
/// window (hidden, so web views get their evals as in Qt) and stays.
fn side_panel(ui: &Ui, label: &str, msg: &Value) {
    let Some(root) = msg["root"].as_u64() else {
        return;
    };
    let visible = msg["visible"] == true;
    let state = &ui.plugins.editors;
    let before = state
        .docks
        .lock()
        .unwrap()
        .insert(label.to_string(), (root, visible));
    if before.map(|d| d.0) == Some(root) {
        editor_window::show_side_panel(ui, label, visible);
    } else {
        load_side_panel(ui, label, root, visible);
    }
}

fn load_side_panel(ui: &Ui, label: &str, root: u64, visible: bool) {
    serve_pages(ui, label);
    ui.plugins
        .editors
        .loading
        .lock()
        .unwrap()
        .insert(label.to_string(), None);
    editor_window::set_side_panel(
        ui,
        label,
        Some(widgets_html(ui, label, root, false)),
        visible,
    );
}

/// The web views' pages, served for webview `label`.
pub(super) fn serve_pages(ui: &Ui, label: &str) {
    for (widget, html) in ui.plugins.editors.pages.lock().unwrap().iter() {
        ui.shell.pages.set(
            &page_id(label, *widget),
            WebviewKind::AddonView,
            Some(html.clone()),
        );
    }
}

/// ... no longer (its window closed).
pub(super) fn forget_pages(ui: &Ui, label: &str) {
    for widget in ui.plugins.editors.pages.lock().unwrap().keys() {
        ui.shell.pages.remove(&page_id(label, *widget));
    }
}

/// The page drawing `root`'s widgets in webview `label`: a dock in an
/// editor's side panel, or an add-on window (`window`).
pub(super) fn widgets_html(ui: &Ui, label: &str, root: u64, window: bool) -> String {
    let setup = json!({"root": root, "label": label, "window": window});
    pages::std_html(
        &ui.shell.web,
        &format!(
            "<style>{WIDGETS_CSS}</style><div id=widgets></div>\
             <script>window.shellWidgetsSetup = {setup};</script><script>{WIDGETS_JS}</script>"
        ),
        &[],
        &[],
        true,
    )
}

fn widget_update(ui: &Ui, msg: &Value) {
    let Some(root) = msg["root"].as_u64() else {
        return;
    };
    let label = ui
        .plugins
        .editors
        .docks
        .lock()
        .unwrap()
        .iter()
        .find(|(_, d)| d.0 == root)
        .map(|(l, _)| l.clone());
    let update = json!({"seq": msg["seq"], "ops": msg["ops"]});
    if let Some(label) = label {
        if let Some(focus) = ui.plugins.editors.loading.lock().unwrap().get_mut(&label) {
            if let Some(id) = focused_widget(&msg["ops"]) {
                *focus = Some(id);
            }
        }
        ui.eval(
            &label,
            format!(
                "document.getElementById(\"shell-side\")?.contentWindow?.shellWidgets?.apply({update});"
            ),
        );
    } else {
        super::windows::update(ui, root, &update);
    }
}

/// The widget a batch of ops last gives the focus.
fn focused_widget(ops: &Value) -> Option<u64> {
    ops.as_array()?
        .iter()
        .rev()
        .find(|op| op["focus"] == true)
        .and_then(|op| op["id"].as_u64())
}

/// A web view's page (`AnkiWebView.stdHtml`). Its commands go to the view's
/// bridge handler, through the dock's page.
fn widget_page(ui: &Ui, msg: &Value) {
    let Some(widget) = msg["widget"].as_u64() else {
        return;
    };
    let strings = |key: &str| -> Vec<String> {
        msg[key]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect()
    };
    let (css, js) = (strings("css"), strings("js"));
    let css: Vec<&str> = css.iter().map(String::as_str).collect();
    let js: Vec<&str> = js.iter().map(String::as_str).collect();
    let html = pages::std_html(
        &ui.shell.web,
        msg["body"].as_str().unwrap_or_default(),
        &css,
        &js,
        msg["default_css"] != false,
    );
    // The view is a sandboxed frame (an opaque origin, so its HTML, which
    // may come from a model, can't reach the editor's bridge or API, as
    // Qt's own webview couldn't); it talks to the dock's page by messages.
    let reroute = format!(
        r#"<script>(() => {{
    const id = {widget};
    let n = 0;
    const waiting = new Map();
    const post = (m) => parent.postMessage({{ shellWidget: id, ...m }}, "*");
    const jsonable = (v) => {{ try {{ return JSON.parse(JSON.stringify(v ?? null)); }} catch {{ return null; }} }};
    window.pycmd = window.bridgeCommand = (arg, cb) => {{
        const k = ++n;
        if (cb) waiting.set(k, cb);
        post({{ bridge: String(arg), k }});
        return false;
    }};
    window.pycmdPromise = (arg) => new Promise((resolve) => window.pycmd(arg, resolve));
    addEventListener("message", (e) => {{
        if (e.source !== parent || !e.data) return;
        const m = e.data;
        if (m.reply) {{
            const cb = waiting.get(m.k);
            waiting.delete(m.k);
            cb?.(m.value);
        }} else if (typeof m.eval === "string") {{
            let value = null;
            try {{ value = (0, eval)(m.eval); }} catch (err) {{ console.error("add-on eval", err); }}
            if (m.n) post({{ result: jsonable(value), n: m.n }});
        }}
    }});
    document.addEventListener("click", (e) => {{
        const link = e.target instanceof Element && e.target.closest("a[href]");
        if (!link || e.defaultPrevented) return;
        const url = new URL(link.href, location.href);
        if (["http:", "https:", "mailto:"].includes(url.protocol)) {{
            e.preventDefault();
            post({{ openUrl: url.href }});
        }}
    }});
}})();</script>"#
    );
    let head = msg["head"].as_str().unwrap_or_default();
    let html = html.replacen("</head>", &format!("{reroute}{head}</head>"), 1);
    ui.plugins
        .editors
        .pages
        .lock()
        .unwrap()
        .insert(widget, html.clone());
    let mut labels: Vec<String> = ui
        .plugins
        .editors
        .docks
        .lock()
        .unwrap()
        .keys()
        .cloned()
        .collect();
    labels.extend(super::windows::labels(ui));
    for label in labels {
        ui.shell.pages.set(
            &page_id(&label, widget),
            WebviewKind::AddonView,
            Some(html.clone()),
        );
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn the_last_focus_op_wins() {
        let ops = json!([
            {"id": 1, "focus": true},
            {"id": 2, "props": {}},
            {"id": 3, "focus": true},
            {"id": 4, "eval": "x"},
        ]);
        assert_eq!(focused_widget(&ops), Some(3));
        assert_eq!(focused_widget(&json!([{"id": 2, "props": {}}])), None);
    }
}
