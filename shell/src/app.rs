// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The Tauri app: windows, the `anki://` protocol, menus, timers, and the
//! profile lifecycle.

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::Mutex;

use anyhow::Context;
use anyhow::Result;
use serde_json::json;
use serde_json::Value;
use tauri::menu::MenuBuilder;
use tauri::menu::MenuItemBuilder;
use tauri::menu::PredefinedMenuItem;
use tauri::menu::SubmenuBuilder;
use tauri::AppHandle;
use tauri::Manager;
use tauri::RunEvent;
use tauri::WebviewUrl;
use tauri::WebviewWindowBuilder;

use crate::api::Api;
use crate::editor::Editors;
use crate::pages;
use crate::pages::WebviewKind;
use crate::profile::OpenError;
use crate::profile::ProfilePaths;
use crate::settings::Settings;
use crate::testchan;
use crate::Shell;

pub const MAIN: &str = "main";

#[derive(Debug, Clone)]
pub struct Args {
    pub settings: PathBuf,
    /// Overrides the settings' Anki base folder for this run.
    pub anki_base: Option<PathBuf>,
    pub profile: Option<String>,
    pub web: Option<PathBuf>,
    /// None disables AnkiConnect.
    pub ankiconnect_port: Option<u16>,
    pub import: bool,
    pub test: Option<testchan::TestRun>,
    pub bench: bool,
    pub geometry: Option<(f64, f64, f64, f64)>,
    /// Run without taking focus: no Dock icon, never activated, windows
    /// open unfocused. Implied by --test and --bench.
    pub background: bool,
}

impl Args {
    pub fn parse(mut it: impl Iterator<Item = String>) -> Result<Args> {
        let mut args = Args {
            settings: dirs::data_dir()
                .context("no data dir")?
                .join("AnkiShell/settings.json"),
            anki_base: None,
            profile: None,
            web: None,
            ankiconnect_port: Some(8765),
            import: false,
            test: None,
            bench: false,
            geometry: None,
            background: false,
        };
        let mut test_script = None;
        let mut test_page = String::from("editor");
        let mut test_note = None;
        while let Some(flag) = it.next() {
            let mut value = || it.next().with_context(|| format!("{flag} needs a value"));
            match flag.as_str() {
                "--settings" => args.settings = value()?.into(),
                "--anki-base" => args.anki_base = Some(value()?.into()),
                "--profile" => args.profile = Some(value()?),
                "--web" => args.web = Some(value()?.into()),
                "--ankiconnect-port" => {
                    let port: u16 = value()?.parse()?;
                    args.ankiconnect_port = (port != 0).then_some(port);
                }
                "--import" => args.import = true,
                "--test" => test_script = Some(PathBuf::from(value()?)),
                "--test-page" => test_page = value()?,
                "--test-note" => test_note = Some(value()?.parse()?),
                "--bench" => args.bench = true,
                "--background" => args.background = true,
                "--geometry" => {
                    let v: Vec<f64> = value()?
                        .split(',')
                        .map(|p| p.trim().parse())
                        .collect::<Result<_, _>>()?;
                    anyhow::ensure!(v.len() == 4, "--geometry x,y,width,height");
                    args.geometry = Some((v[0], v[1], v[2], v[3]));
                }
                other => anyhow::bail!("unknown argument {other}"),
            }
        }
        args.background |= args.bench || test_script.is_some();
        if let Some(script) = test_script {
            args.test = Some(testchan::TestRun {
                script,
                page: test_page,
                note: test_note,
            });
        }
        Ok(args)
    }
}

/// Bundled assets: `Resources/web` in the app bundle, else the dev build's
/// `out/qt/_aqt/data/web`.
fn find_web(args: &Args) -> Result<PathBuf> {
    if let Some(web) = &args.web {
        return Ok(web.clone());
    }
    let exe = std::env::current_exe()?;
    let bundled = exe
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.join("Resources/web"));
    let candidates = bundled.into_iter().chain([
        PathBuf::from("out/qt/_aqt/data/web"),
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../out/qt/_aqt/data/web"),
    ]);
    for dir in candidates {
        if dir.join("js/editor.js").exists() {
            return Ok(std::fs::canonicalize(dir)?);
        }
    }
    anyhow::bail!("web assets not found; build them with ./ninja shell or pass --web")
}

