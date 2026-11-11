// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The plugin host and the windows: starting it with the profile, what
//! it asks the shell for (tooltips, menu items, panels, page refreshes),
//! and the events the shell sends it.
//!
//! Add-on panels are windows showing a page from the add-on's folder
//! (`/_addons/...`); everything their pages send over the bridge goes to
//! the add-on. Unknown commands from the main window's pages go to the
//! add-ons' `webview_did_receive_js_message`, as in Qt.

use std::collections::HashMap;
use std::io::Read;
use std::io::Seek;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;

use prost::Message;
use serde::Deserialize;
use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use super::HostConfig;
use super::PluginHost;
use crate::app::Ui;
use crate::app::MAIN;
use crate::main_window;
use crate::main_window::MainState;
use crate::pages;
use crate::pages::WebviewKind;

const PANEL_PREFIX: &str = "addon-panel-";
const LOG_WINDOW: &str = "addon-log";
/// How long the shell waits for add-ons while drawing a page or a menu.
const RENDER_TIMEOUT: Duration = Duration::from_millis(400);
/// ... for a panel's message, which the add-on handles, and for a main
/// window page's unknown command, which holds up the page's later ones.
const MESSAGE_TIMEOUT: Duration = Duration::from_secs(5);
const UNKNOWN_COMMAND_TIMEOUT: Duration = Duration::from_secs(1);

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
pub struct MenuItem {
    pub id: u64,
    pub text: String,
    #[serde(default = "enabled")]
    pub enabled: bool,
    /// Qt's key sequence ("Ctrl+Alt+Shift+Q"; Ctrl is Cmd).
    #[serde(default)]
    pub shortcut: Option<String>,
}

fn enabled() -> bool {
    true
}

pub struct Plugins {
    pub host: Arc<PluginHost>,
    /// Open panels: window label -> panel id.
    panels: Mutex<HashMap<String, u64>>,
    /// The add-ons' items in Tools.
    pub tools: Mutex<Vec<MenuItem>>,
    pub editors: super::editors::State,
    pub windows: super::windows::State,
}

impl Plugins {
    pub fn new(shell: Arc<crate::Shell>) -> Self {
        Plugins {
            host: PluginHost::new(shell),
            panels: Mutex::new(HashMap::new()),
            tools: Mutex::new(vec![]),
            editors: Default::default(),
            windows: Default::default(),
        }
    }
}

pub fn state_name(state: MainState) -> &'static str {
    match state {
        MainState::Home => "profileManager",
        MainState::DeckBrowser => "deckBrowser",
        MainState::Overview => "overview",
        MainState::Review => "review",
    }
}

pub fn log_path(ui: &Ui) -> std::path::PathBuf {
    ui.settings_path.with_file_name("plugin-host.log")
}

