// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The stock editor page (`editor.js`) in its two windows, Add Cards and
//! Edit Current: `editor.py`'s `Editor` and `addcards.py` without Qt. This
//! is the note shown in each window and what its bridge commands do; the
//! windows, dialogs and pasteboard are in `editor_window.rs`.
//!
//! Add-ons (the phase 2 plugin host) hook in through [EditorEvent]
//! listeners and the side panel (`editor_window::set_side_panel`).

use std::collections::HashMap;
use std::collections::VecDeque;
use std::sync::Mutex;
use std::sync::RwLock;

use anki_proto::config::SetConfigJsonRequest;
use anki_proto::generic;
use anki_proto::notes::AddNoteRequest;
use anki_proto::notes::AddNoteResponse;
use anki_proto::notes::DefaultsForAddingRequest;
use anki_proto::notes::Note;
use anki_proto::notes::NoteFieldsCheckResponse;
use anki_proto::notes::NoteId;
use anki_proto::notes::UpdateNotesRequest;
use anki_proto::notetypes::GetClozeFieldOrdsResponse;
use anki_proto::notetypes::NotetypeId;
use anki_proto::notetypes::UpdateNotetypeLegacyRequest;
use serde_json::json;
use serde_json::Value;

use crate::api::Api;
use crate::api::ApiError;
use crate::settings::EditorPrefs;
use crate::Shell;

/// How many added notes the History button lists (`AddCards.addHistory`).
const HISTORY_SIZE: usize = 15;
/// LaTeX previews kept per editor (`LATEX_PREVIEW_CACHE_SIZE`).
const LATEX_PREVIEW_CACHE_SIZE: usize = 128;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Add,
    EditCurrent,
}

impl Mode {
    /// The page's `setupEditor` mode.
    pub fn page_mode(self) -> &'static str {
        match self {
            Mode::Add => "add",
            Mode::EditCurrent => "review",
        }
    }
}

/// One editor window's state (`Editor` plus `AddCards`).
#[derive(Debug, Clone)]
pub struct Session {
    pub mode: Mode,
    /// The note shown. In Add mode it isn't in the collection yet (id 0)
    /// and lives only here; in Edit Current it's the note as last saved.
    pub note: Note,
    /// Add mode: the deck the note goes to.
    pub deck_id: i64,
    pub current_field: Option<u32>,
    /// As `currentField`, but kept after a blur.
    pub last_field_index: Option<u32>,
    /// The page's `EditorState` (fields, image occlusion picker/masks).
    pub state: i32,
    /// Add mode: notes added from this window, newest first.
    pub history: Vec<i64>,
    /// Add mode: the note last added, so its sticky fields don't count as
    /// unsaved input.
    pub last_added: Option<Note>,
    /// Edit Current: the card being reviewed.
    pub card_id: Option<i64>,
    /// Add mode: which note this is. Every note shown gets a new one, so
    /// saves and adds meant for an earlier note (all id 0) can be told
    /// apart; the page gets its negative as the note id ([Session::page_id]).
    pub generation: i64,
    /// Add mode: the generation the user agreed to add without a cloze.
    pub cloze_confirmed: Option<i64>,
}

static NEXT_GENERATION: std::sync::atomic::AtomicI64 = std::sync::atomic::AtomicI64::new(1);

pub fn next_generation() -> i64 {
    NEXT_GENERATION.fetch_add(1, std::sync::atomic::Ordering::SeqCst)
}

impl Session {
    pub fn edit(note: Note, card_id: Option<i64>) -> Session {
        Session {
            mode: Mode::EditCurrent,
            note,
            deck_id: 0,
            current_field: None,
            last_field_index: None,
            state: 0,
            history: vec![],
            last_added: None,
            card_id,
            generation: 0,
            cloze_confirmed: None,
        }
    }

    pub fn add(note: Note, deck_id: i64) -> Session {
        Session {
            mode: Mode::Add,
            deck_id,
            generation: next_generation(),
            ..Session::edit(note, None)
        }
    }

    /// The note id the page sends with its saves: the note's, or in Add
    /// mode the generation's negative.
    pub fn page_id(&self) -> i64 {
        match self.mode {
            Mode::Add => -self.generation,
            Mode::EditCurrent => self.note.id,
        }
    }

    /// Show `note`, a new note, in Add mode.
    pub fn replace_note(&mut self, note: Note) {
        self.note = note;
        self.generation = next_generation();
        self.cloze_confirmed = None;
    }
}

/// What add-ons hear about, as `gui_hooks.editor_*` and
/// `add_cards_did_add_note`.
#[derive(Debug, Clone, PartialEq)]
pub enum EditorEvent {
    NoteLoaded {
        label: String,
        nid: i64,
    },
    FieldFocused {
        label: String,
        ord: u32,
    },
    /// `editor_did_unfocus_field`, after the field was saved.
    FieldUnfocused {
        label: String,
        ord: u32,
    },
    /// `editor_did_fire_typing_timer`: a `key:` save.
    TypingTimer {
        label: String,
    },
    TagsUpdated {
        label: String,
    },
    StateChanged {
        label: String,
        new: i32,
        old: i32,
    },
    NoteAdded {
        label: String,
        nid: i64,
    },
}

pub type Listener = Box<dyn Fn(&Shell, &EditorEvent) + Send + Sync>;

/// Webview label -> editor session, plus what's shared between them.
#[derive(Default)]
pub struct Editors {
    sessions: Mutex<HashMap<String, Session>>,
    listeners: RwLock<Vec<Listener>>,
    latex: Mutex<VecDeque<(String, Value)>>,
}

impl Editors {
    pub fn note(&self, label: &str) -> Option<Note> {
        self.sessions
            .lock()
            .unwrap()
            .get(label)
            .map(|s| s.note.clone())
    }

    pub fn session(&self, label: &str) -> Option<Session> {
        self.sessions.lock().unwrap().get(label).cloned()
    }

    pub fn set_session(&self, label: &str, session: Session) {
        self.sessions
            .lock()
            .unwrap()
            .insert(label.to_string(), session);
    }

    /// Change the session in `label`, if there is one.
    pub fn update<T>(&self, label: &str, f: impl FnOnce(&mut Session) -> T) -> Option<T> {
        self.sessions.lock().unwrap().get_mut(label).map(f)
    }

