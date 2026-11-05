// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The main window: deck browser, overview and reviewer in one webview, as
//! `AnkiQt.moveToState` switches them, plus the dialogs and the sync UI.
//!
//! The deck browser and overview are SvelteKit routes with a small API
//! (`protocol::MAIN_PAGE_BACKEND`). The reviewer is the stock `reviewer.js`
//! page, which shows card templates and so gets only the reviewer
//! endpoints; it frames the toolbar route at the top and the stock bottom
//! bar (`reviewer-bottom.js`) at the bottom. The webview's kind is changed
//! before it navigates, so a page never has more access than it should.

use std::collections::HashMap;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;

use anki::prelude::I18n;
use prost::Message;
use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use crate::app::Content;
use crate::app::Ui;
use crate::app::MAIN;
use crate::pages;
use crate::pages::WebviewKind;
use crate::reviewer::Effect;
use crate::reviewer::Reviewer;
use crate::reviewer::Side;
use crate::settings::SyncSettings;
use crate::sync;

/// The id of the bottom bar's HTML (served by `legacyPageData`).
const BOTTOM_PAGE: &str = "main-bottom";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum MainState {
    /// No profile open; the home page explains why.
    Home,
    DeckBrowser,
    Overview,
    Review,
}

/// What to do once a sync (or a login) is over.
pub type Then = Box<dyn FnOnce(&Arc<Ui>) + Send>;

type DialogCallback = Box<dyn FnOnce(&Arc<Ui>, Option<DialogAnswer>) + Send>;

#[derive(Debug, Clone)]
pub struct DialogAnswer {
    pub button: usize,
    pub fields: HashMap<String, String>,
}

