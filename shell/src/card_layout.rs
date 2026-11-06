// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The card layout screen (`aqt/clayout.py`): the `card-layout` route edits
//! a note's card templates, and previews them in a frame.
//!
//! The preview shows card templates, whose JS mustn't reach the editing
//! page's API or bridge (it can save the notetype). So the frame is served
//! sandboxed, an opaque origin: its requests carry no `anki://` origin and
//! are refused, and it can't reach into the page. The page posts it each
//! rendered side; rendering (the backend's uncommitted card, then LaTeX,
//! sounds and type boxes, as the reviewer) happens here.

use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::Mutex;

use anki_proto::card_rendering::CompareAnswerRequest;
use anki_proto::card_rendering::ExtractAvTagsRequest;
use anki_proto::card_rendering::ExtractAvTagsResponse;
use anki_proto::card_rendering::RenderCardResponse;
use anki_proto::card_rendering::RenderUncommittedCardRequest;
use anki_proto::notes::Note;
use anki_proto::notetypes::notetype::Template;
use anyhow::Context;
use anyhow::Result;
use prost::Message;
use serde::Deserialize;
use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use crate::api::Api;
use crate::app::Content;
use crate::app::Ui;
use crate::card_text;
use crate::pages;
use crate::pages::WebviewKind;

pub const LABEL: &str = "card-layout";
/// The preview frame's page (a `legacyPageData` id of this window).
const PREVIEW: &str = "card-layout-preview";

/// What the window was opened on: a copy of the editor's note (in Add mode
/// it isn't in the collection), the card shown first, and the editor.
#[derive(Clone)]
struct Session {
    note: Note,
    ord: u32,
    editor: String,
}

static SESSION: Mutex<Option<Session>> = Mutex::new(None);
/// Set once a close was asked of the page, so a second close goes through.
static CLOSE_ASKED: AtomicBool = AtomicBool::new(false);

/// Open on editor `label`'s note, once its pending edits are saved
/// (`Editor.onCardLayout`).
pub fn open_from_editor(ui: &Arc<Ui>, label: &str) {
    let Some(session) = ui.editors.session(label) else {
        return;
    };
    // Edit Current from the reviewer shows the reviewed card's template
    let ord = session
        .card_id
        .and_then(|cid| {
            ui.shell
                .api
                .call::<anki_proto::cards::Card>("get_card", &anki_proto::cards::CardId { cid })
                .ok()
        })
        .filter(|card| card.note_id == session.note.id)
        .map_or(0, |card| card.template_idx);
    open(ui, session.note, ord, label);
}

pub fn open(ui: &Ui, note: Note, ord: u32, editor: &str) {
    *SESSION.lock().unwrap() = Some(Session {
        note,
        ord,
        editor: editor.to_string(),
    });
    CLOSE_ASKED.store(false, Ordering::SeqCst);
    ui.shell.pages.set(
        PREVIEW,
        WebviewKind::CardLayout,
        Some(preview_html(&ui.shell.web)),
    );
    if let Err(err) = ui.open_window(
        LABEL,
        &ui.main.tr.editing_cards(),
        WebviewKind::CardLayoutEditor,
        Content::Route(LABEL.into()),
    ) {
        tracing::error!(%err, "opening card layout failed");
    }
}

/// The preview frame: the reviewer's page, drawing what the page posts
/// (`_showAnswer`, as clayout.py's preview). It has no bridge: it's an
/// opaque origin.
fn preview_html(web: &std::path::Path) -> String {
    let html = pages::reviewer_html(web);
    html.replacen(
        "</body>",
        r#"<script>
    window.pycmd = window.bridgeCommand = () => false;
    window.addEventListener("message", (event) => {
        if (event.source !== window.parent || !event.data || !("html" in event.data)) {
            return;
        }
        const { html, bodyClass, night } = event.data;
        document.documentElement.classList.toggle("night-mode", !!night);
        document.documentElement.dataset.bsTheme = night ? "dark" : "light";
        _showAnswer(html, bodyClass);
    });
    // what's shown, for the page (its tests can't look inside the frame)
    const qa = document.getElementById("qa");
    const report = () => window.parent.postMessage({
        shown: qa.innerText,
        night: document.documentElement.classList.contains("night-mode"),
    }, "*");
    new MutationObserver(report).observe(qa, { childList: true, subtree: true, characterData: true });
    window.parent.postMessage({ previewReady: true }, "*");
</script></body>"#,
        1,
    )
}

/// The window's close button: the page asks first if there are changes.
pub fn close_requested(ui: &Ui) -> bool {
    if CLOSE_ASKED.swap(true, Ordering::SeqCst) {
        return false;
    }
    ui.eval(
        LABEL,
        "window.shellCloseRequested ? shellCloseRequested() : pycmd('clayoutClose');".into(),
    );
    true
}

fn close(ui: &Ui) {
    if let Some(window) = ui.app.get_webview_window(LABEL) {
        let _ = window.destroy();
    }
}