    pub fn clear(&self, label: &str) {
        self.sessions.lock().unwrap().remove(label);
    }

    fn set(&self, label: &str, note: Note) {
        self.update(label, |s| s.note = note);
    }

    /// Listen to every editor's events (the plugin host, side panels).
    pub fn listen(&self, listener: Listener) {
        self.listeners.write().unwrap().push(listener);
    }

    pub fn emit(&self, shell: &Shell, event: EditorEvent) {
        for listener in self.listeners.read().unwrap().iter() {
            listener(shell, &event);
        }
    }
}

fn notetype_json(api: &Api, ntid: i64) -> Result<Value, ApiError> {
    let json: generic::Json = api.call("get_notetype_legacy", &NotetypeId { ntid })?;
    serde_json::from_slice(&json.json).map_err(|e| ApiError {
        kind: -1,
        message: e.to_string(),
    })
}

/// A JSON collection config value (`col.get_config(key, default)`).
pub fn config_json(api: &Api, key: &str, default: Value) -> Value {
    api.call::<generic::Json>("get_config_json", &generic::String { val: key.into() })
        .ok()
        .and_then(|j| serde_json::from_slice(&j.json).ok())
        .unwrap_or(default)
}

fn set_config_json(api: &Api, key: &str, value: Value) {
    let out: Result<generic::Empty, _> = api.call(
        "set_config_json_no_undo",
        &SetConfigJsonRequest {
            key: key.into(),
            value_json: value.to_string().into_bytes(),
            undoable: false,
        },
    );
    if let Err(err) = out {
        tracing::error!(%err, key, "saving config failed");
    }
}

fn is_image_occlusion(nt: &Value) -> bool {
    // StockNotetype.OriginalStockKind.ORIGINAL_STOCK_KIND_IMAGE_OCCLUSION
    nt["originalStockKind"] == json!(6)
}

/// JS that shows the session's note in the editor page, as
/// `Editor.loadNote`, then sends `noteLoaded`.
pub fn load_note_js(
    shell: &Shell,
    editors: &Editors,
    label: &str,
    focus_to: Option<u32>,
    prefs: &EditorPrefs,
) -> Result<String, ApiError> {
    let session = editors.session(label).ok_or_else(|| ApiError {
        kind: -1,
        message: "no note in this editor".into(),
    })?;
    let api = &shell.api;
    let note = &session.note;
    let nt = notetype_json(api, note.notetype_id)?;
    let cloze: GetClozeFieldOrdsResponse = api.call(
        "get_cloze_field_ords",
        &NotetypeId {
            ntid: note.notetype_id,
        },
    )?;
    let flds = nt["flds"].as_array().cloned().unwrap_or_default();
    let mut fields = vec![];
    for (f, v) in flds.iter().zip(&note.fields) {
        let escaped: generic::String =
            api.call("encode_iri_paths", &generic::String { val: v.clone() })?;
        fields.push(json!([f["name"], escaped.val]));
    }
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
    let io = is_image_occlusion(&nt);
    let tags_collapsed = match session.mode {
        Mode::Add => prefs.add_tags_collapsed,
        Mode::EditCurrent => prefs.edit_tags_collapsed,
    };
    let colors = json!([
        prefs.last_text_color.as_deref().unwrap_or("#0000ff"),
        prefs.last_highlight_color.as_deref().unwrap_or("#0000ff"),
    ]);
    let mut extra = String::new();
    if session.mode == Mode::Add {
        extra += &format!("setSticky({});", per_field("sticky", json!(false)));
    } else if io {
        extra += &format!(
            "setupMaskEditor({});",
            json!({"mode": {"kind": "edit", "noteId": note.id}})
        );
    }
    let check: NoteFieldsCheckResponse = api.call("note_fields_check", note)?;
    // set_cloze_button and set_image_occlusion_button
    let toolbar = format!(
        "const toolbar = require(\"anki/NoteEditor\").instances[0].toolbar.toolbar; \
         toolbar.{}(\"cloze\"); toolbar.{}(\"image-occlusion-button\");",
        if nt["type"] == json!(1) {
            "show"
        } else {
            "hide"
        },
        if io { "show" } else { "hide" },
    );
    let js = format!(
        r##"require("anki/ui").loaded.then(async () => {{
            saveSession();
            setMathjaxEnabled({mathjax});
            setIsImageOcclusion({io});
            setNotetypeMeta({meta});
            await setNotetypeTemplates({templates});
            setNotetypeCss({css});
            setFields({fields});
            setCollapsed({collapsed});
            setClozeFields({cloze_fields});
            setPlainTexts({plain});
            setDescriptions({descriptions});
            setFonts({fonts});
            focusField({focus});
            setNoteId({nid});
            setColorButtons({colors});
            setTags({tags});
            setTagsCollapsed({tags_collapsed});
            setShrinkImages({shrink});
            setCloseHTMLTags({close_tags});
            triggerChanges();
            {extra}
            {dupes}
            {toolbar}
            // after the next paint, or a timer where a hidden page gets none
            const painted = new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            await Promise.race([painted, new Promise((resolve) => setTimeout(resolve, 100))]);
            pycmd("noteLoaded");
        }})"##,
        mathjax = config_json(api, "renderMathjax", json!(true)),
        meta = json!({"id": note.notetype_id, "modTime": nt["mod"]}),
        templates = Value::Array(templates),
        css = nt["css"],
        fields = Value::Array(fields),
        collapsed = per_field("collapsed", json!(false)),
        cloze_fields = json!(cloze_fields),
        plain = per_field("plainText", json!(false)),
        descriptions = per_field("description", json!("")),
        fonts = Value::Array(fonts),
        focus = json!(focus_to),
        nid = session.page_id(),
        tags = json!(note.tags),
        shrink = config_json(api, "shrinkEditorImages", json!(true)),
        close_tags = config_json(api, "closeHTMLTags", json!(true)),
        dupes = duplicate_display_js(api, note.fields.len(), check.state()),
    );
    Ok(js)
}