/// Bridge commands and the backend's change notifications; once, at
/// startup.
pub fn register(ui: &Arc<Ui>) {
    let u = ui.clone();
    ui.shell
        .bridge
        .route_prefix(PANEL_PREFIX, move |_, label, cmd| {
            // test scripts evaluated in a panel report through the test channel
            if u.test_mode && cmd.starts_with("test") {
                return None;
            }
            let panel = u.plugins.panels.lock().unwrap().get(label).copied();
            Some(
                panel
                    .and_then(|panel| {
                        u.plugins.host.event(
                            "panel_message",
                            json!({"panel": panel, "message": cmd}),
                            MESSAGE_TIMEOUT,
                        )
                    })
                    .unwrap_or(Value::Null),
            )
        });
    let u = ui.clone();
    ui.shell.bridge.set_fallback(move |_, label, cmd| {
        if !u.plugins.host.is_running() {
            return None;
        }
        if crate::editor_window::is_editor(label) {
            return super::editors::bridge_command(&u, label, cmd, UNKNOWN_COMMAND_TIMEOUT);
        }
        if label != MAIN {
            return None;
        }
        let context = state_name(*u.main.state.lock().unwrap());
        let out = u.plugins.host.event(
            "webview_did_receive_js_message",
            json!({"message": cmd, "context": context}),
            UNKNOWN_COMMAND_TIMEOUT,
        )?;
        (out["handled"] == true).then(|| out["value"].clone())
    });
    super::editors::register(ui);
    let u = ui.clone();
    ui.shell
        .bridge
        .register("addonContent", move |_, label, _| {
            if label != MAIN || *u.main.state.lock().unwrap() != MainState::DeckBrowser {
                return Value::Null;
            }
            u.plugins
                .host
                .event(
                    "deck_browser_will_render_content",
                    json!({}),
                    RENDER_TIMEOUT,
                )
                .map_or(Value::Null, |out| out["stats"].clone())
        });
    // the shell's own operations (answering, undo, AnkiConnect, ...)
    let host = ui.plugins.host.clone();
    ui.shell
        .api
        .add_changes_observer(Box::new(move |_method, changes| {
            if !host.is_running() {
                return;
            }
            let decoded = anki_proto::collection::OpChanges::decode(changes).unwrap_or_default();
            if decoded != Default::default() {
                host.notify(
                    "operation_did_execute",
                    json!({"changes": data_encoding::BASE64.encode(changes)}),
                );
            }
        }));
}

/// After the profile opened: start the add-ons the settings list.
pub fn start(ui: &Arc<Ui>) {
    let (addons, python, base) = {
        let settings = ui.settings.lock().unwrap();
        (
            settings.plugins.clone(),
            settings.import_python.clone(),
            settings.anki_base(),
        )
    };
    if addons.is_empty() {
        return;
    }
    let Some(profile) = ui.shell.open_profile_paths() else {
        return;
    };
    let Some((script, paths)) = HostConfig::locate() else {
        tracing::error!("the plugin host isn't in this build");
        return;
    };
    let Some(python) = python.or_else(crate::settings::find_python) else {
        main_window::message(
            ui,
            "Add-ons need an Anki install's Python; set import_python in the settings.",
        );
        return;
    };
    ui.shell.addon_files.set_folder(Some(base.join("addons21")));
    let u = ui.clone();
    ui.plugins
        .host
        .set_handler(Arc::new(move |op, msg| on_host(&u, op, msg)));
    ui.plugins.host.start(HostConfig {
        python,
        script,
        paths,
        anki_base: base,
        profile,
        addons,
        log: log_path(ui),
    });
}

/// Before the profile closes.
pub fn stop(ui: &Arc<Ui>) {
    ui.plugins.host.stop();
    forget_host_ui(ui);
    ui.shell.addon_files.set_folder(None);
}

pub fn restart(ui: &Arc<Ui>) {
    stop(ui);
    start(ui);
}

/// Send an event without waiting for add-ons.
pub fn notify(ui: &Ui, event: &str, fields: Value) {
    ui.plugins.host.notify(event, fields);
}

pub fn state_changed(ui: &Ui, old: MainState, new: MainState) {
    if old != new {
        notify(
            ui,
            "state_did_change",
            json!({"new": state_name(new), "old": state_name(old)}),
        );
    }
}

/// Add-on items for the reviewer's "More" menu.
pub fn context_menu(ui: &Ui, card_id: i64) -> Vec<MenuItem> {
    ui.plugins
        .host
        .event(
            "reviewer_will_show_context_menu",
            json!({"card_id": card_id}),
            RENDER_TIMEOUT,
        )
        .and_then(|items| serde_json::from_value(items).ok())
        .unwrap_or_default()
}

pub fn menu_action(ui: &Ui, id: u64) {
    notify(ui, "menu_action", json!({"id": id}));
}