/// Evals for each webview, held until its page has sent `domDone`, as
/// `AnkiWebPage._queueAction` does.
#[derive(Default)]
pub struct Evals(Mutex<HashMap<String, (bool, Vec<String>)>>);

impl Evals {
    pub fn reset(&self, label: &str) {
        self.0
            .lock()
            .unwrap()
            .insert(label.to_string(), (false, vec![]));
    }

    pub fn eval(&self, app: &AppHandle, label: &str, js: String) {
        let mut map = self.0.lock().unwrap();
        let entry = map.entry(label.to_string()).or_default();
        if entry.0 {
            run_js(app, label, &js);
        } else {
            entry.1.push(js);
        }
    }

    pub(crate) fn dom_done(&self, app: &AppHandle, label: &str) {
        let mut map = self.0.lock().unwrap();
        let entry = map.entry(label.to_string()).or_default();
        entry.0 = true;
        for js in entry.1.drain(..) {
            run_js(app, label, &js);
        }
    }
}

fn run_js(app: &AppHandle, label: &str, js: &str) {
    match app.get_webview_window(label) {
        Some(w) => {
            if let Err(err) = w.eval(js) {
                tracing::warn!(label, %err, "eval failed");
            }
        }
        None => tracing::warn!(label, "eval on a closed webview"),
    }
}

/// App-level state the bridge handlers need besides [Shell].
pub struct Ui {
    pub shell: Arc<Shell>,
    pub app: AppHandle,
    pub evals: Evals,
    pub editors: Editors,
    pub settings_path: PathBuf,
    pub settings: Mutex<Settings>,
    pub geometry: Option<(f64, f64, f64, f64)>,
    pub background: bool,
    pub status: Mutex<String>,
}

impl Ui {
    /// Open a window showing `html` (served via `legacyPageData`) or a
    /// SvelteKit `route`.
    pub fn open_window(
        &self,
        label: &str,
        title: &str,
        kind: WebviewKind,
        content: Content,
    ) -> Result<()> {
        let url = match &content {
            Content::Html(_) => pages::legacy_page_url(label),
            Content::Route(route) => format!("{}{route}", pages::ORIGIN),
        };
        self.shell.pages.set(
            label,
            kind,
            match content {
                Content::Html(html) => Some(html),
                Content::Route(_) => None,
            },
        );
        self.evals.reset(label);
        if let Some(window) = self.app.get_webview_window(label) {
            window.navigate(url.parse()?)?;
            return Ok(());
        }
        let (x, y, w, h) = self.geometry.unwrap_or((100.0, 100.0, 1100.0, 800.0));
        let mut builder =
            WebviewWindowBuilder::new(&self.app, label, WebviewUrl::CustomProtocol(url.parse()?))
                .title(title)
                .inner_size(w, h)
                .initialization_script(crate::bridge::init_script());
        if self.geometry.is_some() {
            builder = builder.position(x, y);
        }
        if self.background {
            builder = builder.focused(false);
        }
        let window = builder.build()?;
        #[cfg(target_os = "macos")]
        if self.background {
            send_to_back(&window)?;
        }
        Ok(())
    }