/// `_update_duplicate_display`.
fn duplicate_display_js(
    api: &Api,
    field_count: usize,
    state: anki_proto::notes::note_fields_check_response::State,
) -> String {
    use anki_proto::notes::note_fields_check_response::State;
    let mut cols = vec![""; field_count];
    let tr = crate::latex::backend_tr(api);
    let hint = match state {
        State::Duplicate => {
            if let Some(first) = cols.first_mut() {
                *first = "dupe";
            }
            String::new()
        }
        State::NotetypeNotCloze => tr.adding_cloze_outside_cloze_notetype().into_owned(),
        State::FieldNotCloze => tr.adding_cloze_outside_cloze_field().into_owned(),
        _ => String::new(),
    };
    format!(
        "setBackgrounds({}); setClozeHint({});",
        json!(cols),
        json!(hint)
    )
}

/// `_check_and_update_duplicate_display_async`, for the note shown.
pub fn check_duplicates_js(shell: &Shell, editors: &Editors, label: &str) -> Option<String> {
    let note = editors.note(label)?;
    let check: NoteFieldsCheckResponse = shell.api.call("note_fields_check", &note).ok()?;
    Some(format!(
        "require(\"anki/ui\").loaded.then(() => {{ {} }});",
        duplicate_display_js(&shell.api, note.fields.len(), check.state())
    ))
}

/// `editor.munge_html`: `munge_html`, `remove_null_bytes`, then
/// `reverse_url_quoting` (undoing `encode_iri_paths` from loading).
fn munge_html(api: &Api, txt: &str) -> String {
    if txt == "<br>" || txt == "<div><br></div>" {
        return String::new();
    }
    let txt = txt.replace('\0', "");
    api.call::<generic::String>("decode_iri_paths", &generic::String { val: txt.clone() })
        .map(|s| s.val)
        .unwrap_or(txt)
}

/// The note in `label` if the page's save is for it (`page_id` is what
/// the page sent). For Edit Current, the note as currently stored: saves
/// start from it, so changes made elsewhere while the editor is open
/// (AnkiConnect, say) aren't reverted, unless its notetype changed, when
/// the page's fields no longer match. For Add, the unsaved note.
fn current_note(shell: &Shell, editors: &Editors, label: &str, page_id: i64) -> Option<Note> {
    let session = editors.session(label)?;
    if session.page_id() != page_id {
        tracing::debug!("ignored late save");
        return None;
    }
    match session.mode {
        Mode::Add => Some(session.note),
        Mode::EditCurrent => shell
            .api
            .call::<Note>("get_note", &NoteId { nid: page_id })
            .ok()
            .filter(|n| n.notetype_id == session.note.notetype_id),
    }
}

/// `key:<ord>:<nid>:<html>` and `blur:...`: save one field of the note.
/// Returns whether it was saved.
pub fn save_field(shell: &Shell, editors: &Editors, label: &str, args: &str, blur: bool) -> bool {
    let mut parts = args.splitn(3, ':');
    let (Some(ord), Some(nid), Some(txt)) = (parts.next(), parts.next(), parts.next()) else {
        tracing::warn!(args, "malformed field save");
        return false;
    };
    let (Ok(ord), nid) = (ord.parse::<u32>(), nid.parse::<i64>().unwrap_or(0)) else {
        return false;
    };
    let Some(mut note) = current_note(shell, editors, label, nid) else {
        return false;
    };
    let Some(field) = note.fields.get_mut(ord as usize) else {
        tracing::debug!("ignored late save after notetype change");
        return false;
    };
    *field = munge_html(&shell.api, txt);
    save_note(shell, editors, label, note);
    let label = label.to_string();
    if blur {
        editors.update(&label, |s| s.current_field = None);
        editors.emit(shell, EditorEvent::FieldUnfocused { label, ord });
    } else {
        editors.emit(shell, EditorEvent::TypingTimer { label });
    }
    true
}

/// `saveFields:<nid>:<json list>`: all fields at once.
pub fn save_fields(shell: &Shell, editors: &Editors, label: &str, args: &str) -> bool {
    let Some((nid, fields)) = args.split_once(':') else {
        return false;
    };
    let (Ok(nid), Ok(fields)) = (
        nid.parse::<i64>(),
        serde_json::from_str::<Vec<String>>(fields),
    ) else {
        tracing::warn!("ignored malformed field batch");
        return false;
    };
    let Some(mut note) = current_note(shell, editors, label, nid) else {
        return false;
    };
    if fields.len() != note.fields.len() {
        return false;
    }
    note.fields = fields.iter().map(|f| munge_html(&shell.api, f)).collect();
    save_note(shell, editors, label, note);
    editors.emit(
        shell,
        EditorEvent::TypingTimer {
            label: label.into(),
        },
    );
    true
}

/// `saveTags:<json list>`, for the note shown.
pub fn save_tags(shell: &Shell, editors: &Editors, label: &str, args: &str) {
    let Ok(tags) = serde_json::from_str::<Vec<String>>(args) else {
        tracing::warn!("ignored malformed tags");
        return;
    };
    let Some(page_id) = editors.session(label).map(|s| s.page_id()) else {
        return;
    };
    let Some(mut note) = current_note(shell, editors, label, page_id) else {
        return;
    };
    note.tags = tags;
    save_note(shell, editors, label, note);
    editors.emit(
        shell,
        EditorEvent::TagsUpdated {
            label: label.into(),
        },
    );
}

/// `focus:<ord>`.
pub fn focus_field(shell: &Shell, editors: &Editors, label: &str, args: &str) {
    let Ok(ord) = args.parse::<u32>() else {
        return;
    };
    editors.update(label, |s| {
        s.current_field = Some(ord);
        s.last_field_index = Some(ord);
    });
    editors.emit(
        shell,
        EditorEvent::FieldFocused {
            label: label.into(),
            ord,
        },
    );
}

/// `editorState:<new>:<old>`.
pub fn editor_state(shell: &Shell, editors: &Editors, label: &str, args: &str) {
    let mut parts = args.split(':').map(|s| s.parse::<i32>().unwrap_or(0));
    let (new, old) = (parts.next().unwrap_or(0), parts.next().unwrap_or(0));
    editors.update(label, |s| s.state = new);
    editors.emit(
        shell,
        EditorEvent::StateChanged {
            label: label.into(),
            new,
            old,
        },
    );
}