/// A window went away: an add-on panel (closed by the user, or by the
/// add-on), or an editor.
pub fn window_destroyed(ui: &Ui, label: &str) {
    if let Some(panel) = ui.plugins.panels.lock().unwrap().remove(label) {
        notify(ui, "panel_closed", json!({"panel": panel}));
    }
    if crate::editor_window::is_editor(label) {
        super::editors::closed(ui, label);
    }
    super::windows::destroyed(ui, label);
}

pub fn window_focused(ui: &Ui, label: &str, focused: bool) {
    if crate::editor_window::is_editor(label) {
        super::editors::window_focused(ui, label, focused);
    }
}

/// The host's end: its menu items and panels go with it.
fn forget_host_ui(ui: &Arc<Ui>) {
    super::editors::host_stopped(ui);
    super::windows::host_stopped(ui);
    crate::browser_window::host_stopped(ui);
    ui.plugins.tools.lock().unwrap().clear();
    crate::menus::set_tools(ui, vec![]);
    let labels: Vec<String> = ui
        .plugins
        .panels
        .lock()
        .unwrap()
        .drain()
        .map(|p| p.0)
        .collect();
    for label in labels {
        if let Some(window) = ui.app.get_webview_window(&label) {
            let _ = window.destroy();
        }
    }
}

fn on_host(ui: &Arc<Ui>, op: &str, msg: &Value) -> Option<Value> {
    let text = msg["text"].as_str().unwrap_or_default();
    if super::editors::on_host(ui, op, msg) || super::windows::on_host(ui, op, msg) {
        return None;
    }
    if let Some(reply) = crate::browser_window::on_host(ui, op, msg) {
        return reply;
    }
    match op {
        "ready" => {
            tracing::info!(addons = %msg["addons"], "add-ons loaded");
            // a (re)started host starts out on the deck list
            let state = state_name(*ui.main.state.lock().unwrap());
            notify(ui, "set_state", json!({"state": state}));
            notify(ui, "set_theme", json!({"night": ui.shell.night.get()}));
            crate::browser_window::host_ready(ui);
        }
        "addon_failed" => {
            let module = msg["module"].as_str().unwrap_or_default();
            tracing::error!(module, error = %msg["error"], "add-on failed to load");
            main_window::tooltip(
                ui,
                &format!("The {module} add-on failed to load; see Tools > Add-on Log."),
            );
        }
        "stopped" => {
            forget_host_ui(ui);
            if msg["restart"] == true {
                main_window::tooltip(ui, "Add-ons stopped unexpectedly; restarting them.");
            } else {
                main_window::message(
                    ui,
                    "Add-ons kept stopping, so they're off until you restart them \
                     (Tools > Restart Add-ons). See Tools > Add-on Log.",
                );
            }
        }
        "tooltip" => match msg["window"].as_str() {
            Some(label)
                if crate::editor_window::is_editor(label) || super::windows::is_window(label) =>
            {
                crate::editor_window::tooltip(ui, label, text)
            }
            _ => main_window::tooltip(ui, text),
        },
        "choose_folder" => {
            let folder = crate::pasteboard::choose_folder(
                &ui.app,
                msg["title"].as_str().unwrap_or_default(),
                msg["folder"].as_str().unwrap_or_default(),
            );
            return Some(json!(folder.map(|f| f.to_string_lossy().into_owned())));
        }
        "show_info" => main_window::message(ui, text),
        "operation_did_execute" => {
            let changes = msg["changes"]
                .as_str()
                .and_then(|s| data_encoding::BASE64.decode(s.as_bytes()).ok())
                .and_then(|b| anki_proto::collection::OpChanges::decode(b.as_slice()).ok());
            if let Some(changes) = changes {
                main_window::plugin_op_executed(ui, &changes);
                crate::browser_window::plugin_op_executed(ui, &changes);
            }
        }
        "menu" if msg["menu"] == "tools" => {
            let items: Vec<MenuItem> =
                serde_json::from_value(msg["items"].clone()).unwrap_or_default();
            *ui.plugins.tools.lock().unwrap() = items.clone();
            crate::menus::set_tools(ui, items);
        }
        "web_exports" => ui.shell.addon_files.set_exports(
            msg["module"].as_str().unwrap_or_default(),
            msg["pattern"].as_str().unwrap_or_default(),
        ),
        "open_panel" => open_panel(ui, msg),
        "panel_eval" => {
            if let Some(label) = panel_label(ui, msg["panel"].as_u64()) {
                ui.eval(&label, msg["js"].as_str().unwrap_or_default().into());
            }
        }
        "panel_close" => {
            if let Some(label) = panel_label(ui, msg["panel"].as_u64()) {
                if let Some(window) = ui.app.get_webview_window(&label) {
                    let _ = window.destroy();
                }
            }
        }
        "refresh_main" => main_window::refresh(ui),
        "browser_search" => {
            let search = msg["search"].as_str().unwrap_or_default();
            let normalized =
                crate::browser::normalize(&ui.shell.api, crate::browser::text_node(search));
            match normalized {
                Ok(search) => crate::browser_window::queue(ui, move |ui| {
                    crate::browser_window::open(ui, Some(search), None)
                }),
                Err(err) => main_window::message(ui, &err.message),
            }
        }
        op => tracing::warn!(op, "unknown plugin host request"),
    }
    None
}

