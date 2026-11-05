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

    pub fn clear(&self, label: &str) {
        self.0.lock().unwrap().remove(label);
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

/// The note shown in `label`, as currently stored, if it is `nid`. Saves
/// start from the stored note, so changes made elsewhere while the editor
/// is open (AnkiConnect, say) aren't reverted.
fn current_note(shell: &Shell, editors: &Editors, label: &str, nid: i64) -> Option<Note> {
    if editors.note(label).map(|n| n.id) != Some(nid) {
        tracing::debug!("ignored late save");
        return None;
    }
    shell.api.call("get_note", &NoteId { nid }).ok()
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
    let Some(mut note) = current_note(shell, editors, label, nid) else {
        return Value::Null;
    };
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
    let Some(mut note) = current_note(shell, editors, label, nid) else {
        return Value::Null;
    };
    if fields.len() != note.fields.len() {
        return Value::Null;
    }
    note.fields = fields.iter().map(|f| munge_html(f)).collect();
    save_note(shell, editors, label, note);
    json!(true)
}

/// `saveTags:<json list>`, for the note shown.
pub fn save_tags(shell: &Shell, editors: &Editors, label: &str, args: &str) -> Value {
    let Ok(tags) = serde_json::from_str::<Vec<String>>(args) else {
        tracing::warn!("ignored malformed tags");
        return Value::Null;
    };
    let Some(nid) = editors.note(label).map(|n| n.id) else {
        return Value::Null;
    };
    let Some(mut note) = current_note(shell, editors, label, nid) else {
        return Value::Null;
    };
    note.tags = tags;
    save_note(shell, editors, label, note);
    Value::Null
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

#[cfg(test)]
mod store_test {
    use anki_proto::notes::AddNoteRequest;
    use anki_proto::notes::AddNoteResponse;

    use super::*;
    use crate::api::Api;
    use crate::profile::ProfilePaths;

    fn shell_with_note() -> (tempfile::TempDir, Shell, i64) {
        let dir = tempfile::tempdir().unwrap();
        let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), Default::default());
        shell
            .open_profile(ProfilePaths::new(dir.path(), "User 1"))
            .unwrap();
        let ntid: NotetypeId = shell
            .api
            .call(
                "get_notetype_id_by_name",
                &anki_proto::generic::String {
                    val: "Basic".into(),
                },
            )
            .unwrap();
        let mut note: Note = shell.api.call("new_note", &ntid).unwrap();
        note.fields = vec!["front".into(), "back".into()];
        let added: AddNoteResponse = shell
            .api
            .call(
                "add_note",
                &AddNoteRequest {
                    note: Some(note),
                    deck_id: 1,
                },
            )
            .unwrap();
        (dir, shell, added.note_id)
    }

    fn stored(shell: &Shell, nid: i64) -> Note {
        shell.api.call("get_note", &NoteId { nid }).unwrap()
    }

    #[test]
    fn field_saves_keep_changes_made_elsewhere() {
        let (_dir, shell, nid) = shell_with_note();
        let editors = Editors::default();
        load_note_js(&shell, &editors, "editor", nid).unwrap();
        // e.g. AnkiConnect's addTags/updateNoteFields while the editor is open
        let mut other = stored(&shell, nid);
        other.tags = vec!["added".into()];
        other.fields[1] = "changed back".into();
        let _: anki_proto::collection::OpChanges = shell
            .api
            .call(
                "update_notes",
                &UpdateNotesRequest {
                    notes: vec![other],
                    skip_undo_entry: false,
                },
            )
            .unwrap();

        save_field(&shell, &editors, "editor", &format!("0:{nid}:new front"));

        let note = stored(&shell, nid);
        assert_eq!(note.fields, vec!["new front", "changed back"]);
        assert_eq!(note.tags, vec!["added"]);
    }

    #[test]
    fn tags_are_saved() {
        let (_dir, shell, nid) = shell_with_note();
        let editors = Editors::default();
        load_note_js(&shell, &editors, "editor", nid).unwrap();

        save_tags(&shell, &editors, "editor", r#"["one", "two"]"#);

        assert_eq!(stored(&shell, nid).tags, vec!["one", "two"]);
    }
}