/// Store `note` as the session's note; in Edit Current, save it to the
/// collection first.
fn save_note(shell: &Shell, editors: &Editors, label: &str, note: Note) {
    if editors.session(label).map(|s| s.mode) == Some(Mode::Add) {
        editors.set(label, note);
        return;
    }
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

/// `toggleSticky:<ord>` and `toggleStickyAll`: flip fields' sticky flag in
/// the notetype. Returns what the page shows (the new flag, or all flags).
pub fn toggle_sticky(shell: &Shell, editors: &Editors, label: &str, ord: Option<usize>) -> Value {
    let Some(note) = editors.note(label) else {
        return Value::Null;
    };
    let Ok(mut nt) = notetype_json(&shell.api, note.notetype_id) else {
        return Value::Null;
    };
    let Some(flds) = nt["flds"].as_array_mut() else {
        return Value::Null;
    };
    let sticky = |f: &Value| f["sticky"].as_bool().unwrap_or(false);
    let result = match ord {
        Some(ord) => {
            let Some(f) = flds.get_mut(ord) else {
                return Value::Null;
            };
            let new = !sticky(f);
            f["sticky"] = json!(new);
            json!(new)
        }
        None => {
            let any = flds.iter().any(sticky);
            for f in flds.iter_mut() {
                if !any || sticky(f) {
                    f["sticky"] = json!(!sticky(f));
                }
            }
            json!(flds.iter().map(sticky).collect::<Vec<_>>())
        }
    };
    let saved: Result<anki_proto::collection::OpChanges, _> = shell.api.call(
        "update_notetype_legacy",
        &UpdateNotetypeLegacyRequest {
            json: nt.to_string().into_bytes(),
            skip_checks: false,
        },
    );
    match saved {
        Ok(_) => result,
        Err(err) => {
            tracing::error!(%err, "saving sticky fields failed");
            Value::Null
        }
    }
}

/// `toggleMathjax`, `toggleShrinkImages`, `toggleCloseHTMLTags`: flip the
/// collection setting. Returns the config key.
pub fn toggle_config(shell: &Shell, cmd: &str) -> Option<&'static str> {
    // as editor.py: MathJax's default here is off, the others' on
    let (key, default) = match cmd {
        "toggleMathjax" => ("renderMathjax", false),
        "toggleShrinkImages" => ("shrinkEditorImages", true),
        "toggleCloseHTMLTags" => ("closeHTMLTags", true),
        _ => return None,
    };
    let current = config_json(&shell.api, key, json!(default))
        .as_bool()
        .unwrap_or(default);
    set_config_json(&shell.api, key, json!(!current));
    Some(key)
}

// Adding
//////////////////////////////////////////////////////////////////////////

/// The notetype and deck a new Add window starts with
/// (`col.defaults_for_adding`).
pub fn defaults_for_adding(api: &Api, review_card_deck: i64) -> Result<(i64, i64), ApiError> {
    let d: anki_proto::notes::DeckAndNotetype = api.call(
        "defaults_for_adding",
        &DefaultsForAddingRequest {
            home_deck_of_current_review_card: review_card_deck,
        },
    )?;
    Ok((d.notetype_id, d.deck_id))
}

pub fn new_note(api: &Api, ntid: i64) -> Result<Note, ApiError> {
    api.call("new_note", &NotetypeId { ntid })
}

/// The deck last used with the notetype, if any (`default_deck_for_notetype`).
pub fn default_deck_for_notetype(api: &Api, ntid: i64) -> Option<i64> {
    api.call::<anki_proto::decks::DeckId>("default_deck_for_notetype", &NotetypeId { ntid })
        .ok()
        .map(|d| d.did)
        .filter(|&did| did != 0)
}

/// A new note for `notetype`, starting from the sticky fields and tags of
/// `old` (`AddCards._load_new_note`).
pub fn note_with_sticky_fields(new: Note, sticky: &[bool], old: Option<&Note>) -> Note {
    let Some(old) = old else {
        return new;
    };
    let mut note = new;
    for n in 0..note.fields.len().min(old.fields.len()) {
        if sticky.get(n).copied().unwrap_or(false) {
            note.fields[n] = old.fields[n].clone();
        }
    }
    note.tags = old.tags.clone();
    note
}

/// The note shown after a notetype change: identically named non-empty
/// fields carry over, then the remaining non-empty fields in order, and the
/// tags (`AddCards.on_notetype_change`).
pub fn note_for_new_notetype(
    old: &Note,
    old_names: &[String],
    mut new: Note,
    new_names: &[String],
) -> Note {
    let mut copied = std::collections::HashSet::new();
    for (i, name) in new_names.iter().enumerate() {
        if let Some(j) = old_names.iter().position(|n| n == name) {
            if !old.fields[j].is_empty() {
                new.fields[i] = old.fields[j].clone();
                copied.insert(name.clone());
            }
        }
    }
    let mut new_idx = 0;
    for (old_idx, old_name) in old_names.iter().enumerate() {
        while new_idx < new_names.len() && copied.contains(&new_names[new_idx]) {
            new_idx += 1;
        }
        if new_idx >= new_names.len() {
            break;
        }
        // Qt compares the old field's name with the copied names; a field
        // copied by name isn't copied again
        if !copied.contains(old_name) && !old.fields[old_idx].is_empty() {
            new.fields[new_idx] = old.fields[old_idx].clone();
            new_idx += 1;
        }
    }
    new.tags = old.tags.clone();
    new
}

pub fn field_names(nt: &Value) -> Vec<String> {
    nt["flds"]
        .as_array()
        .map(|f| {
            f.iter()
                .map(|f| f["name"].as_str().unwrap_or_default().to_string())
                .collect()
        })
        .unwrap_or_default()
}

fn sticky_flags(nt: &Value) -> Vec<bool> {
    nt["flds"]
        .as_array()
        .map(|f| f.iter().map(|f| f["sticky"] == json!(true)).collect())
        .unwrap_or_default()
}