pub struct Dialog {
    pub title: String,
    /// Trusted HTML (escape anything from outside).
    pub message_html: String,
    /// (key, label, is_password)
    pub fields: Vec<(String, String, bool)>,
    pub buttons: Vec<String>,
    pub default: usize,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum SyncIndicator {
    #[default]
    None,
    Normal,
    Full,
}

#[derive(Default)]
pub struct SyncUi {
    /// A collection sync, from start to the end of any full sync (prompts
    /// included).
    pub syncing: bool,
    /// The background media sync; a collection sync can run meanwhile.
    pub media_syncing: bool,
    pub indicator: SyncIndicator,
}

pub struct MainWindow {
    pub state: Mutex<MainState>,
    pub reviewer: Mutex<Reviewer>,
    pub sync: Mutex<SyncUi>,
    /// Open dialogs: label -> (callback, title and message, for tests).
    dialogs: Mutex<HashMap<String, (DialogCallback, String)>>,
    next_dialog: AtomicU64,
    pub tr: I18n,
    /// Authenticates the key script's messages.
    pub key_token: String,
    /// Quit was asked for during a sync: quit once it's over.
    pub exit_after_sync: std::sync::atomic::AtomicBool,
}

impl Default for MainWindow {
    fn default() -> Self {
        MainWindow {
            state: Mutex::new(MainState::Home),
            reviewer: Mutex::new(Reviewer::default()),
            sync: Mutex::new(SyncUi::default()),
            dialogs: Mutex::new(HashMap::new()),
            next_dialog: AtomicU64::new(1),
            tr: I18n::new(&["en"]),
            key_token: data_encoding::BASE64URL_NOPAD.encode(&rand::random::<[u8; 16]>()),
            exit_after_sync: Default::default(),
        }
    }
}

fn state(ui: &Ui) -> MainState {
    *ui.main.state.lock().unwrap()
}

fn js(s: impl AsRef<str>) -> String {
    serde_json::to_string(s.as_ref()).unwrap()
}

// States
//////////////////////////////////////////////////////////////////////////

/// Leave the reviewer (if reviewing) and set the new state.
fn enter(ui: &Arc<Ui>, new: MainState) {
    let old = std::mem::replace(&mut *ui.main.state.lock().unwrap(), new);
    if old == MainState::Review && new != MainState::Review {
        let effects = ui.main.reviewer.lock().unwrap().stop();
        // the page is about to go; only its audio matters
        apply(ui, effects);
    }
    crate::menus::refresh(ui);
}

pub fn show_deck_browser(ui: &Arc<Ui>) {
    if ui.shell.open_profile_paths().is_none() {
        let _ = ui.show_home();
        return;
    }
    enter(ui, MainState::DeckBrowser);
    open_route(ui, "deck-browser");
}

pub fn show_overview(ui: &Arc<Ui>) {
    enter(ui, MainState::Overview);
    open_route(ui, "deck-overview");
}

fn open_route(ui: &Arc<Ui>, route: &str) {
    if let Err(err) = ui.open_window(
        MAIN,
        "Anki",
        WebviewKind::Main,
        Content::Route(route.into()),
    ) {
        tracing::error!(%err, route, "showing page failed");
    }
    set_keys(ui);
    push_sync_state(ui);
}

/// "Study Now": `col.startTimebox()` and the reviewer.
pub fn start_review(ui: &Arc<Ui>) {
    let Some(media) = ui.shell.media_folder() else {
        return;
    };
    enter(ui, MainState::Review);
    let effects = {
        let spacebar_rates_card = ui.settings.lock().unwrap().spacebar_rates_card;
        let mut r = ui.main.reviewer.lock().unwrap();
        r.night_mode = false;
        r.spacebar_rates_card = spacebar_rates_card.unwrap_or(true);
        r.start(&ui.shell.api, media)
    };
    apply(ui, effects);
}

/// The reviewer page without a queue, for `crate::bench`.
pub fn start_bench_review(ui: &Arc<Ui>) {
    let Some(media) = ui.shell.media_folder() else {
        return;
    };
    enter(ui, MainState::Review);
    let effects = ui
        .main
        .reviewer
        .lock()
        .unwrap()
        .start_for_bench(&ui.shell.api, media);
    apply(ui, effects);
}

/// Reload the shown page's data after changes made elsewhere (a sync, an
/// undo from the menu, AnkiConnect).
pub fn refresh(ui: &Arc<Ui>) {
    match state(ui) {
        MainState::DeckBrowser | MainState::Overview => {
            ui.eval(
                MAIN,
                "globalThis.shellRefresh ? shellRefresh() : location.reload();".into(),
            );
        }
        MainState::Review => {
            let effects = ui.main.reviewer.lock().unwrap().redraw(&ui.shell.api);
            apply(ui, effects);
        }
        MainState::Home => {}
    }
}

fn reviewer_page(ui: &Ui) -> String {
    let bottom = pages::std_html(
        &ui.shell.web,
        &format!(
            "<center id=outer>\n<table id=innertable width=100% cellspacing=0 \
             cellpadding=0>\n<tr>\n<td align=start valign=top class=stat>\n<button \
             title=\"{edit_key}\" onclick=\"pycmd('edit');\">{edit}</button></td>\n<td \
             align=center valign=top id=middle>\n</td>\n<td align=end valign=top \
             class=stat>\n<button title=\"{more_key}\" onclick=\"pycmd('more');\">\n{more} \
             &#9662;\n<span id=time class=stattxt></span>\n</button>\n</td>\n</tr>\n</table>\n\
             </center>\n<script>\ntime = 0;\ntimerStopped = false;\n</script>",
            edit = ui.main.tr.studying_edit(),
            edit_key = ui.main.tr.actions_shortcut_key("E"),
            more = ui.main.tr.studying_more(),
            more_key = ui.main.tr.actions_shortcut_key("M"),
        ),
        &["css/toolbar-bottom.css", "css/reviewer-bottom.css"],
        &["js/vendor/jquery.min.js", "js/reviewer-bottom.js"],
        true,
    );
    ui.shell
        .pages
        .set(BOTTOM_PAGE, WebviewKind::Reviewer, Some(bottom));
    let extra = ui
        .shell
        .api
        .call::<anki_proto::generic::Json>(
            "get_config_json",
            &anki_proto::generic::String {
                val: "reviewExtra".into(),
            },
        )
        .ok()
        .and_then(|j| serde_json::from_slice::<String>(&j.json).ok())
        .unwrap_or_default();
    pages::std_html(
        &ui.shell.web,
        &format!(
            "<style>{REVIEWER_CHROME_CSS}</style>\
             <iframe id=_shellToolbar src=\"/toolbar\" title=toolbar></iframe>\
             <div id=_shellTopSpace></div>\
             <div id=\"_mark\" hidden>&#x2605;</div><div id=\"_flag\" hidden>&#x2691;</div>\
             <div id=\"qa\" dir=\"auto\"></div>{extra}\
             <div id=_shellBottomSpace></div>\
             <iframe id=_shellBottom src=\"{}\" title=answer></iframe>\
             <script>{REVIEWER_CHROME_JS}</script>",
            pages::legacy_page_url(BOTTOM_PAGE)
        ),
        &["css/reviewer.css"],
        &["js/reviewer.js"],
        true,
    )
}

const REVIEWER_CHROME_CSS: &str = "\
#_shellToolbar, #_shellBottom { position: fixed; left: 0; width: 100%; border: 0; \
  z-index: 2147483000; display: block; background: var(--canvas, #fff); } \
#_shellToolbar { top: 0; height: 36px; } \
#_shellBottom { bottom: 0; height: 64px; } \
#_shellTopSpace { height: 36px; } \
#_shellBottomSpace { height: 64px; } \
#_flag, #_mark { top: 36px; }";

/// The reviewer page's own code: the bottom bar bridge, frame sizing, and
/// the audio player (`<audio>`, `speechSynthesis` for TTS, mpv through
/// the shell for formats WebKit can't play).
const REVIEWER_CHROME_JS: &str = r#"(() => {
    const $ = (id) => document.getElementById(id);
    const bottom = $("_shellBottom");
    const fit = (frame, spacer, flagsToo) => {
        try {
            const body = frame.contentDocument.body;
            const style = getComputedStyle(body);
            const h = Math.ceil(body.getBoundingClientRect().bottom + parseFloat(style.marginBottom || 0));
            if (h > 0) {
                frame.style.height = h + "px";
                spacer.style.height = h + "px";
                if (flagsToo) {
                    $("_flag").style.top = $("_mark").style.top = h + "px";
                }
            }
        } catch (e) {}
    };
    const watch = (frame, spacer, flagsToo) => frame.addEventListener("load", () => {
        fit(frame, spacer, flagsToo);
        try {
            new ResizeObserver(() => fit(frame, spacer, flagsToo)).observe(frame.contentDocument.body);
        } catch (e) {}
    });
    watch($("_shellToolbar"), $("_shellTopSpace"), true);
    watch(bottom, $("_shellBottomSpace"), false);
    let ready = false;
    const pending = [];
    const call = (fn, args) => {
        try {
            bottom.contentWindow[fn](...args);
        } catch (e) {
            console.error("bottom bar", fn, e);
        }
    };
    bottom.addEventListener("load", () => {
        ready = true;
        pending.splice(0).forEach(([fn, args]) => call(fn, args));
    });
    window._shellBottomCall = (fn, args) => ready ? call(fn, args) : pending.push([fn, args]);
    window._shellBottomSelected = () => {
        try {
            return bottom.contentWindow.selectedAnswerButton();
        } catch (e) {
            return undefined;
        }
    };

