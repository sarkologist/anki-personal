// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Add-on windows: a main window or dialog an add-on shows (the agent
//! queue, its prompts). Each is a shell window drawing the widget's tree
//! with the side panels' page (`widgets.js`), so its input goes to the
//! host as `addonWidget:` and the host's changes come as `widget_update`s,
//! as for a dock. Closing the window tells the host, which runs the
//! widget's `closeEvent` (a dialog's rejects it).

use std::collections::HashSet;
use std::sync::Mutex;

use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use crate::app::Ui;
use crate::pages::WebviewKind;

pub const PREFIX: &str = "addon-window-";

#[derive(Default)]
pub struct State {
    /// The roots of the windows open now.
    open: Mutex<HashSet<u64>>,
}

pub fn label(root: u64) -> String {
    format!("{PREFIX}{root}")
}

pub fn is_window(label: &str) -> bool {
    label.starts_with(PREFIX)
}

fn root_of(label: &str) -> Option<u64> {
    label.strip_prefix(PREFIX)?.parse().ok()
}

/// The open windows' labels (their web views' pages are served for them).
pub fn labels(ui: &Ui) -> Vec<String> {
    let open = ui.plugins.windows.open.lock().unwrap();
    open.iter().map(|root| label(*root)).collect()
}

pub fn on_host(ui: &Ui, op: &str, msg: &Value) -> bool {
    let Some(root) = msg["root"].as_u64() else {
        return false;
    };
    match op {
        "widget_window" => open(ui, root, msg),
        "widget_window_close" => close(ui, root),
        "widget_window_raise" => raise(ui, root),
        "widget_window_title" => {
            if let Some(window) = ui.app.get_webview_window(&label(root)) {
                let _ = window.set_title(msg["title"].as_str().unwrap_or_default());
            }
        }
        _ => return false,
    }
    true
}

fn open(ui: &Ui, root: u64, msg: &Value) {
    let label = label(root);
    if !ui.plugins.windows.open.lock().unwrap().insert(root) {
        raise(ui, root);
        return;
    }
    super::editors::serve_pages(ui, &label);
    let html = super::editors::widgets_html(ui, &label, root, true);
    let size = |key: &str, default: f64| msg[key].as_f64().unwrap_or(default);
    let title = msg["title"]
        .as_str()
        .filter(|t| !t.is_empty())
        .unwrap_or("Add-on");
    if let Err(err) = ui.open_html_window(
        &label,
        title,
        WebviewKind::Dialog,
        html,
        (size("width", 640.0), size("height", 480.0)),
    ) {
        tracing::error!(%err, "opening add-on window failed");
        ui.plugins.windows.open.lock().unwrap().remove(&root);
        notify_closed(ui, root);
    }
}

/// The add-on closed it: the host knows, so it isn't told.
fn close(ui: &Ui, root: u64) {
    if ui.plugins.windows.open.lock().unwrap().remove(&root) {
        destroy(ui, root);
    }
}

fn destroy(ui: &Ui, root: u64) {
    let label = label(root);
    super::editors::forget_pages(ui, &label);
    if let Some(window) = ui.app.get_webview_window(&label) {
        let _ = window.destroy();
    }
}

fn raise(ui: &Ui, root: u64) {
    if let Some(window) = ui.app.get_webview_window(&label(root)) {
        if !ui.background {
            let _ = window.unminimize();
            let _ = window.set_focus();
        }
    }
}

fn notify_closed(ui: &Ui, root: u64) {
    ui.plugins.host.notify(
        "widget_event",
        json!({"root": root, "op": "closed", "id": root}),
    );
}

/// A window went away; one the user closed is the host's to know about.
pub fn destroyed(ui: &Ui, label: &str) {
    let Some(root) = root_of(label) else {
        return;
    };
    if ui.plugins.windows.open.lock().unwrap().remove(&root) {
        super::editors::forget_pages(ui, label);
        notify_closed(ui, root);
    }
}

/// The host's changes for `root`, if it's a window's.
pub fn update(ui: &Ui, root: u64, update: &Value) -> bool {
    if !ui.plugins.windows.open.lock().unwrap().contains(&root) {
        return false;
    }
    ui.eval(
        &label(root),
        format!("window.shellWidgets?.apply({update});"),
    );
    true
}

/// The host stopped: its windows go with it.
pub fn host_stopped(ui: &Ui) {
    let roots: Vec<u64> = ui.plugins.windows.open.lock().unwrap().drain().collect();
    for root in roots {
        destroy(ui, root);
    }
}