/// The Add window's note for `ntid`, keeping what can be kept of `old`.
pub fn switch_notetype(api: &Api, old: &Note, ntid: i64) -> Result<Note, ApiError> {
    let new = new_note(api, ntid)?;
    let old_nt = notetype_json(api, old.notetype_id)?;
    let new_nt = notetype_json(api, ntid)?;
    Ok(note_for_new_notetype(
        old,
        &field_names(&old_nt),
        new,
        &field_names(&new_nt),
    ))
}

/// After adding `added`: the next note, with its sticky fields.
pub fn next_note(api: &Api, added: &Note) -> Result<Note, ApiError> {
    let nt = notetype_json(api, added.notetype_id)?;
    let new = new_note(api, added.notetype_id)?;
    Ok(note_with_sticky_fields(
        new,
        &sticky_flags(&nt),
        Some(added),
    ))
}

/// `Editor.fieldsAreBlank`: whether closing loses nothing. Sticky fields
/// carry over between notes, so they don't count.
pub fn fields_are_blank(note: &Note, sticky: &[bool]) -> bool {
    note.fields.iter().enumerate().all(|(i, f)| {
        sticky.get(i).copied().unwrap_or(false) || f.replace("<br>", "").trim().is_empty()
    })
}

pub fn session_fields_are_blank(api: &Api, session: &Session) -> bool {
    let sticky = notetype_json(api, session.note.notetype_id)
        .map(|nt| sticky_flags(&nt))
        .unwrap_or_default();
    fields_are_blank(&session.note, &sticky)
}

/// What stops a note being added (`AddCards._note_can_be_added`).
#[derive(Debug, Clone, PartialEq)]
pub enum AddCheck {
    Ok,
    /// Shown as a warning; the note isn't added.
    Problem(String),
    /// A cloze notetype without a cloze: add only if the user confirms.
    ConfirmNoCloze(String),
}

pub fn add_check(api: &Api, note: &Note) -> AddCheck {
    use anki_proto::notes::note_fields_check_response::State;
    let tr = crate::latex::backend_tr(api);
    let Ok(check) = api.call::<NoteFieldsCheckResponse>("note_fields_check", note) else {
        return AddCheck::Problem("checking the note failed".into());
    };
    let io = notetype_json(api, note.notetype_id).is_ok_and(|nt| is_image_occlusion(&nt));
    match check.state() {
        State::Empty if io => AddCheck::Problem(tr.notetypes_no_occlusion_created2().into()),
        State::Empty => AddCheck::Problem(tr.adding_the_first_field_is_empty().into()),
        State::MissingCloze => {
            AddCheck::ConfirmNoCloze(tr.adding_you_have_a_cloze_deletion_note().into())
        }
        State::NotetypeNotCloze => {
            AddCheck::Problem(tr.adding_cloze_outside_cloze_notetype().into())
        }
        State::FieldNotCloze => AddCheck::Problem(tr.adding_cloze_outside_cloze_field().into()),
        State::Normal | State::Duplicate => AddCheck::Ok,
    }
}

/// Add the Add window's note, if it is still note `generation`, to the
/// collection; the window then shows the next one. Returns the new note's
/// id and how many cards it made, or None if that note was already added
/// (a double click) or replaced.
pub fn add_current_note(
    shell: &Shell,
    editors: &Editors,
    label: &str,
    generation: i64,
) -> Result<Option<(i64, u32)>, ApiError> {
    let Some(session) = editors
        .session(label)
        .filter(|s| s.mode == Mode::Add && s.generation == generation)
    else {
        return Ok(None);
    };
    let added: AddNoteResponse = shell.api.call(
        "add_note",
        &AddNoteRequest {
            note: Some(session.note.clone()),
            deck_id: session.deck_id,
        },
    )?;
    let mut note = session.note.clone();
    note.id = added.note_id;
    let next = next_note(&shell.api, &note)?;
    editors.update(label, |s| {
        s.history.insert(0, note.id);
        s.history.truncate(HISTORY_SIZE);
        s.last_added = Some(note.clone());
        s.replace_note(next);
    });
    editors.emit(
        shell,
        EditorEvent::NoteAdded {
            label: label.into(),
            nid: note.id,
        },
    );
    let cards = added.changes.map(|c| c.count).unwrap_or(0);
    Ok(Some((note.id, cards)))
}

/// The History button's entries: (note id, "Edit: text..."), or
/// (id, None) for a note deleted since (`AddCards.onHistory`).
pub fn history_entries(api: &Api, history: &[i64]) -> Vec<(i64, Option<String>)> {
    let tr = crate::latex::backend_tr(api);
    history
        .iter()
        .map(|&nid| {
            let line = api
                .call::<Note>("get_note", &NoteId { nid })
                .ok()
                .map(|note| {
                    let text = api
                        .call::<generic::String>(
                            "html_to_text_line",
                            &anki_proto::card_rendering::HtmlToTextLineRequest {
                                text: note.fields.join(", "),
                                preserve_media_filenames: false,
                            },
                        )
                        .map(|s| s.val)
                        .unwrap_or_default();
                    let text = if text.chars().count() > 30 {
                        format!("{}...", text.chars().take(30).collect::<String>())
                    } else {
                        text
                    };
                    tr.adding_edit(text).into_owned()
                });
            (nid, line)
        })
        .collect()
}

/// Notes with the same first field as the one shown (the editor's
/// "Show duplicates" link, which opens the browser in Qt): (id, text).
pub fn duplicates(api: &Api, note: &Note) -> Vec<(i64, String)> {
    use anki_proto::search::search_node;
    use anki_proto::search::SearchNode;
    let node = SearchNode {
        filter: Some(search_node::Filter::Dupe(search_node::Dupe {
            notetype_id: note.notetype_id,
            first_field: note.fields.first().cloned().unwrap_or_default(),
        })),
    };
    let Ok(search) = api.call::<generic::String>("build_search_string", &node) else {
        return vec![];
    };
    let Ok(found) = api.call::<anki_proto::search::SearchResponse>(
        "search_notes",
        &anki_proto::search::SearchRequest {
            search: search.val,
            order: None,
        },
    ) else {
        return vec![];
    };
    found
        .ids
        .into_iter()
        .filter(|&nid| nid != note.id)
        .filter_map(|nid| {
            let n: Note = api.call("get_note", &NoteId { nid }).ok()?;
            let text: generic::String = api
                .call(
                    "html_to_text_line",
                    &anki_proto::card_rendering::HtmlToTextLineRequest {
                        text: n.fields.join(" / "),
                        preserve_media_filenames: false,
                    },
                )
                .ok()?;
            Some((nid, text.val))
        })
        .collect()
}