    // `log`: the last few playback events, for diagnosing audio
    const av = { queue: [], cur: null, idle: [], log: [] };
    const note = (event) => {
        av.log.push(event);
        if (av.log.length > 20) av.log.shift();
    };
    const url = (f) => "/" + f.split("/").map(encodeURIComponent).join("/");
    av.busy = () => !!av.cur || av.queue.length > 0;
    av.done = () => {
        av.cur = null;
        av.next();
    };
    av.next = () => {
        const tag = av.queue.shift();
        if (!tag) {
            av.idle.splice(0).forEach((cb) => cb());
            return;
        }
        if (tag.file) {
            av.file(tag.file);
        } else if (tag.tts) {
            av.tts(tag.tts);
        } else {
            av.next();
        }
    };
    av.file = (file) => {
        const audio = new Audio(url(file));
        const cur = { mpv: false };
        const fallback = () => {
            if (av.cur !== cur || cur.mpv) return;
            cur.mpv = true;
            note("mpv:" + file);
            pycmd("mpv:" + file);
        };
        cur.stop = () => (cur.mpv ? pycmd("mpvStop") : audio.pause());
        cur.pause = () => (cur.mpv ? pycmd("mpvPause") : audio.paused ? audio.play() : audio.pause());
        cur.seek = (s) => (cur.mpv
            ? pycmd("mpvSeek:" + s)
            : (audio.currentTime = Math.max(0, audio.currentTime + s)));
        av.cur = cur;
        audio.onended = () => {
            note("ended:" + file);
            if (av.cur === cur && !cur.mpv) av.done();
        };
        audio.onerror = fallback;
        audio.play().catch((e) => e.name !== "AbortError" && fallback());
    };
    av.mpvEnded = () => av.cur && av.cur.mpv && av.done();
    av.tts = (t) => {
        if (!window.speechSynthesis) return av.next();
        const u = new SpeechSynthesisUtterance(t.text);
        const lang = (t.lang || "").replace("_", "-");
        const voices = speechSynthesis.getVoices();
        u.voice = voices.find((v) => (t.voices || []).includes(v.name))
            || voices.find((v) => v.lang === lang)
            || voices.find((v) => lang && v.lang.startsWith(lang.split("-")[0]))
            || null;
        if (lang) u.lang = lang;
        u.rate = t.speed || 1;
        const cur = {
            stop: () => speechSynthesis.cancel(),
            pause: () => (speechSynthesis.paused ? speechSynthesis.resume() : speechSynthesis.pause()),
            seek: () => {},
        };
        av.cur = cur;
        u.onend = u.onerror = (e) => {
            note("tts-" + e.type + ":" + t.text);
            if (av.cur === cur) av.done();
        };
        speechSynthesis.speak(u);
    };
    // AVPlayer.play_tags
    av.play = (tags, interrupt) => {
        av.queue = tags.slice();
        if (interrupt && av.cur) {
            const c = av.cur;
            av.cur = null;
            c.stop();
        }
        if (!av.cur) av.next();
    };
    av.stop = () => {
        av.queue = [];
        const c = av.cur;
        av.cur = null;
        if (c) c.stop();
    };
    av.togglePause = () => av.cur && av.cur.pause();
    av.seek = (secs) => av.cur && av.cur.seek(secs);
    av.whenIdle = (cb) => (av.busy() ? av.idle.push(cb) : cb());
    window._shellAv = av;
})();"#;

// Effects
//////////////////////////////////////////////////////////////////////////

pub fn apply(ui: &Arc<Ui>, effects: Vec<Effect>) {
    for effect in effects {
        match effect {
            Effect::Js(code) => ui.eval(MAIN, code),
            Effect::LoadPage => {
                let html = reviewer_page(ui);
                if let Err(err) =
                    ui.open_window(MAIN, "Anki", WebviewKind::Reviewer, Content::Html(html))
                {
                    tracing::error!(%err, "loading the reviewer failed");
                }
                set_keys(ui);
            }
            Effect::Overview => show_overview(ui),
            Effect::DeckBrowser => show_deck_browser(ui),
            Effect::Tooltip(msg) => tooltip(ui, &msg),
            Effect::EditNote(nid) => {
                if let Err(err) = ui.open_editor(nid) {
                    tooltip(ui, &format!("{err:#}"));
                }
            }
            Effect::CardInfo(cid) => {
                let _ = ui.open_window(
                    "card-info",
                    &ui.main.tr.actions_card_info(),
                    WebviewKind::CardInfo,
                    Content::Route(format!("card-info/{cid}")),
                );
            }
            Effect::DeckOptions(did) => open_deck_options(ui, did),
            Effect::ContextMenu => crate::menus::popup_more(ui),
            Effect::Timer { seq, after } => {
                let ui = ui.clone();
                std::thread::spawn(move || {
                    std::thread::sleep(after);
                    // as Qt, auto advance stops when the window loses focus
                    let focused = ui.background
                        || ui
                            .app
                            .get_webview_window(MAIN)
                            .and_then(|w| w.is_focused().ok())
                            .unwrap_or(false);
                    let effects = {
                        let mut r = ui.main.reviewer.lock().unwrap();
                        if !focused {
                            r.disable_auto_advance();
                            vec![]
                        } else {
                            r.auto_advance_due(seq)
                        }
                    };
                    apply(&ui, effects);
                });
            }
            Effect::Timebox { cards, minutes } => {
                let tr = &ui.main.tr;
                let message = tr
                    .studying_card_studied_in_minute(cards, minutes.to_string())
                    .into_owned();
                ask(
                    ui,
                    Dialog {
                        title: "Anki".into(),
                        message_html: htmlescape::encode_minimal(&message),
                        fields: vec![],
                        buttons: vec![
                            tr.studying_continue().into_owned(),
                            tr.studying_finish().into_owned(),
                        ],
                        default: 0,
                    },
                    |ui, answer| {
                        // the user may have left the reviewer meanwhile
                        if state(ui) != MainState::Review {
                            return;
                        }
                        let keep_going = answer.is_some_and(|a| a.button == 0);
                        let effects = ui
                            .main
                            .reviewer
                            .lock()
                            .unwrap()
                            .timebox_answered(&ui.shell.api, keep_going);
                        apply(ui, effects);
                    },
                );
            }
            Effect::UndoChanged => crate::menus::refresh(ui),
        }
    }
}

pub fn tooltip(ui: &Ui, msg: &str) {
    ui.eval(
        MAIN,
        format!("window._shellTooltip && _shellTooltip({});", js(msg)),
    );
}

pub fn open_deck_options(ui: &Ui, did: i64) {
    let _ = ui.open_window(
        "deck-options",
        "Deck Options",
        WebviewKind::DeckOptions,
        Content::Route(format!("deck-options/{did}")),
    );
}

pub fn open_stats(ui: &Ui) {
    let _ = ui.open_window(
        "stats",
        &ui.main.tr.qt_misc_stats(),
        WebviewKind::DeckStats,
        Content::Route("graphs".into()),
    );
}

