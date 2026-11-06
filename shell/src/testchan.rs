// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! An in-app test channel. There's no WebDriver for WKWebView, so tests are
//! JS files evaluated in a real page; they report back over the bridge and
//! are checked against backend data, not screenshots.
//!
//! The script is the body of an async function. It gets `log(msg)` and
//! `bridge(cmd)` (a promise of the bridge result), and returns a JSON value
//! with an `ok` field. The shell prints `TEST <json>` lines and exits with 0
//! if `ok` is true, 1 if not, 2 on a timeout.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::Context;
use anyhow::Result;
use serde_json::json;
use serde_json::Value;

use crate::app::Ui;

/// How long a test may run; `ANKI_SHELL_TEST_TIMEOUT` (seconds) overrides it,
/// for runs with the screen locked, when WebKit throttles the hidden pages'
/// timers.
const TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Debug, Clone)]
pub struct TestRun {
    pub script: PathBuf,
    /// "editor" (Edit Current; needs `note`), "add" (Add Cards), "main"
    /// (the deck browser, then every page the main window loads) or
    /// "home".
    pub page: String,
    /// 0 creates a Cloze note with the text "alpha beta gamma".
    pub note: Option<i64>,
}

fn create_test_note(shell: &crate::Shell) -> Result<i64> {
    use anki_proto::notes::AddNoteRequest;
    use anki_proto::notes::AddNoteResponse;
    use anki_proto::notes::Note;
    use anki_proto::notetypes::NotetypeId;
    // the first cloze notetype with at least two fields
    let names: anki_proto::notetypes::NotetypeNames = shell
        .api
        .call("get_notetype_names", &anki_proto::generic::Empty {})?;
    let ntid = names
        .entries
        .iter()
        .find(|entry| {
            shell
                .api
                .call::<anki_proto::generic::Json>(
                    "get_notetype_legacy",
                    &NotetypeId { ntid: entry.id },
                )
                .ok()
                .and_then(|j| serde_json::from_slice::<Value>(&j.json).ok())
                .is_some_and(|nt| {
                    nt["type"] == json!(1) && nt["flds"].as_array().is_some_and(|f| f.len() >= 2)
                })
        })
        .map(|entry| NotetypeId { ntid: entry.id })
        .context("no cloze notetype")?;
    let mut note: Note = shell.api.call("new_note", &ntid)?;
    note.fields[0] = "alpha beta gamma".into();
    let added: AddNoteResponse = shell.api.call(
        "add_note",
        &AddNoteRequest {
            note: Some(note),
            deck_id: 1,
        },
    )?;
    Ok(added.note_id)
}

/// For the "main" page: the script runs again on each page the main window
/// loads, with `testStage` counting them; a result with `next: true` waits
/// for the next page instead of ending the test.
static MAIN_SCRIPT: std::sync::Mutex<Option<(String, u32, i64)>> = std::sync::Mutex::new(None);

/// For the other screens (`--test-page preferences`, ...): the window's
/// label, the script, and how often it ran, as for [MAIN_SCRIPT].
static SCREEN_SCRIPT: std::sync::Mutex<Option<(String, String, u32)>> = std::sync::Mutex::new(None);

/// Set once the script reported its result.
static FINISHED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// The exit code when the last window closes: a test that ended without a
/// result (its window went away) failed.
pub fn exit_code_for_last_window(ui: &Ui) -> i32 {
    static REPORTED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
    if ui.test_mode && !FINISHED.load(std::sync::atomic::Ordering::SeqCst) {
        if !REPORTED.swap(true, std::sync::atomic::Ordering::SeqCst) {
            println!("TEST ended without a result");
        }
        3
    } else {
        0
    }
}

/// The screens a test can open, by `--test-page` name: their window's
/// label.
fn screen_label(page: &str) -> Option<&'static str> {
    match page {
        "preferences" => Some(crate::preferences::LABEL),
        "filtered-deck" => Some(crate::screens::FILTERED_DECK),
        "custom-study" => Some(crate::screens::CUSTOM_STUDY),
        "media-check" => Some(crate::screens::MEDIA_CHECK),
        "empty-cards" => Some(crate::screens::EMPTY_CARDS),
        _ => None,
    }
}