// Media and image occlusion
//////////////////////////////////////////////////////////////////////////

/// `resolveMedia` for the attach and record buttons, which wait for it.
pub fn resolve_media_js(html: &str) -> String {
    format!(
        "require(\"anki/TemplateButtons\").resolveMedia({});",
        json!(html)
    )
}

/// The image occlusion mask editor for `image` (an absolute path): a new
/// note in Add (`setup_mask_editor_for_new_note`), a replaced image in
/// Edit Current. `image_html` is the image added to the media folder.
pub fn mask_editor_js(mode: Mode, nid: i64, image: &str, image_html: &str) -> String {
    let setup = |options: Value| {
        format!("require(\"anki/ui\").loaded.then(() => setupMaskEditor({options}));")
    };
    match mode {
        Mode::Add => setup(json!({
            "mode": {"kind": "add", "imagePath": image, "notetypeId": 0},
            "html": image_html,
        })),
        Mode::EditCurrent => format!(
            "resetIOImage({}); setImageField({}); {}",
            json!(image),
            json!(image_html),
            setup(json!({"mode": {"kind": "edit", "noteId": nid}}))
        ),
    }
}

// LaTeX preview
//////////////////////////////////////////////////////////////////////////

/// `renderLatexPreview:<json>`: a cached result right away, or None, in
/// which case the caller renders with [render_latex_preview] off the
/// bridge (renders take a while, and the bridge is ordered) and sends the
/// result with `receiveLatexPreviewResult`.
pub fn cached_latex_preview(editors: &Editors, key: &str, request_id: &str) -> Option<Value> {
    let mut cache = editors.latex.lock().unwrap();
    let pos = cache.iter().position(|(k, _)| k == key)?;
    let entry = cache.remove(pos)?;
    let mut result = entry.1.clone();
    cache.push_back(entry);
    result["requestId"] = json!(request_id);
    Some(result)
}

pub fn remember_latex_preview(editors: &Editors, key: String, result: Value) {
    let mut cache = editors.latex.lock().unwrap();
    cache.retain(|(k, _)| k != &key);
    cache.push_back((key, result));
    while cache.len() > LATEX_PREVIEW_CACHE_SIZE {
        cache.pop_front();
    }
}

/// A preview request, parsed: (request id, kind, html, cache key, saved
/// fields). The key includes the notetype's LaTeX settings and the saved
/// fields, which decide whether the image goes to the media folder.
pub struct LatexRequest {
    pub request_id: String,
    pub kind: String,
    pub html: String,
    pub key: String,
    pub config: crate::latex::LatexConfig,
    pub saved_fields: Vec<String>,
}