/// Installed in the main window's top frame: a tooltip that doesn't take
/// the card's styles (`aqt.utils.tooltip`).
pub const TOOLTIP_SCRIPT: &str = r#"window._shellTooltip = (msg) => {
    let host = document.getElementById("_shellTip");
    if (!host) {
        host = document.createElement("div");
        host.id = "_shellTip";
        host.style.cssText = "position:fixed;left:0;right:0;bottom:90px;display:flex;"
            + "justify-content:center;pointer-events:none;z-index:2147483647";
        host.attachShadow({ mode: "open" }).innerHTML = "<style>div{font:13px -apple-system,"
            + "sans-serif;background:#feffd6;color:#000;border:1px solid #999;border-radius:5px;"
            + "padding:6px 12px;max-width:60vw;box-shadow:0 2px 8px #0003}</style><div></div>";
        document.documentElement.appendChild(host);
    }
    host.shadowRoot.querySelector("div").textContent = msg;
    host.style.display = "flex";
    clearTimeout(host._timer);
    host._timer = setTimeout(() => (host.style.display = "none"), 3000);
};"#;

// Shortcuts
//////////////////////////////////////////////////////////////////////////

const GLOBAL_ACTIONS: &[&str] = &["decks", "study", "stats", "sync"];
const OVERVIEW_ACTIONS: &[&str] = &["showAnswer", "options"];

fn shortcuts(ui: &Ui) -> crate::shortcuts::Shortcuts {
    let settings = ui.settings.lock().unwrap();
    crate::shortcuts::Shortcuts::new(&settings.answer_keys, &settings.shortcuts)
}

/// Tell the page which keys to send (`shortcuts::key_script`).
fn set_keys(ui: &Ui) {
    let s = shortcuts(ui);
    let mut actions: Vec<&str> = GLOBAL_ACTIONS.to_vec();
    match state(ui) {
        MainState::Review => {
            actions = crate::shortcuts::DEFAULTS.iter().map(|(a, _)| *a).collect();
        }
        MainState::Overview => actions.extend(OVERVIEW_ACTIONS),
        _ => {}
    }
    ui.eval(
        MAIN,
        format!(
            "window.__shellKeys = {};",
            serde_json::to_string(&s.bound(&actions)).unwrap()
        ),
    );
}

pub fn shortcut(ui: &Arc<Ui>, combo: &str) {
    let s = shortcuts(ui);
    let Some(action) = s.action(combo) else {
        return;
    };
    action_for_state(ui, action);
}

/// A shortcut or menu action, as the current state handles it.
pub fn action_for_state(ui: &Arc<Ui>, action: &str) {
    let api = &ui.shell.api;
    match (state(ui), action) {
        (MainState::Home, _) => {}
        (_, "decks") => show_deck_browser(ui),
        (_, "study") => show_overview(ui),
        (_, "stats") => open_stats(ui),
        (_, "sync") => sync_now(ui, None),
        (MainState::Overview, "showAnswer") => start_review(ui),
        (MainState::Overview | MainState::DeckBrowser, "options") => {
            open_deck_options(ui, current_deck_id(ui))
        }
        (MainState::DeckBrowser | MainState::Overview, "undo" | "redo") => {
            let redo = action == "redo";
            let out = api.call::<anki_proto::collection::OpChangesAfterUndo>(
                if redo { "redo" } else { "undo" },
                &anki_proto::generic::Empty {},
            );
            match out {
                Ok(out) => tooltip(
                    ui,
                    &if redo {
                        ui.main.tr.undo_redo_action(out.operation).into_owned()
                    } else {
                        ui.main.tr.undo_action_undone(out.operation).into_owned()
                    },
                ),
                Err(err) => tooltip(ui, &err.message),
            }
            crate::menus::refresh(ui);
            refresh(ui);
        }
        (MainState::Review, action) => {
            let effects = {
                let mut r = ui.main.reviewer.lock().unwrap();
                match action {
                    "answer1" | "answer2" | "answer3" | "answer4" => {
                        r.answer(api, action.as_bytes()[6] - b'0')
                    }
                    "showAnswer" => r.enter_key(),
                    "edit" => r.edit(),
                    "more" => vec![Effect::ContextMenu],
                    "replay" => r.replay(),
                    "mark" => r.toggle_mark(api),
                    "buryNote" => r.bury_or_suspend(api, true, false),
                    "buryCard" => r.bury_or_suspend(api, false, false),
                    "suspendNote" => r.bury_or_suspend(api, true, true),
                    "suspendCard" => r.bury_or_suspend(api, false, true),
                    "deleteNote" => r.delete_note(api),
                    "options" => r.options(),
                    "cardInfo" => r.card_info(false),
                    "previousCardInfo" => r.card_info(true),
                    "undo" => r.undo(api, false),
                    "redo" => r.undo(api, true),
                    "pauseAudio" => vec![Effect::Js("_shellAv.togglePause();".into())],
                    "seekBackward" => vec![Effect::Js("_shellAv.seek(-5);".into())],
                    "seekForward" => vec![Effect::Js("_shellAv.seek(5);".into())],
                    "autoAdvance" => r.toggle_auto_advance(),
                    flag if flag.starts_with("flag") => {
                        r.set_flag(api, flag[4..].parse().unwrap_or(0))
                    }
                    _ => vec![],
                }
            };
            apply(ui, effects);
        }
        _ => {}
    }
}

fn current_deck_id(ui: &Ui) -> i64 {
    ui.shell
        .api
        .call::<anki_proto::decks::Deck>("get_current_deck", &anki_proto::generic::Empty {})
        .map(|d| d.id)
        .unwrap_or(1)
}

// Dialogs
//////////////////////////////////////////////////////////////////////////