fn panel_label(ui: &Ui, panel: Option<u64>) -> Option<String> {
    let panel = panel?;
    ui.plugins
        .panels
        .lock()
        .unwrap()
        .iter()
        .find(|(_, id)| **id == panel)
        .map(|(label, _)| label.clone())
}

fn open_panel(ui: &Arc<Ui>, msg: &Value) {
    let Some(panel) = msg["panel"].as_u64() else {
        return;
    };
    let label = format!("{PANEL_PREFIX}{panel}");
    let url = format!(
        "{}_addons/{}/{}",
        pages::ORIGIN,
        msg["module"].as_str().unwrap_or_default(),
        msg["path"].as_str().unwrap_or_default()
    );
    ui.plugins
        .panels
        .lock()
        .unwrap()
        .insert(label.clone(), panel);
    let size = |key: &str, default: f64| msg[key].as_f64().unwrap_or(default);
    if let Err(err) = ui.open_url_window(
        &label,
        msg["title"].as_str().unwrap_or("Add-on"),
        WebviewKind::Dialog,
        &url,
        (size("width", 600.0), size("height", 500.0)),
    ) {
        tracing::error!(%err, url, "opening add-on panel failed");
        window_destroyed(ui, &label);
    }
}

/// Tools > Add-on Log: the end of the host's output.
pub fn show_log(ui: &Arc<Ui>) {
    const TAIL: u64 = 256 * 1024;
    let mut text = String::new();
    if let Ok(mut file) = std::fs::File::open(log_path(ui)) {
        let len = file.metadata().map(|m| m.len()).unwrap_or(0);
        let _ = file.seek(std::io::SeekFrom::Start(len.saturating_sub(TAIL)));
        let mut bytes = vec![];
        let _ = file.read_to_end(&mut bytes);
        text = String::from_utf8_lossy(&bytes).into_owned();
    }
    if text.is_empty() {
        text = "Nothing logged yet.".into();
    }
    let html = pages::std_html(
        &ui.shell.web,
        &format!(
            "<style>body {{ margin: 0; }} pre {{ margin: 0; padding: 10px; font-size: 11px; \
             white-space: pre-wrap; }}</style><pre>{}</pre>\
             <script>window.scrollTo(0, document.body.scrollHeight);</script>",
            htmlescape::encode_minimal(&text)
        ),
        &[],
        &[],
        true,
    );
    let _ = ui.open_window(
        LOG_WINDOW,
        "Add-on Log",
        WebviewKind::Dialog,
        crate::app::Content::Html(html),
    );
}
