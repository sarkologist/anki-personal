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

const TIMEOUT: Duration = Duration::from_secs(120);

#[derive(Debug, Clone)]
pub struct TestRun {
    pub script: PathBuf,
    /// "editor" (needs `note`) or "home".
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
        if msg["done"] == json!(true) {
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
        std::thread::sleep(TIMEOUT);
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
        _ => {
            ui.show_home()?;
            ui.eval(crate::app::MAIN, wrap(&script, None));
        }
    }
    Ok(())
}
