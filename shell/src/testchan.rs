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
    /// "editor" (needs `note`), "main" (the deck browser, then every page
    /// the main window loads) or "home".
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
                "shell sound [sound:shell-test.wav][anki:tts lang=en_US]hello[/anki:tts]",
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
        } else if msg["done"] == json!(true) {
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
        _ => {
            // runs on the home page, and again on each page after it (a
            // full sync download shows the deck browser)
            *MAIN_SCRIPT.lock().unwrap() = Some((script, 0, 0));
            ui.show_home()?;
        }
    }
    Ok(())
}