/// Filtered decks open as a new one, custom study on the current deck.
fn open_screen(ui: &Arc<Ui>, page: &str) {
    match page {
        "preferences" => crate::preferences::open(ui),
        "filtered-deck" => crate::screens::open_filtered_deck(ui, 0),
        "custom-study" => crate::screens::open_custom_study(ui, current_deck(ui)),
        "media-check" => crate::screens::open_media_check(ui),
        "empty-cards" => crate::screens::open_empty_cards(ui),
        _ => {}
    }
}

/// The current deck if it's a normal one, else the normal deck with the
/// most cards (custom study is for normal decks).
fn current_deck(ui: &Ui) -> i64 {
    let current = ui
        .shell
        .api
        .call::<anki_proto::decks::Deck>("get_current_deck", &anki_proto::generic::Empty {});
    if let Ok(deck) = current {
        if matches!(deck.kind, Some(anki_proto::decks::deck::Kind::Normal(_))) {
            return deck.id;
        }
    }
    ui.shell
        .api
        .query(
            // cards in a filtered deck have their home deck in odid
            "select did from cards where odid = 0 group by did order by count() desc limit 1",
            &[],
        )
        .ok()
        .and_then(|rows| rows.first().and_then(|r| r[0].as_i64()))
        .unwrap_or(1)
}

/// A webview loaded a page (its `domDone`): run a screen's script there.
pub fn page_loaded(ui: &Ui, label: &str) {
    let mut guard = SCREEN_SCRIPT.lock().unwrap();
    let Some((screen, script, stage)) = guard.as_mut() else {
        return;
    };
    if screen != label {
        return;
    }
    let js = wrap(script, None).replace(
        "const testNote = 0;",
        &format!("const testNote = 0;\n    const testStage = {stage};"),
    );
    *stage += 1;
    ui.eval(label, js);
}

/// The main window loaded a page (its `domDone`).
pub fn main_page_loaded(ui: &Ui) {
    let mut guard = MAIN_SCRIPT.lock().unwrap();
    let Some((script, stage, deck)) = guard.as_mut() else {
        return;
    };
    let js = wrap(script, None).replace(
        "const testNote = 0;",
        &format!(
            "const testNote = 0;\n    const testStage = {stage};\n    const testDeck = {deck};"
        ),
    );
    *stage += 1;
    ui.eval(crate::app::MAIN, js);
}

/// Notes for the "main" page in a deck of their own, which becomes the
/// current deck: Basic, Basic (type in the answer), and a Basic note with
/// a sound and a TTS tag on the front.
fn create_test_deck(shell: &crate::Shell) -> Result<i64> {
    use anki_proto::notes::AddNoteRequest;
    use anki_proto::notes::AddNoteResponse;
    use anki_proto::notes::Note;
    let deck: anki_proto::decks::Deck =
        shell.api.call("new_deck", &anki_proto::generic::Empty {})?;
    // earlier runs' decks
    let names: anki_proto::decks::DeckNames = shell.api.call(
        "get_deck_names",
        &anki_proto::decks::GetDeckNamesRequest {
            skip_empty_default: false,
            include_filtered: true,
        },
    )?;
    let old: Vec<i64> = names
        .entries
        .iter()
        .filter(|e| e.name.starts_with("shell test "))
        .map(|e| e.id)
        .collect();
    if !old.is_empty() {
        let _: anki_proto::collection::OpChangesWithCount = shell
            .api
            .call("remove_decks", &anki_proto::decks::DeckIds { dids: old })?;
    }
    // its own preset, so the profile's daily limits don't hide the cards
    let conf: anki_proto::generic::Json = shell
        .api
        .call("new_deck_config_legacy", &anki_proto::generic::Empty {})?;
    let mut conf: Value = serde_json::from_slice(&conf.json)?;
    conf["name"] = json!("shell test");
    conf["new"]["perDay"] = json!(9999);
    conf["rev"]["perDay"] = json!(9999);
    let conf_id: anki_proto::deck_config::DeckConfigId = shell.api.call(
        "add_or_update_deck_config_legacy",
        &anki_proto::generic::Json {
            json: serde_json::to_vec(&conf)?,
        },
    )?;
    let mut deck = anki_proto::decks::Deck {
        name: format!("shell test {}", chrono::Utc::now().timestamp_millis()),
        ..deck
    };
    if let Some(anki_proto::decks::deck::Kind::Normal(normal)) = deck.kind.as_mut() {
        normal.config_id = conf_id.dcid;
    }
    let added: anki_proto::collection::OpChangesWithId = shell.api.call("add_deck", &deck)?;
    let did = added.id;
    let notes: &[(&str, i32, &[&str])] = &[
        ("Basic", 0, &["shell front one", "shell back one"]),
        ("Basic (type in the answer)", 3, &["shell capital", "Paris"]),
        ("Basic", 0, &["shell latex [$]x^2[/$]", "shell back four"]),
        (
            "Basic",
            0,
            &[
                "shell sound [sound:shell-test.wav][anki:tts lang=en_US]okay okay okay[/anki:tts]",
                "shell back three",
            ],
        ),
    ];
    for (notetype, stock, fields) in notes {
        let ntid = stock_notetype(shell, notetype, *stock)?;
        let mut note: Note = shell.api.call("new_note", &ntid)?;
        for (i, f) in fields.iter().enumerate() {
            note.fields[i] = f.to_string();
        }
        let _: AddNoteResponse = shell.api.call(
            "add_note",
            &AddNoteRequest {
                note: Some(note),
                deck_id: did,
            },
        )?;
    }
    let _: anki_proto::generic::String = shell.api.call(
        "add_media_file",
        &anki_proto::media::AddMediaFileRequest {
            desired_name: "shell-test.wav".into(),
            data: silent_wav(),
        },
    )?;
    let _: anki_proto::collection::OpChanges = shell
        .api
        .call("set_current_deck", &anki_proto::decks::DeckId { did })?;
    Ok(did)
}