pub fn register(ui: &Arc<Ui>) {
    let reg = |name: &'static str, f: fn(&Arc<Ui>, &str) -> Value| {
        let u = ui.clone();
        ui.shell.bridge.register(name, move |_, from, args| {
            if from != LABEL {
                tracing::warn!(from, name, "rejected a card layout command");
                return Value::Null;
            }
            f(&u, args)
        });
    };
    reg("clayoutInit", |ui, _| {
        init(ui).unwrap_or_else(|e| json!({ "error": format!("{e:#}") }))
    });
    reg("clayoutRender", |ui, args| {
        render(ui, args).unwrap_or_else(|e| json!({ "error": format!("{e:#}") }))
    });
    // cards a template makes (removing it deletes them)
    reg("clayoutCardCount", |ui, ord| {
        let ntid = SESSION.lock().unwrap().as_ref().map(|s| s.note.notetype_id);
        let (Some(ntid), Ok(ord)) = (ntid, ord.parse::<u32>()) else {
            return Value::Null;
        };
        ui.shell
            .api
            .query(
                &format!(
                    "select count() from cards, notes where cards.nid = notes.id \
                     and notes.mid = {ntid} and cards.ord = {ord}"
                ),
                &[],
            )
            .ok()
            .and_then(|rows| rows.first().and_then(|r| r[0].as_i64()))
            .map_or(Value::Null, Value::from)
    });
    // notes of the notetype (a new template makes a card for each)
    reg("clayoutNoteCount", |ui, _| {
        let ntid = SESSION.lock().unwrap().as_ref().map(|s| s.note.notetype_id);
        let Some(ntid) = ntid else {
            return Value::Null;
        };
        ui.shell
            .api
            .query(&format!("select count() from notes where mid = {ntid}"), &[])
            .ok()
            .and_then(|rows| rows.first().and_then(|r| r[0].as_i64()))
            .map_or(Value::Null, Value::from)
    });
    // saved (the page called update_notetype): the editor and the reviewer
    // show the new templates
    reg("clayoutSaved", |ui, _| {
        let editor = SESSION.lock().unwrap().take().map(|s| s.editor);
        close(ui);
        if let Some(editor) = editor.filter(|e| ui.editors.session(e).is_some()) {
            crate::editor_window::reload_note_keeping_focus(ui, &editor);
        }
        crate::editor_window::note_changed_elsewhere(ui);
        crate::main_window::refresh(ui);
        crate::menus::refresh(ui);
        crate::main_window::tooltip(ui, &ui.main.tr.card_templates_changes_saved());
        Value::Null
    });
    reg("clayoutClose", |ui, _| {
        SESSION.lock().unwrap().take();
        close(ui);
        Value::Null
    });
    // the page kept the window open (the user kept their changes)
    reg("clayoutStay", |_, _| {
        CLOSE_ASKED.store(false, Ordering::SeqCst);
        Value::Null
    });
}

fn session() -> Result<Session> {
    SESSION
        .lock()
        .unwrap()
        .clone()
        .context("card layout isn't open")
}