pub fn latex_request(
    shell: &Shell,
    editors: &Editors,
    label: &str,
    args: &str,
) -> Option<LatexRequest> {
    let request: Value = serde_json::from_str(args).ok()?;
    let session = editors.session(label)?;
    let config = crate::latex::latex_config(&shell.api, session.note.notetype_id).ok()?;
    let saved_fields = match session.mode {
        Mode::Add => vec![],
        Mode::EditCurrent => session.note.fields.clone(),
    };
    let request_id = match &request["requestId"] {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    let kind = request["kind"].as_str().unwrap_or("inline").to_string();
    let html = request["html"].as_str().unwrap_or_default().to_string();
    let key = json!({
        "kind": kind,
        "html": html,
        "latexPre": config.latex_pre,
        "latexPost": config.latex_post,
        "latexsvg": config.svg,
        "savedFields": saved_fields.join("\x1f"),
    })
    .to_string();
    Some(LatexRequest {
        request_id,
        kind,
        html,
        key,
        config,
        saved_fields,
    })
}

/// Render a preview: the bridge result `_latex_preview_bridge_result`
/// builds.
pub fn render_latex_preview(shell: &Shell, request: &LatexRequest) -> Value {
    let preview = crate::latex::render_preview(
        &shell.api,
        shell.media_folder().as_deref(),
        &request.config,
        &request.kind,
        &request.html,
        &request.saved_fields,
    );
    match preview {
        Ok(p) => json!({
            "requestId": request.request_id,
            "ok": true,
            "src": p.src,
            "alt": p.alt,
            "svg": p.svg,
        }),
        Err(error) => json!({
            "requestId": request.request_id,
            "ok": false,
            "errorText": error,
        }),
    }
}

// Formatting buttons
//////////////////////////////////////////////////////////////////////////

/// The Mini Format Pack add-on's actions this user had enabled (its
/// default config, unchanged): (command, tooltip, Qt hotkey, JS). Qt's
/// "Ctrl" is Cmd on macOS. `abbr` asks for its title first.
pub const FORMAT_ACTIONS: &[(&str, &str, &str, &str)] = &[
    (
        "strikeThrough",
        "Apply strikethrough",
        "Alt+Shift+5",
        "setFormat('strikeThrough')",
    ),
    ("abbr", "Apply <abbr> tag", "Cmd+Shift+Alt+A", ""),
    (
        "formatBlockPre",
        "Insert code block",
        "Cmd+.",
        "setFormat('formatBlock', 'pre')",
    ),
    (
        "formatInlineCode",
        "Insert inline code",
        "Cmd+,",
        "wrap('<code>', '</code>')",
    ),
    (
        "insertHorizontalRule",
        "Insert horizontal line",
        "Cmd+Shift+Alt+_",
        "setFormat('insertHorizontalRule')",
    ),
    (
        "insertUnorderedList",
        "Toggle unordered list",
        "Cmd+[",
        "setFormat('insertUnorderedList')",
    ),
    (
        "insertOrderedList",
        "Toggle ordered list",
        "Cmd+]",
        "setFormat('insertOrderedList')",
    ),
    ("indent", "Indent", "Cmd+Shift+]", "setFormat('indent')"),
    ("outdent", "Outdent", "Cmd+Shift+[", "setFormat('outdent')"),
    (
        "justifyCenter",
        "Center text",
        "Cmd+Shift+Alt+S",
        "setFormat('justifyCenter')",
    ),
    (
        "justifyLeft",
        "Align text to the left",
        "Cmd+Shift+Alt+L",
        "setFormat('justifyLeft')",
    ),
    (
        "justifyRight",
        "Align text to the right",
        "Cmd+Shift+Alt+R",
        "setFormat('justifyRight')",
    ),
    (
        "justifyFull",
        "Justify text",
        "Cmd+Shift+Alt+B",
        "setFormat('justifyFull')",
    ),
];

/// The JS for a format command (`format:<name>`), if it's one; `abbr`
/// takes the title the user typed.
pub fn format_js(name: &str, abbr_title: Option<&str>) -> Option<String> {
    if name == "abbr" {
        // the add-on's html.escape
        let title = crate::latex::html_escape(abbr_title.unwrap_or_default());
        return Some(format!(
            "wrap({}, \"</abbr>\");",
            json!(format!("<abbr title='{title}'>"))
        ));
    }
    FORMAT_ACTIONS
        .iter()
        .find(|a| a.0 == name)
        .map(|a| format!("{};", a.3))
}

#[cfg(test)]
mod test {
    use super::*;

    fn note(fields: &[&str], tags: &[&str]) -> Note {
        Note {
            fields: fields.iter().map(|s| s.to_string()).collect(),
            tags: tags.iter().map(|s| s.to_string()).collect(),
            ..Default::default()
        }
    }

    fn names(n: &[&str]) -> Vec<String> {
        n.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn sticky_fields_and_tags_carry_over_to_the_next_note() {
        let old = note(&["front", "back", "extra"], &["t1"]);
        let new = note_with_sticky_fields(note(&["", "", ""], &[]), &[false, true], Some(&old));
        assert_eq!(new.fields, vec!["", "back", ""]);
        assert_eq!(new.tags, vec!["t1"]);
        let fresh = note_with_sticky_fields(note(&["", ""], &[]), &[true, true], None);
        assert_eq!(fresh.fields, vec!["", ""]);
    }

    #[test]
    fn notetype_change_copies_fields_by_name_then_in_order() {
        let old = note(&["q", "a", "", "src"], &["t"]);
        let old_names = names(&["Front", "Back", "Empty", "Source"]);
        let new = note_for_new_notetype(
            &old,
            &old_names,
            note(&["", "", ""], &[]),
            &names(&["Text", "Source", "Extra"]),
        );
        // Source by name; then Front and Back fill the remaining fields
        assert_eq!(new.fields, vec!["q", "src", "a"]);
        assert_eq!(new.tags, vec!["t"]);
    }

    #[test]
    fn blank_check_ignores_sticky_fields_and_empty_markup() {
        assert!(fields_are_blank(&note(&["<br>", " "], &[]), &[]));
        assert!(fields_are_blank(&note(&["", "kept"], &[]), &[false, true]));
        assert!(!fields_are_blank(&note(&["x", ""], &[]), &[false, true]));
    }

    #[test]
    fn format_commands_map_to_page_calls() {
        assert_eq!(
            format_js("strikeThrough", None).as_deref(),
            Some("setFormat('strikeThrough');")
        );
        assert_eq!(
            format_js("formatInlineCode", None).as_deref(),
            Some("wrap('<code>', '</code>');")
        );
        // the title is escaped for the attribute, and the JS string quoted
        assert_eq!(
            format_js("abbr", Some("it's <x>")).as_deref(),
            Some(r#"wrap("<abbr title='it&#x27;s &lt;x&gt;'>", "</abbr>");"#)
        );
        assert_eq!(format_js("nope", None), None);
        assert_eq!(FORMAT_ACTIONS.len(), 13);
    }

    #[test]
    fn latex_cache_keeps_the_most_recent() {
        let editors = Editors::default();
        for i in 0..LATEX_PREVIEW_CACHE_SIZE + 2 {
            remember_latex_preview(&editors, format!("k{i}"), json!({"ok": true, "n": i}));
        }
        assert!(cached_latex_preview(&editors, "k0", "1").is_none());
        let hit = cached_latex_preview(&editors, "k5", "9").unwrap();
        assert_eq!(hit["requestId"], json!("9"));
        assert_eq!(hit["n"], json!(5));
    }
}

#[cfg(test)]
mod store_test {
    use anki_proto::notes::AddNoteRequest;
    use anki_proto::notes::AddNoteResponse;

    use super::*;
    use crate::api::Api;
    use crate::profile::ProfilePaths;

    fn shell() -> (tempfile::TempDir, Shell) {
        let dir = tempfile::tempdir().unwrap();
        let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), Default::default());
        shell
            .open_profile(ProfilePaths::new(dir.path(), "User 1"))
            .unwrap();
        (dir, shell)
    }

    fn basic(shell: &Shell) -> i64 {
        let ntid: NotetypeId = shell
            .api
            .call(
                "get_notetype_id_by_name",
                &generic::String {
                    val: "Basic".into(),
                },
            )
            .unwrap();
        ntid.ntid
    }

    fn shell_with_note() -> (tempfile::TempDir, Shell, i64) {
        let (dir, shell) = shell();
        let mut note = new_note(&shell.api, basic(&shell)).unwrap();
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

    fn edit_session(shell: &Shell, editors: &Editors, nid: i64) {
        editors.set_session("editor", Session::edit(stored(shell, nid), None));
        load_note_js(shell, editors, "editor", None, &Default::default()).unwrap();
    }

    fn note_count(shell: &Shell) -> i64 {
        shell.api.query("select count() from notes", &[]).unwrap()[0][0]
            .as_i64()
            .unwrap()
    }

    #[test]
    fn field_saves_keep_changes_made_elsewhere() {
        let (_dir, shell, nid) = shell_with_note();
        let editors = Editors::default();
        edit_session(&shell, &editors, nid);
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

        save_field(
            &shell,
            &editors,
            "editor",
            &format!("0:{nid}:new front"),
            false,
        );

        let note = stored(&shell, nid);
        assert_eq!(note.fields, vec!["new front", "changed back"]);
        assert_eq!(note.tags, vec!["added"]);
    }

    #[test]
    fn saved_fields_undo_the_load_time_media_escaping() {
        let (_dir, shell, nid) = shell_with_note();
        let editors = Editors::default();
        edit_session(&shell, &editors, nid);

        save_field(
            &shell,
            &editors,
            "editor",
            &format!("0:{nid}:<img src=\"a%20b.png\">"),
            true,
        );

        assert_eq!(stored(&shell, nid).fields[0], "<img src=\"a b.png\">");
    }

    #[test]
    fn tags_are_saved() {
        let (_dir, shell, nid) = shell_with_note();
        let editors = Editors::default();
        edit_session(&shell, &editors, nid);

        save_tags(&shell, &editors, "editor", r#"["one", "two"]"#);

        assert_eq!(stored(&shell, nid).tags, vec!["one", "two"]);
    }

    #[test]
    fn add_mode_keeps_edits_in_memory_until_added() {
        let (_dir, shell) = shell();
        let editors = Editors::default();
        let ntid = basic(&shell);
        editors.set_session("add", Session::add(new_note(&shell.api, ntid).unwrap(), 1));
        let before = note_count(&shell);
        let first = editors.session("add").unwrap();
        let page = first.page_id();

        assert!(save_field(
            &shell,
            &editors,
            "add",
            &format!("0:{page}:question"),
            false
        ));
        save_tags(&shell, &editors, "add", r#"["tagged"]"#);

        assert_eq!(note_count(&shell), before);
        let (nid, cards) = add_current_note(&shell, &editors, "add", first.generation)
            .unwrap()
            .unwrap();
        assert_eq!(cards, 1);
        let added = stored(&shell, nid);
        assert_eq!(added.fields, vec!["question", ""]);
        assert_eq!(added.tags, vec!["tagged"]);
        let session = editors.session("add").unwrap();
        assert_eq!(session.history, vec![nid]);
        // the next note: empty fields (none are sticky), same tags
        assert_eq!(session.note.id, 0);
        assert_eq!(session.note.fields, vec!["", ""]);
        assert_eq!(session.note.tags, vec!["tagged"]);
        // a late save for the added note doesn't land in the next one
        assert!(!save_field(
            &shell,
            &editors,
            "add",
            &format!("0:{page}:late"),
            false
        ));
        assert_eq!(editors.session("add").unwrap().note.fields, vec!["", ""]);
        // nor does a second add of it (a double click)
        assert_eq!(
            add_current_note(&shell, &editors, "add", first.generation).unwrap(),
            None
        );
        assert_eq!(note_count(&shell), before + 1);
        assert_ne!(editors.session("add").unwrap().page_id(), page);
    }

    #[test]
    fn sticky_toggles_are_saved_in_the_notetype() {
        let (_dir, shell) = shell();
        let editors = Editors::default();
        let ntid = basic(&shell);
        editors.set_session("add", Session::add(new_note(&shell.api, ntid).unwrap(), 1));

        assert_eq!(toggle_sticky(&shell, &editors, "add", Some(1)), json!(true));
        let nt = notetype_json(&shell.api, ntid).unwrap();
        assert_eq!(sticky_flags(&nt), vec![false, true]);
        // with one sticky, "all" turns the sticky ones off
        assert_eq!(
            toggle_sticky(&shell, &editors, "add", None),
            json!([false, false])
        );
        assert_eq!(
            toggle_sticky(&shell, &editors, "add", None),
            json!([true, true])
        );

        // sticky fields carry over after adding
        let session = editors.session("add").unwrap();
        let page = session.page_id();
        save_field(&shell, &editors, "add", &format!("0:{page}:q"), false);
        save_field(&shell, &editors, "add", &format!("1:{page}:kept"), false);
        add_current_note(&shell, &editors, "add", session.generation).unwrap();
        assert_eq!(
            editors.session("add").unwrap().note.fields,
            vec!["q", "kept"]
        );
    }

    #[test]
    fn add_checks_report_why_a_note_cant_be_added() {
        let (_dir, shell) = shell();
        let empty = new_note(&shell.api, basic(&shell)).unwrap();
        assert!(matches!(
            add_check(&shell.api, &empty),
            AddCheck::Problem(msg) if msg.contains("empty")
        ));
        let mut cloze_outside = empty.clone();
        cloze_outside.fields[0] = "{{c1::x}}".into();
        assert!(matches!(
            add_check(&shell.api, &cloze_outside),
            AddCheck::Problem(_)
        ));
        let mut fine = empty;
        fine.fields[0] = "x".into();
        assert_eq!(add_check(&shell.api, &fine), AddCheck::Ok);
    }

    #[test]
    fn duplicates_are_found_by_first_field() {
        let (_dir, shell, nid) = shell_with_note();
        let mut dupe = stored(&shell, nid);
        dupe.id = 0;
        assert_eq!(duplicates(&shell.api, &dupe).len(), 1);
        assert_eq!(duplicates(&shell.api, &dupe)[0].0, nid);
        let mut other = dupe;
        other.fields[0] = "unique".into();
        assert!(duplicates(&shell.api, &other).is_empty());
    }

    #[test]
    fn history_lists_added_notes() {
        let (_dir, shell, nid) = shell_with_note();
        let entries = history_entries(&shell.api, &[nid, 12345]);
        assert_eq!(entries[0].0, nid);
        assert!(entries[0].1.as_deref().unwrap().contains("front, back"));
        assert_eq!(entries[1].1, None);
    }

    #[test]
    fn events_reach_listeners() {
        let (_dir, shell, nid) = shell_with_note();
        let editors = Editors::default();
        let seen = std::sync::Arc::new(Mutex::new(vec![]));
        let s = seen.clone();
        editors.listen(Box::new(move |_, e| s.lock().unwrap().push(e.clone())));
        edit_session(&shell, &editors, nid);

        focus_field(&shell, &editors, "editor", "1");
        save_field(&shell, &editors, "editor", &format!("1:{nid}:x"), true);

        assert_eq!(
            *seen.lock().unwrap(),
            vec![
                EditorEvent::FieldFocused {
                    label: "editor".into(),
                    ord: 1
                },
                EditorEvent::FieldUnfocused {
                    label: "editor".into(),
                    ord: 1
                },
            ]
        );
    }
}