/// A small window with a message, optional fields and buttons. `done`
/// gets None if the window was closed.
pub fn ask(
    ui: &Arc<Ui>,
    dialog: Dialog,
    done: impl FnOnce(&Arc<Ui>, Option<DialogAnswer>) + Send + 'static,
) {
    let id = ui.main.next_dialog.fetch_add(1, Ordering::SeqCst);
    let label = format!("dialog-{id}");
    ui.main.dialogs.lock().unwrap().insert(
        label.clone(),
        (
            Box::new(done),
            format!("{}: {}", dialog.title, dialog.message_html),
        ),
    );
    let fields: String = dialog
        .fields
        .iter()
        .map(|(key, label, password)| {
            format!(
                "<label>{}<input name=\"{}\" type=\"{}\" autocomplete=off></label>",
                htmlescape::encode_minimal(label),
                htmlescape::encode_attribute(key),
                if *password { "password" } else { "text" }
            )
        })
        .collect();
    let buttons: String = dialog
        .buttons
        .iter()
        .enumerate()
        .map(|(i, b)| {
            format!(
                "<button type=button data-i={i}{}>{}</button>",
                if i == dialog.default {
                    " autofocus"
                } else {
                    ""
                },
                htmlescape::encode_minimal(b)
            )
        })
        .collect();
    let html = pages::std_html(
        &ui.shell.web,
        &format!(
            "<style>body {{ margin: 0; padding: 18px 22px; font-size: 13px; }} \
             .msg {{ margin-bottom: 14px; line-height: 1.4; }} \
             label {{ display: block; margin: 6px 0; }} \
             input {{ display: block; width: 100%; box-sizing: border-box; margin-top: 3px; }} \
             .buttons {{ text-align: right; margin-top: 16px; }} \
             button {{ margin-left: 6px; }}</style>\
             <form><div class=msg>{}</div>{fields}<div class=buttons>{buttons}</div></form>\
             <script>{{\
               const send = (i) => {{ const fields = {{}}; \
                 document.querySelectorAll('input').forEach((e) => fields[e.name] = e.value); \
                 pycmd('dialogAnswer:' + JSON.stringify({{ button: i, fields }})); }}; \
               document.querySelectorAll('button').forEach((b) => \
                 b.addEventListener('click', () => send(Number(b.dataset.i)))); \
               document.querySelector('form').addEventListener('submit', (e) => {{ \
                 e.preventDefault(); send({default}); }}); \
               document.addEventListener('keydown', (e) => {{ \
                 if (e.key === 'Enter' && e.target.tagName === 'INPUT') {{ \
                   e.preventDefault(); send({default}); }} }}); \
               (document.querySelector('input') || document.querySelector('[autofocus]'))?.focus(); \
             }}</script>",
            dialog.message_html,
            default = dialog.default,
        ),
        &[],
        &[],
        true,
    );
    let height = 150.0
        + 52.0 * dialog.fields.len() as f64
        + dialog.message_html.len().min(1200) as f64 / 6.0;
    if let Err(err) = ui.open_dialog_window(&label, &dialog.title, html, height) {
        tracing::error!(%err, "opening dialog failed");
        dialog_closed(ui, &label);
    }
}

/// The dialog page's `dialogAnswer`.
pub fn dialog_answered(ui: &Arc<Ui>, label: &str, args: &str) {
    let Ok(v) = serde_json::from_str::<Value>(args) else {
        return;
    };
    let answer = DialogAnswer {
        button: v["button"].as_u64().unwrap_or(0) as usize,
        fields: serde_json::from_value(v["fields"].clone()).unwrap_or_default(),
    };
    let done = ui.main.dialogs.lock().unwrap().remove(label).map(|d| d.0);
    if let Some(window) = ui.app.get_webview_window(label) {
        let _ = window.destroy();
    }
    if let Some(done) = done {
        done(ui, Some(answer));
    }
}

/// A dialog window went away without an answer.
pub fn dialog_closed(ui: &Arc<Ui>, label: &str) {
    let done = ui.main.dialogs.lock().unwrap().remove(label).map(|d| d.0);
    if let Some(done) = done {
        done(ui, None);
    }
}

/// The open dialogs, oldest first: (label, "title: message").
pub fn open_dialogs(ui: &Ui) -> Vec<(String, String)> {
    let mut out: Vec<(String, String)> = ui
        .main
        .dialogs
        .lock()
        .unwrap()
        .iter()
        .map(|(label, (_, text))| (label.clone(), text.clone()))
        .collect();
    out.sort_by_key(|(label, _)| {
        label
            .trim_start_matches("dialog-")
            .parse::<u64>()
            .unwrap_or(0)
    });
    out
}

pub fn message(ui: &Arc<Ui>, text: &str) {
    ask(
        ui,
        Dialog {
            title: "Anki".into(),
            message_html: htmlescape::encode_minimal(text).replace('\n', "<br>"),
            fields: vec![],
            buttons: vec!["OK".into()],
            default: 0,
        },
        |_, _| {},
    );
}

// Sync
//////////////////////////////////////////////////////////////////////////

fn with_sync_settings<T>(ui: &Ui, f: impl FnOnce(&mut SyncSettings) -> T) -> Option<T> {
    let name = ui.shell.open_profile_paths()?.name;
    let out = {
        let mut settings = ui.settings.lock().unwrap();
        let profile = settings
            .profiles
            .iter_mut()
            .find(|p| p.name.eq_ignore_ascii_case(&name))?;
        f(&mut profile.sync)
    };
    ui.save_settings_now();
    Some(out)
}

fn sync_settings(ui: &Ui) -> Option<SyncSettings> {
    let name = ui.shell.open_profile_paths()?.name;
    ui.settings
        .lock()
        .unwrap()
        .profile(&name)
        .map(|p| p.sync.clone())
}

fn anki_base(ui: &Ui) -> std::path::PathBuf {
    ui.settings.lock().unwrap().anki_base()
}

pub fn sync_state_json(ui: &Ui) -> Value {
    let logged_in = sync_settings(ui).is_some_and(|s| sync::auth(&s).is_some());
    let s = ui.main.sync.lock().unwrap();
    let state = if s.syncing || s.media_syncing {
        "syncing"
    } else {
        match s.indicator {
            SyncIndicator::None => "none",
            SyncIndicator::Normal => "normal",
            SyncIndicator::Full => "full",
        }
    };
    json!({ "state": state, "loggedIn": logged_in })
}

fn push_sync_state(ui: &Ui) {
    let state = sync_state_json(ui);
    ui.eval(
        MAIN,
        format!(
            "(() => {{ const s = {state}; window.shellSetSyncState?.(s); \
             document.getElementById('_shellToolbar')?.contentWindow?.shellSetSyncState?.(s); \
             }})();"
        ),
    );
}

/// A collection sync is running (or waiting on its full-sync prompt).
pub fn is_syncing(ui: &Ui) -> bool {
    ui.main.sync.lock().unwrap().syncing
}

/// The app is quitting (the close sync, or a quit during a sync).
fn exiting(ui: &Ui) -> bool {
    ui.close_synced.load(Ordering::SeqCst) || ui.main.exit_after_sync.load(Ordering::SeqCst)
}

fn set_syncing(ui: &Ui, syncing: bool) {
    ui.main.sync.lock().unwrap().syncing = syncing;
    push_sync_state(ui);
}

