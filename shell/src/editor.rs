// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The stock editor page in "edit current" mode: loading a note
//! (`Editor.loadNote`) and saving fields as they change (`key:`/`blur:`/
//! `saveFields:`). The rest of `editor.py`'s bridge is phase 3.

use std::collections::HashMap;
use std::sync::Mutex;

use anki_proto::notes::Note;
use anki_proto::notes::NoteId;
use anki_proto::notes::UpdateNotesRequest;
use anki_proto::notetypes::GetClozeFieldOrdsResponse;
use anki_proto::notetypes::NotetypeId;
use serde_json::json;
use serde_json::Value;

use crate::api::ApiError;
use crate::Shell;

/// Webview label -> note being edited.
#[derive(Default)]
pub struct Editors(Mutex<HashMap<String, Note>>);

impl Editors {
    pub fn note(&self, label: &str) -> Option<Note> {
        self.0.lock().unwrap().get(label).cloned()
    }

    fn set(&self, label: &str, note: Note) {
        self.0.lock().unwrap().insert(label.to_string(), note);
    }
}

/// JS that loads note `nid` into the editor page, as `Editor.loadNote` does,
/// then sends `noteLoaded`.
pub fn load_note_js(
    shell: &Shell,
    editors: &Editors,
    label: &str,
    nid: i64,
) -> Result<String, ApiError> {
    let note: Note = shell.api.call("get_note", &NoteId { nid })?;
    let notetype: anki_proto::generic::Json = shell.api.call(
        "get_notetype_legacy",
        &NotetypeId {
            ntid: note.notetype_id,
        },
    )?;
    let cloze: GetClozeFieldOrdsResponse = shell.api.call(
        "get_cloze_field_ords",
        &NotetypeId {
            ntid: note.notetype_id,
        },
    )?;
    let nt: Value = serde_json::from_slice(&notetype.json).unwrap_or_default();
    let flds = nt["flds"].as_array().cloned().unwrap_or_default();
    let fields: Vec<Value> = flds
        .iter()
        .zip(&note.fields)
        .map(|(f, v)| json!([f["name"], v]))
        .collect();
    let per_field = |key: &str, default: Value| -> Value {
        flds.iter()
            .map(|f| f.get(key).cloned().unwrap_or(default.clone()))
            .collect()
    };
    let cloze_fields: Vec<bool> = (0..flds.len() as u32)
        .map(|ord| cloze.ords.contains(&ord))
        .collect();
    let templates: Vec<Value> = nt["tmpls"]
        .as_array()
        .map(|t| t.iter().map(|t| json!([t["qfmt"], t["afmt"]])).collect())
        .unwrap_or_default();
    let fonts: Vec<Value> = flds
        .iter()
        .map(|f| json!([f["font"], f["size"], f["rtl"]]))
        .collect();
    let js = format!(
        r##"require("anki/ui").loaded.then(async () => {{
            saveSession();
            setMathjaxEnabled(true);
            setIsImageOcclusion(false);
            setNotetypeMeta({meta});
            await setNotetypeTemplates({templates});
            setNotetypeCss({css});
            setFields({fields});
            setCollapsed({collapsed});
            setClozeFields({cloze_fields});
            setPlainTexts({plain});
            setDescriptions({descriptions});
            setFonts({fonts});
            focusField(null);
            setNoteId({nid});
            setColorButtons(["#0000ff", "#0000ff"]);
            setTags({tags});
            setTagsCollapsed(false);
            setShrinkImages(true);
            setCloseHTMLTags(true);
            triggerChanges();
            // after the next paint, or a timer where a hidden page gets none
            const painted = new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            await Promise.race([painted, new Promise((resolve) => setTimeout(resolve, 100))]);
            pycmd("noteLoaded");
        }})"##,
        meta = json!({"id": note.notetype_id, "modTime": nt["mod"]}),
        templates = Value::Array(templates),
        css = nt["css"],
        fields = Value::Array(fields),
        collapsed = per_field("collapsed", json!(false)),
        cloze_fields = json!(cloze_fields),
        plain = per_field("plainText", json!(false)),
        descriptions = per_field("description", json!("")),
        fonts = Value::Array(fonts),
        nid = note.id,
        tags = json!(note.tags),
    );
    editors.set(label, note);
    Ok(js)
}

/// `editor.munge_html` and `remove_null_bytes`.
fn munge_html(txt: &str) -> String {
    if txt == "<br>" || txt == "<div><br></div>" {
        String::new()
    } else {
        txt.replace('\0', "")
    }
}

/// `key:<ord>:<nid>:<html>` and `blur:...`: save one field of the note.
pub fn save_field(shell: &Shell, editors: &Editors, label: &str, args: &str) -> Value {
    let mut parts = args.splitn(3, ':');
    let (Some(ord), Some(nid), Some(txt)) = (parts.next(), parts.next(), parts.next()) else {
        tracing::warn!(args, "malformed field save");
        return Value::Null;
    };
    let (Ok(ord), nid) = (ord.parse::<usize>(), nid.parse::<i64>().unwrap_or(0)) else {
        return Value::Null;
    };
    let Some(mut note) = editors.note(label) else {
        return Value::Null;
    };
    if note.id != nid {
        tracing::debug!("ignored late field save");
        return Value::Null;
    }
    let Some(field) = note.fields.get_mut(ord) else {
        return Value::Null;
    };
    *field = munge_html(txt);
    save_note(shell, editors, label, note);
    Value::Null
}

/// `saveFields:<nid>:<json list>`: all fields at once.
pub fn save_fields(shell: &Shell, editors: &Editors, label: &str, args: &str) -> Value {
    let Some((nid, fields)) = args.split_once(':') else {
        return Value::Null;
    };
    let (Ok(nid), Ok(fields)) = (
        nid.parse::<i64>(),
        serde_json::from_str::<Vec<String>>(fields),
    ) else {
        tracing::warn!("ignored malformed field batch");
        return Value::Null;
    };
    let Some(mut note) = editors.note(label) else {
        return Value::Null;
    };
    if note.id != nid || fields.len() != note.fields.len() {
        return Value::Null;
    }
    note.fields = fields.iter().map(|f| munge_html(f)).collect();
    save_note(shell, editors, label, note);
    json!(true)
}

fn save_note(shell: &Shell, editors: &Editors, label: &str, note: Note) {
    let result: Result<anki_proto::collection::OpChanges, _> = shell.api.call(
        "update_notes",
        &UpdateNotesRequest {
            notes: vec![note.clone()],
            skip_undo_entry: false,
        },
    );
    match result {
        Ok(_) => editors.set(label, note),
        Err(err) => tracing::error!(%err, "saving note failed"),
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn empty_looking_fields_are_saved_empty() {
        assert_eq!(munge_html("<br>"), "");
        assert_eq!(munge_html("<div><br></div>"), "");
        assert_eq!(munge_html("a\0b"), "ab");
    }
}