    fn settings(&self) -> std::sync::MutexGuard<'_, Settings> {
        self.settings.lock().unwrap()
    }

    fn save_settings(&self) {
        if let Err(err) = self.settings().save(&self.settings_path) {
            tracing::error!(%err, "saving settings failed");
        }
    }

    pub fn reimport(&self) -> Result<()> {
        let mut settings = self.settings.lock().unwrap();
        settings.import_from_anki()?;
        settings.save(&self.settings_path)?;
        tracing::info!(
            profiles = settings.profiles.len(),
            "imported profiles from Anki"
        );
        Ok(())
    }

    /// Open the startup (or given) profile; on failure the home page shows
    /// why, with a retry.
    pub fn open_profile(&self, name: Option<&str>) {
        let (base, name) = {
            let settings = self.settings();
            let name = name
                .map(str::to_string)
                .or_else(|| settings.startup_profile().map(str::to_string));
            (settings.anki_base(), name)
        };
        let status = match name {
            None => "No profiles found. Use Profiles > Re-import from Anki.".to_string(),
            Some(name) => match self.shell.open_profile(ProfilePaths::new(&base, &name)) {
                Ok(()) => {
                    self.settings().last_profile = Some(name.clone());
                    self.save_settings();
                    String::new()
                }
                Err(err @ OpenError::Locked(_)) => format!("Can't open \"{name}\": {err}"),
                Err(err) => format!("Opening \"{name}\" failed: {err}"),
            },
        };
        *self.status.lock().unwrap() = status;
    }

    pub fn show_home(&self) -> Result<()> {
        let html = home_html(self);
        self.open_window(MAIN, "Anki", WebviewKind::Main, Content::Html(html))
    }

    pub fn open_editor(&self, nid: i64) -> Result<()> {
        let label = "editor";
        let js = crate::editor::load_note_js(&self.shell, &self.editors, label, nid)?;
        self.open_window(
            label,
            "Edit",
            WebviewKind::Editor,
            Content::Html(pages::editor_html(&self.shell.web)),
        )?;
        self.evals
            .eval(&self.app, label, "setupEditor('review')".into());
        self.evals.eval(&self.app, label, js);
        Ok(())
    }

    pub fn eval(&self, label: &str, js: String) {
        self.evals.eval(&self.app, label, js);
    }
}

/// Background (test and bench) windows: behind the user's windows, and still
/// rendering. WebKit stops animation frames in windows it thinks are covered,
/// so occlusion detection is turned off with WebKit SPI
/// (`_setWindowOcclusionDetectionEnabled:`, macOS 10.13+), if present.
#[cfg(target_os = "macos")]
fn send_to_back(window: &tauri::WebviewWindow) -> tauri::Result<()> {
    if let Ok(ns_window) = window.ns_window() {
        // SAFETY: an NSWindow
        unsafe { objc::send(ns_window, "orderBack:", std::ptr::null_mut()) };
    }
    window.with_webview(|webview| {
        let sel = "_setWindowOcclusionDetectionEnabled:";
        // SAFETY: inner() is the WKWebView
        unsafe {
            if objc::responds_to(webview.inner(), sel) {
                objc::send_bool(webview.inner(), sel, false);
            } else {
                tracing::warn!("can't disable occlusion detection; covered pages may stall");
            }
        }
    })
}

/// Just enough of the Objective-C runtime for [send_to_back].
#[cfg(target_os = "macos")]
mod objc {
    use std::ffi::c_char;
    use std::ffi::c_void;
    use std::ffi::CString;

    #[link(name = "objc")]
    extern "C" {
        fn sel_registerName(name: *const c_char) -> *mut c_void;
        fn objc_msgSend();
    }

    unsafe fn sel(name: &str) -> *mut c_void {
        let name = CString::new(name).unwrap();
        sel_registerName(name.as_ptr())
    }

    pub unsafe fn responds_to(obj: *mut c_void, name: &str) -> bool {
        let f: unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void) -> bool =
            std::mem::transmute(objc_msgSend as unsafe extern "C" fn());
        f(obj, sel("respondsToSelector:"), sel(name))
    }

    pub unsafe fn send(obj: *mut c_void, name: &str, arg: *mut c_void) {
        let f: unsafe extern "C" fn(*mut c_void, *mut c_void, *mut c_void) =
            std::mem::transmute(objc_msgSend as unsafe extern "C" fn());
        f(obj, sel(name), arg)
    }

    pub unsafe fn send_bool(obj: *mut c_void, name: &str, arg: bool) {
        let f: unsafe extern "C" fn(*mut c_void, *mut c_void, bool) =
            std::mem::transmute(objc_msgSend as unsafe extern "C" fn());
        f(obj, sel(name), arg)
    }
}

pub enum Content {
    Html(String),
    Route(String),
}