fn init(ui: &Ui) -> Result<Value> {
    let s = session()?;
    let api = &ui.shell.api;
    let notetype: anki_proto::notetypes::Notetype = api.call(
        "get_notetype",
        &anki_proto::notetypes::NotetypeId {
            ntid: s.note.notetype_id,
        },
    )?;
    let clozes: anki_proto::notes::ClozeNumbersInNoteResponse =
        api.call("cloze_numbers_in_note", &s.note)?;
    Ok(json!({
        "notetype": data_encoding::BASE64.encode(&notetype.encode_to_vec()),
        "ord": s.ord,
        "clozeNumbers": clozes.numbers,
        "hasEmptyField": s.note.fields.iter().any(|f| f.trim().is_empty()),
        "night": ui.shell.night.get(),
    }))
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct RenderArgs {
    /// The edited template (protobuf, base64).
    template: String,
    /// The edited styling.
    css: String,
    ord: u32,
    fill_empty: bool,
    answer: bool,
    night: bool,
}

fn render(ui: &Ui, args: &str) -> Result<Value> {
    let args: RenderArgs = serde_json::from_str(args)?;
    let template = Template::decode(&*data_encoding::BASE64.decode(args.template.as_bytes())?)?;
    let s = session()?;
    let media = ui.shell.media_folder().context("no collection open")?;
    let html = render_side(&ui.shell.api, &media, &s.note, template, &args)?;
    let night = if args.night {
        " nightMode night_mode"
    } else {
        ""
    };
    Ok(json!({
        "html": html,
        "bodyClass": format!("card card{} isMac{night}", args.ord + 1),
    }))
}

/// `CardLayout._renderPreview` for one side.
fn render_side(
    api: &Api,
    media: &std::path::Path,
    note: &Note,
    template: Template,
    args: &RenderArgs,
) -> Result<String> {
    let out: RenderCardResponse = api.call(
        "render_uncommitted_card",
        &RenderUncommittedCardRequest {
            note: Some(note.clone()),
            card_ord: args.ord,
            template: Some(template),
            fill_empty: args.fill_empty,
            partial_render: false,
        },
    )?;
    let nodes = if args.answer {
        &out.answer_nodes
    } else {
        &out.question_nodes
    };
    let side: ExtractAvTagsResponse = api.call(
        "extract_av_tags",
        &ExtractAvTagsRequest {
            text: card_text::nodes_to_text(nodes),
            question_side: !args.answer,
        },
    )?;
    let latex = crate::latex::latex_config(api, note.notetype_id).unwrap_or_default();
    let (text, errors) = crate::latex::render_latex(api, media, &side.text, &latex);
    let text = text + &errors.join("\n");
    // the edited styling, not the stored one (template.py does the same)
    let text = format!("<style>{}</style>{text}", args.css);
    let text = type_boxes(api, &text, args.answer);
    Ok(card_text::prepare_for_display(api, &text, false)?)
}

/// `CardLayout.maybeTextInput`: a sample type box on the front, a sample
/// comparison on the back; a second box is a warning.
fn type_boxes(api: &Api, text: &str, answer: bool) -> String {
    static TYPE: std::sync::LazyLock<regex::Regex> =
        std::sync::LazyLock::new(|| regex::Regex::new(r"\[\[type:.+?\]\]").unwrap());
    if !text.contains("[[type:") {
        return text.to_string();
    }
    let without_hr = text.replace("<hr id=answer>", "");
    let had_hr = without_hr.len() != text.len();
    let first = if answer {
        let compared = api
            .call::<anki_proto::generic::String>(
                "compare_answer",
                &CompareAnswerRequest {
                    expected: "example".into(),
                    provided: "sample".into(),
                    combining: true,
                },
            )
            .map(|s| s.val)
            .unwrap_or_default();
        if had_hr {
            format!("<hr id=answer>{compared}")
        } else {
            compared
        }
    } else {
        "<center><input id='typeans' type=text value='example' readonly='readonly'></center>"
            .to_string()
    };
    let out = TYPE.replace(&without_hr, regex::NoExpand(&first)).into_owned();
    let warning = format!(
        "<center><b>{}</b></center>",
        crate::latex::html_escape(&crate::latex::backend_tr(api).card_templates_type_boxes_warning())
    );
    TYPE.replace_all(&out, regex::NoExpand(&warning)).into_owned()
}

#[cfg(test)]
mod test {
    use anki_proto::generic::Empty;
    use anki_proto::notetypes::NotetypeNames;

    use super::*;
    use crate::profile::open_collection;
    use crate::profile::ProfilePaths;

    fn collection() -> (tempfile::TempDir, Api, std::path::PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        let api = Api::new(vec!["en".into()]).unwrap();
        open_collection(&api, &paths).unwrap();
        let media = paths.media();
        (dir, api, media)
    }

    fn basic_note(api: &Api) -> Note {
        let names: NotetypeNames = api.call("get_notetype_names", &Empty {}).unwrap();
        let basic = names.entries.iter().find(|e| e.name == "Basic").unwrap();
        let mut note: Note = api
            .call(
                "new_note",
                &anki_proto::notetypes::NotetypeId { ntid: basic.id },
            )
            .unwrap();
        note.fields = vec!["front text".into(), "back text".into()];
        note
    }

    fn args(answer: bool) -> RenderArgs {
        RenderArgs {
            template: String::new(),
            css: ".card { color: red }".into(),
            ord: 0,
            fill_empty: false,
            answer,
            night: false,
        }
    }

    #[test]
    fn previews_render_the_edited_template_and_styling() {
        let (_dir, api, media) = collection();
        // a note not in the collection, as in Add mode
        let note = basic_note(&api);
        let template = Template {
            name: "Card 1".into(),
            config: Some(anki_proto::notetypes::notetype::template::Config {
                q_format: "Q: {{Front}}".into(),
                a_format: "{{FrontSide}}<hr id=answer>A: {{Back}} [[type:Back]]".into(),
                ..Default::default()
            }),
            ..Default::default()
        };
        let q = render_side(&api, &media, &note, template.clone(), &args(false)).unwrap();
        assert_eq!(q, "<style>.card { color: red }</style>Q: front text");
        let a = render_side(&api, &media, &note, template, &args(true)).unwrap();
        assert!(a.starts_with("<style>.card { color: red }</style>"), "{a}");
        assert!(a.contains("A: back text"), "{a}");
        // the sample comparison, after the answer line
        assert!(a.contains("<hr id=answer><code id=typeans>"), "{a}");
        assert!(!a.contains("[[type:"), "{a}");
    }

    #[test]
    fn a_second_type_box_is_a_warning() {
        let (_dir, api, _media) = collection();
        let q = type_boxes(&api, "[[type:Front]] and [[type:Back]]", false);
        assert!(q.starts_with("<center><input id='typeans'"), "{q}");
        assert!(q.ends_with("Only one typing box per card template is supported.</b></center>"), "{q}");
    }

    #[test]
    fn the_preview_frame_draws_what_the_page_posts() {
        let html = preview_html(std::path::Path::new("/nonexistent"));
        assert!(html.contains("js/reviewer.js"));
        // after the reviewer's scripts, so _showAnswer exists
        assert!(html.find("js/reviewer.js").unwrap() < html.find("_showAnswer(html").unwrap());
        assert!(html.contains("event.source !== window.parent"));
    }
}