/// The notetype called `name`, or a new stock one of `kind`
/// (`StockNotetype.Kind`) if the collection has none.
fn stock_notetype(
    shell: &crate::Shell,
    name: &str,
    kind: i32,
) -> Result<anki_proto::notetypes::NotetypeId> {
    if let Ok(id) = shell.api.call(
        "get_notetype_id_by_name",
        &anki_proto::generic::String { val: name.into() },
    ) {
        return Ok(id);
    }
    let json: anki_proto::generic::Json = shell.api.call(
        "get_stock_notetype_legacy",
        &anki_proto::notetypes::StockNotetype { kind },
    )?;
    let added: anki_proto::collection::OpChangesWithId = shell.api.call(
        "add_notetype_legacy",
        &anki_proto::generic::Json { json: json.json },
    )?;
    Ok(anki_proto::notetypes::NotetypeId { ntid: added.id })
}

/// 0.3 s of 8 kHz 8-bit mono silence.
fn silent_wav() -> Vec<u8> {
    let samples = 2400u32;
    let mut out = Vec::with_capacity(44 + samples as usize);
    out.extend(b"RIFF");
    out.extend((36 + samples).to_le_bytes());
    out.extend(b"WAVEfmt ");
    out.extend(16u32.to_le_bytes());
    out.extend(1u16.to_le_bytes()); // PCM
    out.extend(1u16.to_le_bytes()); // mono
    out.extend(8000u32.to_le_bytes());
    out.extend(8000u32.to_le_bytes()); // bytes per second
    out.extend(1u16.to_le_bytes()); // block align
    out.extend(8u16.to_le_bytes()); // bits per sample
    out.extend(b"data");
    out.extend(samples.to_le_bytes());
    out.extend(std::iter::repeat(128u8).take(samples as usize));
    out
}

/// `setMarkedText:` and `insertText:` on a WKWebView, as an input method
/// composes and commits text.
unsafe fn ime_input(webview: *mut std::ffi::c_void, steps: &[(String, String)]) {
    use objc2::msg_send;
    use objc2::runtime::AnyObject;
    use objc2_foundation::NSRange;
    use objc2_foundation::NSString;
    let view = webview as *mut AnyObject;
    // NSNotFound: no replacement range
    let none = NSRange::new(isize::MAX as usize, 0);
    for (kind, text) in steps {
        let s = NSString::from_str(text);
        let len = text.encode_utf16().count();
        if kind == "mark" {
            let _: () = msg_send![view, setMarkedText: &*s, selectedRange: NSRange::new(len, 0), replacementRange: none];
        } else {
            let _: () = msg_send![view, insertText: &*s, replacementRange: none];
        }
    }
}