fn home_html(ui: &Ui) -> String {
    let status = ui.status.lock().unwrap().clone();
    let settings = ui.settings().clone();
    let body = match ui.shell.open_profile_paths() {
        Some(paths) => {
            let cards: anki_proto::search::SearchResponse = ui
                .shell
                .api
                .call(
                    "search_cards",
                    &anki_proto::search::SearchRequest {
                        search: String::new(),
                        order: None,
                    },
                )
                .unwrap_or_default();
            format!(
                "<h1>{}</h1><p>{} cards. Reviewing, the deck list and the browser \
                 come in later phases; use the old Anki for those.</p>\
                 <p><button onclick=\"pycmd('openDeckOptions')\">Deck options</button> \
                 <input id=nid placeholder=\"note id\" size=14> \
                 <button onclick=\"pycmd('openEditor:' + nid.value)\">Edit note</button></p>",
                htmlescape::encode_minimal(&paths.name),
                cards.ids.len()
            )
        }
        None => format!(
            "<h1>Anki</h1><p id=status>{}</p>\
             <p><button onclick=\"pycmd('retryOpen')\">Try again</button></p>",
            htmlescape::encode_minimal(&status)
        ),
    };
    let profiles: String = settings
        .profiles
        .iter()
        .map(|p| format!("<li>{}</li>", htmlescape::encode_minimal(&p.name)))
        .collect();
    pages::std_html(
        &ui.shell.web,
        &format!(
            "<div style=\"padding: 2em\">{body}<h3>Profiles</h3><ul>{profiles}</ul>\
             <p><button onclick=\"pycmd('reimportProfiles')\">Re-import profiles from \
             Anki</button></p></div>"
        ),
        &[],
        &[],
        true,
    )
}

fn register_bridge(ui: &Arc<Ui>) {
    let bridge = &ui.shell.bridge;
    let u = ui.clone();
    bridge.register("domDone", move |_, label, _| {
        u.evals.dom_done(&u.app, label);
        Value::Null
    });
    let u = ui.clone();
    bridge.register("reimportProfiles", move |_, _, _| {
        if let Err(err) = u.reimport() {
            *u.status.lock().unwrap() = format!("Re-import failed: {err:#}");
        }
        let _ = u.show_home();
        Value::Null
    });
    let u = ui.clone();
    bridge.register("retryOpen", move |_, _, _| {
        u.open_profile(None);
        let _ = u.show_home();
        Value::Null
    });
    let u = ui.clone();
    bridge.register("openEditor", move |_, _, nid| {
        match nid
            .trim()
            .parse()
            .map_err(anyhow::Error::from)
            .and_then(|nid| u.open_editor(nid))
        {
            Ok(()) => json!(true),
            Err(err) => {
                tracing::warn!(%err, "opening editor failed");
                json!(false)
            }
        }
    });
    let u = ui.clone();
    bridge.register("openDeckOptions", move |shell, _, _| {
        let did = current_deck_id(shell).unwrap_or(1);
        let _ = u.open_window(
            "deck-options",
            "Deck Options",
            WebviewKind::DeckOptions,
            Content::Route(format!("deck-options/{did}")),
        );
        Value::Null
    });
    // editor
    for cmd in ["key", "blur"] {
        let u = ui.clone();
        bridge.register(cmd, move |shell, label, args| {
            crate::editor::save_field(shell, &u.editors, label, args)
        });
    }
    let u = ui.clone();
    bridge.register("saveFields", move |shell, label, args| {
        crate::editor::save_fields(shell, &u.editors, label, args)
    });
    for quiet in ["focus", "noteLoaded"] {
        bridge.register(quiet, |_, _, _| Value::Null);
    }
    // deck options
    bridge.register("deckOptionsReady", |_, _, _| Value::Null);
    let u = ui.clone();
    ui.shell
        .register_frontend("deck_options_ready", move |_, _, _| {
            let _ = &u;
            Ok(vec![])
        });
    let u = ui.clone();
    ui.shell
        .register_frontend("deck_options_require_close", move |_, label, _| {
            if let Some(w) = u.app.get_webview_window(label) {
                let _ = w.close();
            }
            Ok(vec![])
        });
}

fn current_deck_id(shell: &Shell) -> Option<i64> {
    let deck: anki_proto::decks::Deck = shell
        .api
        .call("get_current_deck", &anki_proto::generic::Empty {})
        .ok()?;
    Some(deck.id)
}

