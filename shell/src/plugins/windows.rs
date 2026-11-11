// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Add-on windows: a main window or dialog an add-on shows (the agent
//! queue, its prompts). Each is a shell window drawing the widget's tree
//! with the side panels' page (`widgets.js`), so its input goes to the
//! host as `addonWidget:` and the host's changes come as `widget_update`s,
//! as for a dock. Closing the window tells the host, which runs the
//! widget's `closeEvent` (a dialog's rejects it).
//!
//! Each opening gets its own label (`addon-window-<root>-<n>`): a window
//! being destroyed is still there for a moment, and its `Destroyed` must
//! not close the root's next window.

use std::collections::HashMap;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::sync::Mutex;

use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use crate::app::Ui;
use crate::pages::WebviewKind;

pub const PREFIX: &str = "addon-window-";

#[derive(Default)]
pub struct State {
    /// The windows open now: root -> label.
    open: Mutex<HashMap<u64, String>>,
    openings: AtomicU64,
}

pub fn is_window(label: &str) -> bool {
    label.starts_with(PREFIX)
}

/// The root in a window's label, or in the host's name for it
/// (`addon-window-<root>`, for its tooltips).
fn root_of(label: &str) -> Option<u64> {
    let rest = label.strip_prefix(PREFIX)?;
    rest.split('-').next()?.parse().ok()
}

fn current(ui: &Ui, root: u64) -> Option<String> {
    ui.plugins.windows.open.lock().unwrap().get(&root).cloned()
}

/// The open window the host means by `label` (its tooltips' window).
pub fn resolve(ui: &Ui, label: &str) -> Option<String> {
    current(ui, root_of(label)?)
}

/// The open windows' labels (their web views' pages are served for them).
pub fn labels(ui: &Ui) -> Vec<String> {
    let open = ui.plugins.windows.open.lock().unwrap();
    open.values().cloned().collect()
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
            let window = current(ui, root).and_then(|l| ui.app.get_webview_window(&l));
            if let Some(window) = window {
                let _ = window.set_title(msg["title"].as_str().unwrap_or_default());
            }
        }
        _ => return false,
    }
    true
}

fn open(ui: &Ui, root: u64, msg: &Value) {
    let state = &ui.plugins.windows;
    let label = {
        let mut open = state.open.lock().unwrap();
        if open.contains_key(&root) {
            drop(open);
            raise(ui, root);
            return;
        }
        let n = state.openings.fetch_add(1, Ordering::Relaxed) + 1;
        let label = format!("{PREFIX}{root}-{n}");
        open.insert(root, label.clone());
        label
    };
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
        state.open.lock().unwrap().remove(&root);
        super::editors::forget_pages(ui, &label);
        notify_closed(ui, root);
    }
}

/// The add-on closed it: the host knows, so it isn't told.
fn close(ui: &Ui, root: u64) {
    let label = ui.plugins.windows.open.lock().unwrap().remove(&root);
    if let Some(label) = label {
        destroy(ui, &label);
    }
}

fn destroy(ui: &Ui, label: &str) {
    super::editors::forget_pages(ui, label);
    if let Some(window) = ui.app.get_webview_window(label) {
        let _ = window.destroy();
    }
}

fn raise(ui: &Ui, root: u64) {
    let window = current(ui, root).and_then(|l| ui.app.get_webview_window(&l));
    if let Some(window) = window {
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
/// A root's earlier window (the add-on closed it) isn't its window now.
pub fn destroyed(ui: &Ui, label: &str) {
    let Some(root) = root_of(label) else {
        return;
    };
    let gone = {
        let mut open = ui.plugins.windows.open.lock().unwrap();
        let current = open.get(&root).is_some_and(|l| l == label);
        current && open.remove(&root).is_some()
    };
    if gone {
        super::editors::forget_pages(ui, label);
        notify_closed(ui, root);
    }
}

/// The host's changes for `root`, if it's a window's.
pub fn update(ui: &Ui, root: u64, update: &Value) -> bool {
    let Some(label) = current(ui, root) else {
        return false;
    };
    ui.eval(&label, format!("window.shellWidgets?.apply({update});"));
    true
}

/// The host stopped: its windows go with it.
pub fn host_stopped(ui: &Ui) {
    let labels: Vec<String> = ui
        .plugins
        .windows
        .open
        .lock()
        .unwrap()
        .drain()
        .map(|w| w.1)
        .collect();
    for label in labels {
        destroy(ui, &label);
    }
}

#[cfg(test)]
mod test {
    use super::root_of;

    #[test]
    fn a_windows_root_is_in_its_label_and_the_hosts_name_for_it() {
        assert_eq!(root_of("addon-window-12-3"), Some(12));
        assert_eq!(root_of("addon-window-12"), Some(12));
        assert_eq!(root_of("addon-panel-12"), None);
        assert_eq!(root_of("addon-window-x"), None);
    }
}