fn add_note(shell: &crate::Shell, args: &str) -> Result<Value> {
    let v: Value = serde_json::from_str(args)?;
    let ntid: anki_proto::notetypes::NotetypeId = shell.api.call(
        "get_notetype_id_by_name",
        &anki_proto::generic::String {
            val: v["notetype"].as_str().unwrap_or("Basic").into(),
        },
    )?;
    let mut note: anki_proto::notes::Note = shell.api.call("new_note", &ntid)?;
    for (i, field) in v["fields"].as_array().into_iter().flatten().enumerate() {
        if let Some(slot) = note.fields.get_mut(i) {
            *slot = field.as_str().unwrap_or_default().into();
        }
    }
    let added: anki_proto::notes::AddNoteResponse = shell.api.call(
        "add_note",
        &anki_proto::notes::AddNoteRequest {
            note: Some(note),
            deck_id: v["deck"].as_i64().unwrap_or(1),
        },
    )?;
    Ok(json!(added.note_id))
}

fn update_note(shell: &crate::Shell, args: &str) -> Result<Value> {
    let v: Value = serde_json::from_str(args)?;
    let nid = v["nid"].as_i64().context("nid")?;
    let mut note: anki_proto::notes::Note = shell
        .api
        .call("get_note", &anki_proto::notes::NoteId { nid })?;
    for (i, field) in v["fields"].as_array().into_iter().flatten().enumerate() {
        if let Some(slot) = note.fields.get_mut(i) {
            *slot = field.as_str().unwrap_or_default().into();
        }
    }
    let _: anki_proto::collection::OpChanges = shell.api.call(
        "update_notes",
        &anki_proto::notes::UpdateNotesRequest {
            notes: vec![note],
            skip_undo_entry: false,
        },
    )?;
    Ok(json!(true))
}

fn local_http(v: &Value) -> Result<Value> {
    use std::io::Read;
    use std::io::Write;
    let port = v["port"].as_u64().context("port")? as u16;
    let body = v["body"].as_str().unwrap_or_default();
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port))?;
    stream.set_read_timeout(Some(Duration::from_secs(30)))?;
    write!(
        stream,
        "{} {} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {}\r\n\
         Content-Type: text/html; charset=utf-8\r\nConnection: close\r\n\r\n{body}",
        v["method"].as_str().unwrap_or("GET"),
        v["path"].as_str().unwrap_or("/"),
        body.len(),
    )?;
    let mut response = String::new();
    stream.read_to_string(&mut response)?;
    let status = response
        .split(' ')
        .nth(1)
        .and_then(|s| s.parse::<u16>().ok())
        .unwrap_or(0);
    let body = response
        .split_once("\r\n\r\n")
        .map(|(_, b)| b)
        .unwrap_or_default();
    Ok(json!({ "status": status, "body": body }))
}

fn wrap(script: &str, note: Option<i64>) -> String {
    format!(
        r#"(async () => {{
    const log = (msg) => pycmd("test:" + JSON.stringify({{ log: String(msg) }}));
    const bridge = (cmd) => pycmdPromise(cmd);
    const testNote = {note};
    let result;
    try {{
        result = await (async () => {{
{script}
        }})();
    }} catch (e) {{
        result = {{ ok: false, error: String(e), stack: e && e.stack }};
    }}
    pycmd("test:" + JSON.stringify({{ done: true, result }}));
}})();"#,
        note = note.unwrap_or(0)
    )
}