/// The toolbar's sync indicator, from `sync_status`. Errors are ignored,
/// as in `sync.py`.
pub fn check_sync_status(ui: &Arc<Ui>) {
    let Some(settings) = sync_settings(ui) else {
        return;
    };
    let Some(auth) = sync::auth(&settings) else {
        return;
    };
    let ui = ui.clone();
    std::thread::spawn(
        move || match sync::status(&ui.shell.api, &anki_base(&ui), &auth) {
            Ok(status) => {
                with_sync_settings(&ui, |s| sync::apply_status(s, &status));
                ui.main.sync.lock().unwrap().indicator = match status.required {
                    sync::StatusRequired::NoChanges => SyncIndicator::None,
                    sync::StatusRequired::NormalSync => SyncIndicator::Normal,
                    sync::StatusRequired::FullSync => SyncIndicator::Full,
                };
                push_sync_state(&ui);
            }
            Err(err) => tracing::info!("sync status: {err:#}"),
        },
    );
}

/// Log in first if needed, then sync. `done` runs when the sync (not the
/// media sync) is over, whatever happened.
pub fn sync_now(ui: &Arc<Ui>, done: Option<Then>) {
    if ui.main.sync.lock().unwrap().syncing {
        if let Some(done) = done {
            done(ui);
        }
        return;
    }
    let Some(settings) = sync_settings(ui) else {
        if let Some(done) = done {
            done(ui);
        }
        return;
    };
    match sync::auth(&settings) {
        Some(auth) => {
            set_syncing(ui, true);
            let ui = ui.clone();
            std::thread::spawn(move || run_sync(&ui, auth, done));
        }
        None => login(ui, done),
    }
}

fn login(ui: &Arc<Ui>, done: Option<Then>) {
    let tr = &ui.main.tr;
    ask(
        ui,
        Dialog {
            title: "AnkiWeb".into(),
            message_html: tr
                .sync_account_required("https://ankiweb.net/account/signup")
                .into_owned(),
            fields: vec![
                (
                    "user".into(),
                    tr.sync_ankiweb_id_label().into_owned(),
                    false,
                ),
                ("pass".into(), tr.sync_password_label().into_owned(), true),
            ],
            buttons: vec![
                tr.sync_log_in_button().into_owned(),
                tr.actions_cancel().into_owned(),
            ],
            default: 0,
        },
        move |ui, answer| {
            let Some(answer) = answer.filter(|a| a.button == 0) else {
                if let Some(done) = done {
                    done(ui);
                }
                return;
            };
            let user = answer.fields.get("user").cloned().unwrap_or_default();
            let pass = answer.fields.get("pass").cloned().unwrap_or_default();
            let ui = ui.clone();
            std::thread::spawn(move || {
                let endpoint = sync_settings(&ui).and_then(|s| sync::endpoint(&s));
                match sync::login(
                    &ui.shell.api,
                    &anki_base(&ui),
                    user.trim(),
                    &pass,
                    endpoint.as_deref(),
                ) {
                    Ok(auth) => {
                        with_sync_settings(&ui, |s| sync::store_login(s, user.trim(), &auth));
                        sync_now(&ui, done);
                    }
                    Err(err) => {
                        message(&ui, &format!("{err:#}"));
                        if let Some(done) = done {
                            done(&ui);
                        }
                    }
                }
            });
        },
    );
}

pub fn logout(ui: &Arc<Ui>) {
    with_sync_settings(ui, sync::clear_auth);
    ui.main.sync.lock().unwrap().indicator = SyncIndicator::None;
    push_sync_state(ui);
}

fn sync_error(ui: &Arc<Ui>, err: &anyhow::Error) {
    if let Some(api_err) = sync::api_error(err) {
        if sync::is_interrupted(api_err) {
            return;
        }
        if sync::is_auth_error(api_err) {
            with_sync_settings(ui, sync::clear_auth);
        }
    }
    message(ui, &format!("{err:#}"));
}

fn run_sync(ui: &Arc<Ui>, auth: sync::SyncAuth, done: Option<Then>) {
    let finish = move |ui: &Arc<Ui>| {
        set_syncing(ui, false);
        match done {
            // the close sync exits by itself, once media is done
            Some(done) => done(ui),
            None if ui.main.exit_after_sync.load(Ordering::SeqCst) => ui.app.exit(0),
            None => {}
        }
    };
    let base = anki_base(ui);
    let media = sync_settings(ui).map_or(true, |s| sync::media_enabled(&s));
    match sync::sync_collection(&ui.shell.api, &base, &auth, media) {
        Err(err) => {
            tracing::warn!("sync failed: {err:#}");
            sync_error(ui, &err);
            finish(ui);
        }
        Ok(outcome) => {
            with_sync_settings(ui, |s| sync::apply_outcome(s, &outcome));
            if let Some(msg) = &outcome.server_message {
                message(ui, msg);
            }
            match outcome.required {
                sync::Required::Done => {
                    tracing::info!("sync done");
                    tooltip(ui, &ui.main.tr.sync_collection_complete());
                    ui.main.sync.lock().unwrap().indicator = SyncIndicator::None;
                    after_sync(ui);
                    if media {
                        watch_media_sync(ui);
                    }
                    finish(ui);
                }
                sync::Required::FullSync(choice) => {
                    let usn = outcome.full_sync_media_usn(media);
                    full_sync_prompt(ui, auth, choice, usn, Box::new(finish));
                }
            }
        }
    }
}