fn build_menu(app: &AppHandle) -> tauri::Result<tauri::menu::Menu<tauri::Wry>> {
    let app_menu = SubmenuBuilder::new(app, "Anki")
        .item(&PredefinedMenuItem::about(app, None, None)?)
        .separator()
        .item(&PredefinedMenuItem::quit(app, None)?)
        .build()?;
    let edit = SubmenuBuilder::new(app, "Edit")
        .undo()
        .redo()
        .separator()
        .cut()
        .copy()
        .paste()
        .select_all()
        .build()?;
    let profiles = SubmenuBuilder::new(app, "Profiles")
        .item(&MenuItemBuilder::with_id("reimport", "Re-import from Anki").build(app)?)
        .build()?;
    MenuBuilder::new(app)
        .items(&[&app_menu, &edit, &profiles])
        .build()
}

pub fn run(args: Args) -> Result<()> {
    let web = find_web(&args)?;
    let mut settings = Settings::load(&args.settings)?;
    if let Some(base) = &args.anki_base {
        settings.anki_base = Some(base.clone());
    }
    if args.import || settings.profiles.is_empty() {
        if let Err(err) = settings.import_from_anki() {
            tracing::warn!("{err:#}");
        }
        settings.save(&args.settings)?;
    }

    let shell = Arc::new(Shell::new(Api::new(vec!["en".into()])?, web));
    let protocol_shell = shell.clone();
    let mut tauri_app = tauri::Builder::default()
        .register_asynchronous_uri_scheme_protocol("anki", move |ctx, request, responder| {
            let shell = protocol_shell.clone();
            let label = ctx.webview_label().to_string();
            tauri::async_runtime::spawn_blocking(move || {
                responder.respond(crate::protocol::handle(&shell, &label, &request));
            });
        })
        .build(tauri::generate_context!())
        .context("starting Tauri")?;
    #[cfg(target_os = "macos")]
    if args.background {
        // Test and bench windows mustn't take focus or keystrokes from the
        // user. tao activates the app at launch, which Prohibited turns into
        // a no-op; Ready then switches to Accessory so windows can show
        // (WebKit stops animation frames in windows that aren't on screen).
        tauri_app.set_activation_policy(tauri::ActivationPolicy::Prohibited);
    }
    let app = tauri_app.handle().clone();

    let ui = Arc::new(Ui {
        shell: shell.clone(),
        app: app.clone(),
        evals: Evals::default(),
        editors: Editors::default(),
        settings_path: args.settings.clone(),
        settings: Mutex::new(settings),
        geometry: args.geometry,
        background: args.background,
        status: Mutex::new(String::new()),
    });
    register_bridge(&ui);
    app.set_menu(build_menu(&app)?)?;
    let u = ui.clone();
    app.on_menu_event(move |_, event| {
        if event.id() == "reimport" {
            if let Err(err) = u.reimport() {
                *u.status.lock().unwrap() = format!("Re-import failed: {err:#}");
            }
            let _ = u.show_home();
        }
    });

    ui.open_profile(args.profile.as_deref());
    if let Some(port) = args.ankiconnect_port {
        crate::ankiconnect::spawn(shell.clone(), port);
    }
    let backup_shell = shell.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(crate::profile::BACKUP_CHECK_INTERVAL);
        backup_shell.periodic_backup();
    });

    if let Some(test) = &args.test {
        testchan::start(&ui, test.clone())?;
    } else if args.bench {
        crate::bench::start(&ui)?;
    } else {
        ui.show_home()?;
    }

    let exit_shell = shell.clone();
    let background = args.background;
    tauri_app.run(move |app, event| match event {
        #[cfg(target_os = "macos")]
        RunEvent::Ready if background => {
            let _ = app.set_activation_policy(tauri::ActivationPolicy::Accessory);
            for window in app.webview_windows().values() {
                let _ = send_to_back(window);
            }
        }
        RunEvent::WindowEvent {
            label,
            event: tauri::WindowEvent::Destroyed,
            ..
        } => {
            exit_shell.pages.remove(&label);
            if label == MAIN || app.webview_windows().is_empty() {
                app.exit(0);
            }
        }
        RunEvent::Exit => {
            exit_shell.close_profile();
        }
        _ => {}
    });
    Ok(())
}