pub fn start(ui: &Arc<Ui>, run: TestRun) -> Result<()> {
    let script = std::fs::read_to_string(&run.script)
        .with_context(|| format!("reading {}", run.script.display()))?;
    let u = ui.clone();
    ui.shell.bridge.register("test", move |shell, _, args| {
        let msg: Value = serde_json::from_str(args).unwrap_or(Value::Null);
        if msg["done"] == json!(true) && msg["result"]["next"] == json!(true) {
            println!("TEST stage {}", msg["result"]);
            // a screen's script goes on in the window it opens (`in`)
            if let Some(label) = msg["result"]["in"].as_str() {
                if let Some((screen, _, _)) = SCREEN_SCRIPT.lock().unwrap().as_mut() {
                    *screen = label.to_string();
                }
            }
        } else if msg["done"] == json!(true) {
            FINISHED.store(true, std::sync::atomic::Ordering::SeqCst);
            let result = &msg["result"];
            println!("TEST {result}");
            let unknown = shell.bridge.unknown_commands();
            if !unknown.is_empty() {
                println!("TEST unknown bridge commands: {unknown:?}");
            }
            let code = if result["ok"] == json!(true) { 0 } else { 1 };
            u.app.exit(code);
        } else {
            println!("TEST log {}", msg["log"].as_str().unwrap_or_default());
        }
        Value::Null
    });
    // open dialogs, and answering the oldest (`{"button": 0, "fields": {}}`)
    let u = ui.clone();
    ui.shell.bridge.register("testDialogs", move |_, _, _| {
        json!(crate::main_window::open_dialogs(&u))
    });
    let u = ui.clone();
    ui.shell.bridge.register("testDialog", move |_, _, answer| {
        match crate::main_window::open_dialogs(&u).first() {
            Some((label, _)) => {
                crate::main_window::dialog_answered(&u, label, answer);
                json!(true)
            }
            None => json!(false),
        }
    });
    // paste from a scratch pasteboard with these contents
    // (`{"html": .., "text": .., "urls": [..]}`), never the user's
    static SCRATCH: std::sync::Mutex<Option<crate::pasteboard::ScratchPasteboard>> =
        std::sync::Mutex::new(None);
    ui.shell.bridge.register("testPasteboard", |_, _, args| {
        let v: Value = serde_json::from_str(args).unwrap_or(Value::Null);
        let mut scratch = SCRATCH.lock().unwrap();
        let board = scratch.get_or_insert_with(crate::pasteboard::ScratchPasteboard::new);
        let urls: Vec<&str> = v["urls"]
            .as_array()
            .map(|a| a.iter().filter_map(Value::as_str).collect())
            .unwrap_or_default();
        board.write(v["html"].as_str(), v["text"].as_str(), &urls, None);
        crate::editor_window::use_pasteboard(Some(board.name().to_string()));
        json!(true)
    });
    // text input as an input method sends it, to the page's webview:
    // `[["mark", "に"], ["mark", "にほ"], ["insert", "日本"]]`
    let u = ui.clone();
    ui.shell.bridge.register("testIme", move |_, label, args| {
        use tauri::Manager;
        let steps: Vec<(String, String)> = serde_json::from_str(args).unwrap_or_default();
        let Some(window) = u.app.get_webview_window(label) else {
            return json!(false);
        };
        let (tx, rx) = std::sync::mpsc::channel();
        let sent = window.with_webview(move |webview| {
            // SAFETY: inner() is the WKWebView, an NSTextInputClient; this
            // runs on the main thread
            unsafe { ime_input(webview.inner(), &steps) };
            let _ = tx.send(());
        });
        json!(sent.is_ok() && rx.recv_timeout(Duration::from_secs(5)).is_ok())
    });
    // Edit Current on another note, saving the shown one first (the
    // reviewer's edit; `openEditor` is the home page's only)
    let u = ui.clone();
    ui.shell
        .bridge
        .register("testOpenEditor", move |_, _, nid| {
            json!(nid
                .parse()
                .map(|nid| u.open_editor(nid).is_ok())
                .unwrap_or(false))
        });
    // the absolute path of a media file with this extension, if any
    ui.shell.bridge.register("testMediaFile", |shell, _, ext| {
        let folder = shell.media_folder();
        let found = folder
            .as_deref()
            .and_then(|dir| std::fs::read_dir(dir).ok())
            .and_then(|entries| {
                entries
                    .flatten()
                    .map(|e| e.path())
                    .find(|p| p.extension().is_some_and(|e| e.eq_ignore_ascii_case(ext)))
            });
        json!(found.map(|p| p.to_string_lossy().into_owned()))
    });
    // an add-on side panel with this HTML in the sending editor
    let u = ui.clone();
    ui.shell
        .bridge
        .register("testSidePanel", move |_, label, html| {
            crate::editor_window::set_side_panel(&u, label, Some(html.to_string()), true);
            json!(true)
        });
    // a file in the Anki base folder (what an add-on wrote there), as text
    let u = ui.clone();
    ui.shell.bridge.register("testReadFile", move |_, _, path| {
        let base = u.settings.lock().unwrap().anki_base();
        if std::path::Path::new(path)
            .components()
            .any(|c| !matches!(c, std::path::Component::Normal(_)))
        {
            return Value::Null;
        }
        std::fs::read_to_string(base.join(path))
            .map(Value::from)
            .unwrap_or(Value::Null)
    });
    // a request to a server on this machine (an add-on's):
    // `{"port", "method", "path", "body"}` -> `{"status", "body"}`
    ui.shell.bridge.register("testHttp", |_, _, args| {
        let v: Value = serde_json::from_str(args).unwrap_or(Value::Null);
        local_http(&v).unwrap_or_else(|e| json!({ "error": e.to_string() }))
    });
    // the open windows' labels
    let u = ui.clone();
    ui.shell.bridge.register("testWindows", move |_, _, _| {
        use tauri::Manager;
        let mut labels: Vec<String> = u.app.webview_windows().keys().cloned().collect();
        labels.sort();
        json!(labels)
    });
    // read-only SQL, for checking what a test changed
    ui.shell.bridge.register("testQuery", |shell, _, sql| {
        shell
            .api
            .query(sql, &[])
            .map(|rows| json!(rows))
            .unwrap_or_else(|e| json!({ "error": e.message }))
    });
    // the fields as saved in the collection, for checking edits
    ui.shell.bridge.register("testNote", |shell, _, nid| {
        let note: Result<anki_proto::notes::Note, _> = shell.api.call(
            "get_note",
            &anki_proto::notes::NoteId {
                nid: nid.parse().unwrap_or(0),
            },
        );
        note.map(|n| json!(n.fields)).unwrap_or(Value::Null)
    });
    // add-ons: their Tools items, choosing one, the reviewer menu's items
    // for a card, and JS for a window (a panel), whose `pycmd("test:...")`
    // reports here
    let u = ui.clone();
    ui.shell.bridge.register("testAddonMenu", move |_, _, _| {
        json!(u
            .plugins
            .tools
            .lock()
            .unwrap()
            .iter()
            .map(|i| json!({"id": i.id, "text": i.text, "enabled": i.enabled}))
            .collect::<Vec<_>>())
    });
    let u = ui.clone();
    ui.shell
        .bridge
        .register("testAddonAction", move |_, _, id| {
            crate::plugins::ui::menu_action(&u, id.parse().unwrap_or(0));
            Value::Null
        });
    let u = ui.clone();
    ui.shell
        .bridge
        .register("testContextMenu", move |_, _, cid| {
            json!(
                crate::plugins::ui::context_menu(&u, cid.parse().unwrap_or(0))
                    .iter()
                    .map(|i| json!({"id": i.id, "text": i.text}))
                    .collect::<Vec<_>>()
            )
        });
    // a value another window (a panel) leaves for the script
    let stash = Arc::new(std::sync::Mutex::new(Value::Null));
    let st = stash.clone();
    ui.shell.bridge.register("testStash", move |_, _, value| {
        let mut stash = st.lock().unwrap();
        if value.is_empty() {
            return std::mem::take(&mut *stash);
        }
        *stash = serde_json::from_str(value).unwrap_or(Value::Null);
        Value::Null
    });
    // a note: `{"notetype": name, "deck": id (default 1), "fields": [..]}`
    ui.shell.bridge.register("testAddNote", |shell, _, args| {
        add_note(shell, args).unwrap_or_else(|e| json!({ "error": format!("{e:#}") }))
    });
    // change a note's fields: `{"nid": id, "fields": [..]}`
    ui.shell
        .bridge
        .register("testUpdateNote", |shell, _, args| {
            update_note(shell, args).unwrap_or_else(|e| json!({ "error": format!("{e:#}") }))
        });
    // a media file with this text: `{"name", "text"}` -> the name used
    ui.shell
        .bridge
        .register("testAddMediaFile", |shell, _, args| {
            let v: Value = serde_json::from_str(args).unwrap_or(Value::Null);
            shell
                .api
                .call::<anki_proto::generic::String>(
                    "add_media_file",
                    &anki_proto::media::AddMediaFileRequest {
                        desired_name: v["name"].as_str().unwrap_or("shell-test.txt").into(),
                        data: v["text"].as_str().unwrap_or_default().as_bytes().to_vec(),
                    },
                )
                .map(|name| json!(name.val))
                .unwrap_or_else(|e| json!({ "error": e.message }))
        });
    // LaTeX commands for this run only (`LatexCommands` JSON), so checks
    // don't run the user's own toolchain
    ui.shell.bridge.register(
        "testLatexToolchain",
        |_, _, args| match serde_json::from_str(args) {
            Ok(commands) => {
                crate::latex::set_toolchain(commands);
                json!(true)
            }
            Err(_) => json!(false),
        },
    );
    // close a window as its close button does (CloseRequested)
    let u = ui.clone();
    ui.shell.bridge.register("testClose", move |_, _, label| {
        use tauri::Manager;
        match u.app.get_webview_window(label) {
            Some(window) => json!(window.close().is_ok()),
            None => json!(false),
        }
    });
    // the settings file as the shell has it
    let u = ui.clone();
    ui.shell.bridge.register("testSettings", move |_, _, _| {
        json!(*u.settings.lock().unwrap())
    });
    // render LaTeX in `text` as a card would (the first notetype's header),
    // with the image's contents
    ui.shell
        .bridge
        .register("testRenderLatex", |shell, _, text| {
            let names: anki_proto::notetypes::NotetypeNames = shell
                .api
                .call("get_notetype_names", &anki_proto::generic::Empty {})
                .unwrap_or_default();
            let (Some(first), Some(media)) = (names.entries.first(), shell.media_folder()) else {
                return Value::Null;
            };
            let cfg = crate::latex::latex_config(&shell.api, first.id).unwrap_or_default();
            let (html, errors) = crate::latex::render_latex(&shell.api, &media, text, &cfg);
            let image = html
                .split("src=\"")
                .nth(1)
                .and_then(|rest| rest.split('"').next())
                .and_then(|name| std::fs::read(media.join(name)).ok())
                .map(|data| String::from_utf8_lossy(&data).into_owned());
            json!({ "html": html, "errors": errors, "image": image, "svg": cfg.svg })
        });
    let u = ui.clone();
    ui.shell.bridge.register("testEval", move |_, _, args| {
        let (label, js) = args.split_once('\n').unwrap_or((args, ""));
        u.eval(label, js.to_string());
        Value::Null
    });
    let u = ui.clone();
    std::thread::spawn(move || {
        let timeout = std::env::var("ANKI_SHELL_TEST_TIMEOUT")
            .ok()
            .and_then(|s| s.parse().ok())
            .map_or(TIMEOUT, Duration::from_secs);
        std::thread::sleep(timeout);
        println!("TEST timeout");
        u.app.exit(2);
    });
    match run.page.as_str() {
        "editor" => {
            anyhow::ensure!(
                ui.shell.open_profile_paths().is_some(),
                "no profile open: {}",
                ui.status.lock().unwrap()
            );
            let nid = match run.note.context("--test-note is needed for the editor")? {
                0 => create_test_note(&ui.shell)?,
                nid => nid,
            };
            println!("TEST note {nid}");
            let js = wrap(&script, Some(nid));
            let label = "editor";
            let u = ui.clone();
            // run once the note is in the page
            ui.shell.bridge.register("noteLoaded", move |_, l, _| {
                if l == label {
                    u.eval(label, js.clone());
                }
                Value::Null
            });
            ui.open_editor(nid)?;
        }
        "add" => {
            anyhow::ensure!(
                ui.shell.open_profile_paths().is_some(),
                "no profile open: {}",
                ui.status.lock().unwrap()
            );
            let js = wrap(&script, None);
            let label = crate::editor_window::ADD;
            let u = ui.clone();
            let started = std::sync::atomic::AtomicBool::new(false);
            // run once, when the first note is in the page (adding loads
            // the next note, which sends noteLoaded again)
            ui.shell.bridge.register("noteLoaded", move |_, l, _| {
                if l == label && !started.swap(true, std::sync::atomic::Ordering::SeqCst) {
                    u.eval(label, js.clone());
                }
                Value::Null
            });
            crate::editor_window::open_add(ui)?;
        }
        "main" => {
            anyhow::ensure!(
                ui.shell.open_profile_paths().is_some(),
                "no profile open: {}",
                ui.status.lock().unwrap()
            );
            let did = create_test_deck(&ui.shell)?;
            println!("TEST deck {did}");
            *MAIN_SCRIPT.lock().unwrap() = Some((script, 0, did));
            ui.show_main()?;
        }
        page if screen_label(page).is_some() => {
            anyhow::ensure!(
                ui.shell.open_profile_paths().is_some(),
                "no profile open: {}",
                ui.status.lock().unwrap()
            );
            let label = screen_label(page).unwrap_or_default();
            *SCREEN_SCRIPT.lock().unwrap() = Some((label.into(), script, 0));
            open_screen(ui, page);
        }
        _ => {
            // runs on the home page, and again on each page after it (a
            // full sync download shows the deck browser)
            *MAIN_SCRIPT.lock().unwrap() = Some((script, 0, 0));
            ui.show_home()?;
        }
    }
    Ok(())
}