/// The full-sync choices of `sync.py`: both, or a confirmation when one
/// side is empty.
fn full_sync_prompt(
    ui: &Arc<Ui>,
    auth: sync::SyncAuth,
    choice: sync::FullSyncChoice,
    usn: Option<i32>,
    finish: Then,
) {
    let tr = &ui.main.tr;
    let (message_html, buttons, uploads): (String, Vec<String>, Vec<Option<bool>>) = match choice {
        sync::FullSyncChoice::UploadOrDownload => (
            render_markdown(ui, &tr.sync_conflict_explanation2()),
            vec![
                tr.sync_upload_to_ankiweb().into_owned(),
                tr.sync_download_from_ankiweb().into_owned(),
                tr.sync_cancel_button().into_owned(),
            ],
            vec![Some(true), Some(false), None],
        ),
        sync::FullSyncChoice::DownloadOnly => (
            htmlescape::encode_minimal(&tr.sync_confirm_empty_download()),
            vec!["OK".into(), tr.actions_cancel().into_owned()],
            vec![Some(false), None],
        ),
        sync::FullSyncChoice::UploadOnly => (
            htmlescape::encode_minimal(&tr.sync_confirm_empty_upload()),
            vec!["OK".into(), tr.actions_cancel().into_owned()],
            vec![Some(true), None],
        ),
    };
    let default = buttons.len() - 1;
    ask(
        ui,
        Dialog {
            title: tr.qt_misc_sync().into_owned(),
            message_html,
            fields: vec![],
            buttons,
            default,
        },
        move |ui, answer| {
            let upload = answer.and_then(|a| uploads.get(a.button).copied().flatten());
            let Some(upload) = upload else {
                finish(ui);
                return;
            };
            // As `closeAllWindows` before a full sync: the editor's pending
            // save, or deck options, would write into the replaced
            // collection. When quitting, the editor has been saved already.
            let open: Vec<&str> = ["editor", "deck-options"]
                .into_iter()
                .filter(|l| ui.app.get_webview_window(l).is_some())
                .collect();
            if !open.is_empty() {
                if exiting(ui) {
                    for label in &open {
                        if let Some(w) = ui.app.get_webview_window(label) {
                            let _ = w.destroy();
                        }
                    }
                } else {
                    message(
                        ui,
                        "Close the editor and the deck options, then sync again for the full sync.",
                    );
                    finish(ui);
                    return;
                }
            }
            let ui = ui.clone();
            std::thread::spawn(move || {
                tooltip(
                    &ui,
                    &if upload {
                        ui.main.tr.sync_uploading_to_ankiweb()
                    } else {
                        ui.main.tr.sync_downloading_from_ankiweb()
                    },
                );
                // the reviewer's card and the pages' data are about to go
                let leaving_review = state(&ui) == MainState::Review;
                let Some(paths) = ui.shell.open_profile_paths() else {
                    finish(&ui);
                    return;
                };
                let result =
                    sync::full_sync(&ui.shell.api, &anki_base(&ui), &paths, &auth, upload, usn);
                tracing::info!(upload, ok = result.is_ok(), "full sync done");
                match result {
                    Ok(()) => {
                        ui.main.sync.lock().unwrap().indicator = SyncIndicator::None;
                        if usn.is_some() {
                            watch_media_sync(&ui);
                        }
                    }
                    Err(err) => sync_error(&ui, &err),
                }
                if exiting(&ui) {
                    // no window to update
                } else if leaving_review || !upload {
                    show_deck_browser(&ui);
                } else {
                    refresh(&ui);
                }
                finish(&ui);
            });
        },
    );
}

fn render_markdown(ui: &Ui, text: &str) -> String {
    ui.shell
        .api
        .call::<anki_proto::generic::String>(
            "render_markdown",
            &anki_proto::card_rendering::RenderMarkdownRequest {
                markdown: text.into(),
                sanitize: true,
            },
        )
        .map(|s| s.val)
        .unwrap_or_else(|_| htmlescape::encode_minimal(text))
}

/// After a normal sync: the pages show what changed.
fn after_sync(ui: &Arc<Ui>) {
    match state(ui) {
        MainState::Review => {
            // the card may have been answered or changed elsewhere
            let effects = {
                let mut r = ui.main.reviewer.lock().unwrap();
                if r.side() == Some(Side::Transition) {
                    vec![]
                } else {
                    r.next_card(&ui.shell.api)
                }
            };
            apply(ui, effects);
        }
        _ => refresh(ui),
    }
    crate::menus::refresh(ui);
}

/// Keep the toolbar spinning until the background media sync is done.
fn watch_media_sync(ui: &Arc<Ui>) {
    let ui = ui.clone();
    std::thread::spawn(move || {
        ui.main.sync.lock().unwrap().media_syncing = true;
        push_sync_state(&ui);
        loop {
            std::thread::sleep(Duration::from_millis(500));
            match sync::media_status(&ui.shell.api) {
                Ok(status) if status.active => continue,
                Ok(_) => break,
                Err(err) => {
                    if !sync::is_interrupted(&err) {
                        tooltip(&ui, &format!("{}: {}", ui.main.tr.sync_media_failed(), err));
                    }
                    break;
                }
            }
        }
        ui.main.sync.lock().unwrap().media_syncing = false;
        push_sync_state(&ui);
        check_sync_status(&ui);
    });
}

/// Sync on open/close, if enabled and logged in
/// (`maybe_auto_sync_on_open_close`).
pub fn wants_auto_sync(ui: &Ui) -> bool {
    (!ui.background || ui.auto_sync_in_background)
        && sync_settings(ui)
            .is_some_and(|s| sync::auto_sync_enabled(&s) && sync::auth(&s).is_some())
}

/// Wait for a running media sync, at most `max` (on close).
pub fn wait_for_media_sync(ui: &Ui, max: Duration) {
    let start = std::time::Instant::now();
    while start.elapsed() < max {
        match sync::media_status(&ui.shell.api) {
            Ok(status) if status.active => std::thread::sleep(Duration::from_millis(250)),
            _ => break,
        }
    }
}

// Bridge
//////////////////////////////////////////////////////////////////////////

// `frontend.proto`'s messages, which anki_proto doesn't export
#[derive(Clone, PartialEq, prost::Message)]
struct SchedulingStatesWithContext {
    #[prost(message, optional, tag = "1")]
    states: Option<anki_proto::scheduler::SchedulingStates>,
    #[prost(message, optional, tag = "2")]
    context: Option<anki_proto::scheduler::SchedulingContext>,
}

#[derive(Clone, PartialEq, prost::Message)]
struct SetSchedulingStatesRequest {
    #[prost(string, tag = "1")]
    key: String,
    #[prost(message, optional, tag = "2")]
    states: Option<anki_proto::scheduler::SchedulingStates>,
}

/// Reviewer commands, which only count while reviewing (as Qt's bridge
/// handler is set per state).
fn reviewer_cmd(ui: &Arc<Ui>, f: impl FnOnce(&mut Reviewer, &crate::api::Api) -> Vec<Effect>) {
    if state(ui) != MainState::Review {
        return;
    }
    let effects = f(&mut ui.main.reviewer.lock().unwrap(), &ui.shell.api);
    apply(ui, effects);
}

pub fn register(ui: &Arc<Ui>) {
    let bridge = &ui.shell.bridge;
    let reg = |name: &str, f: fn(&Arc<Ui>, &str, &str) -> Value| {
        let u = ui.clone();
        bridge.register(name, move |_, label, args| f(&u, label, args));
    };
    // pages (main window only)
    reg("decks", |ui, label, _| {
        if label == MAIN {
            show_deck_browser(ui);
        }
        Value::Null
    });
    reg("open", |ui, label, did| {
        if label == MAIN && matches!(state(ui), MainState::DeckBrowser | MainState::Overview) {
            if let Ok(did) = did.parse::<i64>() {
                let _ = ui.shell.api.call::<anki_proto::collection::OpChanges>(
                    "set_current_deck",
                    &anki_proto::decks::DeckId { did },
                );
                show_overview(ui);
            }
        }
        Value::Null
    });
    reg("opts", |ui, label, did| {
        if label == MAIN && matches!(state(ui), MainState::DeckBrowser | MainState::Overview) {
            let did = did.parse().unwrap_or_else(|_| current_deck_id(ui));
            open_deck_options(ui, did);
        }
        Value::Null
    });
    reg("study", |ui, label, _| {
        if label == MAIN && state(ui) == MainState::Overview {
            start_review(ui);
        }
        Value::Null
    });
    reg("stats", |ui, label, _| {
        if label == MAIN {
            open_stats(ui);
        }
        Value::Null
    });
    reg("sync", |ui, label, _| {
        if label == MAIN {
            sync_now(ui, None);
        }
        Value::Null
    });
    reg("syncState", |ui, _, _| sync_state_json(ui));
    reg("shortcut", |ui, label, args| {
        // only from the key script (see `shortcuts::key_script`)
        match args.split_once(':') {
            Some((token, combo)) if label == MAIN && token == ui.main.key_token => {
                shortcut(ui, combo)
            }
            _ => tracing::warn!(label, "rejected a shortcut without the key token"),
        }
        Value::Null
    });
    reg("dialogAnswer", |ui, label, args| {
        if label.starts_with("dialog-") {
            dialog_answered(ui, label, args);
        }
        Value::Null
    });
    // the reviewer and its bottom bar
    reg("ans", |ui, _, _| {
        reviewer_cmd(ui, |r, _| r.request_answer());
        Value::Null
    });
    reg("typedAnswer", |ui, _, typed| {
        let typed: String = serde_json::from_str(typed).unwrap_or_default();
        reviewer_cmd(ui, |r, api| r.show_answer(api, &typed));
        Value::Null
    });
    for ease in 1..=4u8 {
        let u = ui.clone();
        bridge.register(&format!("ease{ease}"), move |_, _, _| {
            reviewer_cmd(&u, |r, api| r.answer(api, ease));
            Value::Null
        });
    }
    reg("edit", |ui, _, _| {
        reviewer_cmd(ui, |r, _| r.edit());
        Value::Null
    });
    reg("more", |ui, _, _| {
        reviewer_cmd(ui, |_, _| vec![Effect::ContextMenu]);
        Value::Null
    });
    reg("play", |ui, _, args| {
        reviewer_cmd(ui, |r, _| r.play_clicked(args));
        Value::Null
    });
    reg("statesMutated", |ui, _, _| {
        reviewer_cmd(ui, |r, api| r.states_mutated(api));
        Value::Null
    });
    reg("autoAdvanceNow", |ui, _, seq| {
        let seq = seq.parse().unwrap_or(0);
        reviewer_cmd(ui, |r, api| r.auto_advance_now(api, seq));
        Value::Null
    });
    reg("updateToolbar", |_, _, _| Value::Null);
    reg("mpv", |ui, label, file| {
        if label == MAIN {
            crate::audio::play_with_mpv(ui, file);
        }
        Value::Null
    });
    reg("mpvStop", |_, _, _| {
        crate::audio::stop_mpv();
        Value::Null
    });
    reg("mpvPause", |_, _, _| {
        crate::audio::mpv_command(&["cycle", "pause"]);
        Value::Null
    });
    reg("mpvSeek", |_, _, secs| {
        crate::audio::mpv_command(&["seek", secs, "relative"]);
        Value::Null
    });

    // the custom scheduling endpoints (reviewer page)
    let u = ui.clone();
    ui.shell
        .register_frontend("get_scheduling_states_with_context", move |_, label, _| {
            if label != MAIN || state(&u) != MainState::Review {
                return Err("not reviewing".into());
            }
            let (states, context) = u
                .main
                .reviewer
                .lock()
                .unwrap()
                .scheduling_states()
                .ok_or("no card")?;
            Ok(SchedulingStatesWithContext {
                states: Some(states),
                context: Some(context),
            }
            .encode_to_vec())
        });
    let u = ui.clone();
    ui.shell
        .register_frontend("set_scheduling_states", move |_, label, input| {
            if label != MAIN || state(&u) != MainState::Review {
                return Err("not reviewing".into());
            }
            let req = SetSchedulingStatesRequest::decode(input).map_err(|e| e.to_string())?;
            u.main
                .reviewer
                .lock()
                .unwrap()
                .set_scheduling_states(&req.key, req.states.unwrap_or_default());
            Ok(vec![])
        });
}

/// The main window's `domDone`: the reviewer page shows its first card.
pub fn dom_done(ui: &Arc<Ui>, label: &str) {
    if label != MAIN {
        return;
    }
    crate::testchan::main_page_loaded(ui);
    let is_reviewer = ui
        .shell
        .pages
        .get(MAIN)
        .is_some_and(|p| p.kind == WebviewKind::Reviewer);
    if state(ui) == MainState::Review && is_reviewer {
        let effects = ui.main.reviewer.lock().unwrap().page_ready(&ui.shell.api);
        apply(ui, effects);
    }
}

/// The editor window closed: the reviewer shows the edited card.
pub fn editor_closed(ui: &Arc<Ui>) {
    if state(ui) == MainState::Review {
        let effects = ui
            .main
            .reviewer
            .lock()
            .unwrap()
            .redraw_if_note_changed(&ui.shell.api);
        apply(ui, effects);
    }
}
