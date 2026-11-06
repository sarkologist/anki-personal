// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Small screens that were Qt dialogs, now SvelteKit routes: the filtered
//! deck dialog (`filtered_deck.py`), custom study (`customstudy.py`),
//! Check Media (`mediacheck.py`) and Empty Cards (`emptycards.py`). Each
//! page calls its own backend methods (`WebviewKind::own_backend`); what
//! needs the shell (moving the main window on, LaTeX) goes through the
//! bridge commands here, accepted only from that screen's window.

use std::sync::Arc;

use anyhow::Result;
use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use crate::app::Content;
use crate::app::Ui;
use crate::main_window;
use crate::pages::WebviewKind;

pub const FILTERED_DECK: &str = "filtered-deck";
pub const CUSTOM_STUDY: &str = "custom-study";
pub const MEDIA_CHECK: &str = "media-check";
pub const EMPTY_CARDS: &str = "empty-cards";

fn open(ui: &Ui, label: &str, title: &str, kind: WebviewKind, route: String) {
    // already open: keep what's typed there (`reopen` in Qt)
    if let Some(window) = ui.app.get_webview_window(label) {
        if !ui.background {
            let _ = window.set_focus();
        }
        return;
    }
    if let Err(err) = ui.open_window(label, title, kind, Content::Route(route)) {
        tracing::error!(%err, label, "opening a window failed");
    }
}

/// The filtered deck dialog for deck `did`, or for a new deck (0), as
/// `FilteredDeckConfigDialog`.
pub fn open_filtered_deck(ui: &Ui, did: i64) {
    let title = if did == 0 {
        ui.main.tr.qt_misc_create_filtered_deck().replace("...", "")
    } else {
        ui.main.tr.actions_options().into_owned()
    };
    open(
        ui,
        FILTERED_DECK,
        &title,
        WebviewKind::FilteredDeck,
        format!("{FILTERED_DECK}/{did}"),
    );
}

pub fn open_custom_study(ui: &Ui, did: i64) {
    open(
        ui,
        CUSTOM_STUDY,
        &ui.main.tr.actions_custom_study(),
        WebviewKind::CustomStudy,
        format!("{CUSTOM_STUDY}/{did}"),
    );
}

pub fn open_media_check(ui: &Ui) {
    open(
        ui,
        MEDIA_CHECK,
        &ui.main.tr.media_check_window_title(),
        WebviewKind::MediaCheck,
        MEDIA_CHECK.into(),
    );
}

pub fn open_empty_cards(ui: &Ui) {
    open(
        ui,
        EMPTY_CARDS,
        &ui.main.tr.empty_cards_window_title(),
        WebviewKind::EmptyCards,
        EMPTY_CARDS.into(),
    );
}

fn close(ui: &Ui, label: &str) {
    if let Some(window) = ui.app.get_webview_window(label) {
        let _ = window.close();
    }
}

/// The collection changed under the main window: redraw it and the menus.
fn collection_changed(ui: &Arc<Ui>) {
    crate::menus::refresh(ui);
    main_window::refresh(ui);
}

pub fn register(ui: &Arc<Ui>) {
    let reg = |name: &str, label: &'static str, f: fn(&Arc<Ui>, &str) -> Value| {
        let u = ui.clone();
        ui.shell.bridge.register(name, move |_, from, args| {
            if from != label {
                tracing::warn!(from, "rejected a screen command from another window");
                return Value::Null;
            }
            f(&u, args)
        });
    };
    // a filtered deck was built (`args` is its id): study it, as Qt's
    // overview after the dialog
    reg("filteredDeckBuilt", FILTERED_DECK, |ui, did| {
        if let Ok(did) = did.parse::<i64>() {
            let _: Result<anki_proto::collection::OpChanges, _> = ui
                .shell
                .api
                .call("set_current_deck", &anki_proto::decks::DeckId { did });
        }
        // the overview first: closing the last window would quit
        main_window::show_overview(ui);
        close(ui, FILTERED_DECK);
        crate::menus::refresh(ui);
        Value::Null
    });
    // custom study made its session deck current, or raised today's limits
    // of deck `args` (which is then shown, though the main window may have
    // moved on)
    reg("customStudyDone", CUSTOM_STUDY, |ui, did| {
        if let Ok(did) = did.parse::<i64>() {
            let _: Result<anki_proto::collection::OpChanges, _> = ui
                .shell
                .api
                .call("set_current_deck", &anki_proto::decks::DeckId { did });
        }
        main_window::show_overview(ui);
        close(ui, CUSTOM_STUDY);
        crate::menus::refresh(ui);
        Value::Null
    });
    // Cancel or Escape in either dialog (one handler per command name)
    let u = ui.clone();
    ui.shell.bridge.register("screenClose", move |_, from, _| {
        if [FILTERED_DECK, CUSTOM_STUDY].contains(&from) {
            close(&u, from);
        }
        Value::Null
    });
    reg(
        "renderAllLatex",
        MEDIA_CHECK,
        |ui, _| match render_all_latex(ui) {
            Ok(None) => json!({ "done": true }),
            Ok(Some((nid, error))) => json!({ "nid": nid, "error": error }),
            Err(err) => json!({ "error": format!("{err:#}") }),
        },
    );
    reg("openMediaFolder", MEDIA_CHECK, |ui, _| {
        if let Some(media) = ui.shell.media_folder() {
            let _ = std::process::Command::new("open").arg(media).spawn();
        }
        Value::Null
    });
    reg("mediaChanged", MEDIA_CHECK, |ui, _| {
        collection_changed(ui);
        Value::Null
    });
    reg("emptyCardsDone", EMPTY_CARDS, |ui, _| {
        collection_changed(ui);
        Value::Null
    });
}

/// `MediaManager.render_all_latex`: make every note's missing LaTeX
/// images; the first note whose LaTeX fails, with the errors.
pub fn render_all_latex(ui: &Ui) -> Result<Option<(i64, String)>> {
    let media = ui
        .shell
        .media_folder()
        .ok_or_else(|| anyhow::anyhow!("no collection open"))?;
    render_all_latex_in(&ui.shell.api, &media, &crate::latex::toolchain())
}

fn render_all_latex_in(
    api: &crate::api::Api,
    media: &std::path::Path,
    toolchain: &crate::latex::LatexCommands,
) -> Result<Option<(i64, String)>> {
    let rows = api
        .query("select id, mid, flds from notes where flds like '%[%'", &[])
        .map_err(|e| anyhow::anyhow!(e.message))?;
    let mut configs = std::collections::HashMap::new();
    for row in rows {
        let (Some(nid), Some(mid), Some(fields)) =
            (row[0].as_i64(), row[1].as_i64(), row[2].as_str())
        else {
            continue;
        };
        let cfg = match configs.get(&mid) {
            Some(cfg) => cfg,
            None => configs
                .entry(mid)
                .or_insert(crate::latex::latex_config_with(api, mid, toolchain)?),
        };
        // clozes expanded, as media.py does and the media check looks
        let (_, errors) = crate::latex::render_latex_expanding(api, media, fields, cfg, true);
        if !errors.is_empty() {
            let text: Vec<String> = errors.iter().map(|e| html_to_text(e)).collect();
            return Ok(Some((nid, text.join("\n"))));
        }
    }
    Ok(None)
}

/// latex.rs's HTML messages as plain text: line breaks kept, tags dropped,
/// entities decoded.
fn html_to_text(html: &str) -> String {
    let with_breaks = html.replace("<br>", "\n");
    let tags = regex::Regex::new(r"<[^>]*>").unwrap();
    tags.replace_all(&with_breaks, "")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", "\"")
        .replace("&#x27;", "'")
        .replace("&amp;", "&")
}

#[cfg(test)]
mod test {
    use anki_proto::generic::Empty;
    use anki_proto::notes::AddNoteRequest;
    use anki_proto::notes::AddNoteResponse;
    use anki_proto::notes::Note;
    use anki_proto::notetypes::NotetypeNames;

    use super::*;
    use crate::api::Api;
    use crate::latex::LatexCommands;
    use crate::profile::open_collection;
    use crate::profile::ProfilePaths;

    fn add_note(api: &Api, front: &str) -> i64 {
        add_note_of(api, "Basic", front)
    }

    fn add_note_of(api: &Api, notetype: &str, front: &str) -> i64 {
        let names: NotetypeNames = api.call("get_notetype_names", &Empty {}).unwrap();
        let basic = names.entries.iter().find(|e| e.name == notetype).unwrap();
        let mut note: Note = api
            .call(
                "new_note",
                &anki_proto::notetypes::NotetypeId { ntid: basic.id },
            )
            .unwrap();
        note.fields[0] = front.into();
        let added: AddNoteResponse = api
            .call(
                "add_note",
                &AddNoteRequest {
                    note: Some(note),
                    deck_id: 1,
                },
            )
            .unwrap();
        added.note_id
    }

    #[test]
    fn render_all_latex_makes_missing_images_and_names_the_first_failure() {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        let api = Api::new(vec!["en".into()]).unwrap();
        open_collection(&api, &paths).unwrap();
        let _: Empty = api
            .call(
                "set_config_bool",
                &anki_proto::config::SetConfigBoolRequest {
                    key: anki_proto::config::config_key::Bool::RenderLatex as i32,
                    value: true,
                    undoable: false,
                },
            )
            .unwrap();
        let media = paths.media();
        add_note(&api, "no latex [here]");
        add_note(&api, "[$]x[/$]");
        // fails on a marker
        let toolchain = LatexCommands {
            png: Some(vec![vec![
                "sh".into(),
                "-c".into(),
                "grep -q FAIL tmp.tex && exit 1; printf png > tmp.png".into(),
            ]]),
            svg: None,
        };
        let first = render_all_latex_in(&api, &media, &toolchain);
        let bad = add_note(&api, "[$]FAIL[/$]");
        let second = render_all_latex_in(&api, &media, &toolchain);

        assert_eq!(first.unwrap(), None);
        let files: Vec<_> = std::fs::read_dir(&media).unwrap().collect();
        assert_eq!(files.len(), 1);
        let (nid, error) = second.unwrap().unwrap();
        assert_eq!(nid, bad);
        // the backend's translations wrap arguments in isolation marks
        let error = error.replace(['\u{2068}', '\u{2069}'], "");
        assert!(error.starts_with("Error executing sh."), "{error}");
        assert!(!error.contains("<br>"), "{error}");
    }

    #[test]
    fn render_all_latex_makes_the_images_clozes_show() {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        let api = Api::new(vec!["en".into()]).unwrap();
        open_collection(&api, &paths).unwrap();
        let _: Empty = api
            .call(
                "set_config_bool",
                &anki_proto::config::SetConfigBoolRequest {
                    key: anki_proto::config::config_key::Bool::RenderLatex as i32,
                    value: true,
                    undoable: false,
                },
            )
            .unwrap();
        // the cloze is inside the LaTeX, so the image the card shows is of
        // the expanded body
        let text = "[$]{{c1::x}}[/$]";
        add_note_of(&api, "Cloze", text);
        let toolchain = LatexCommands {
            png: Some(vec![vec!["sh".into(), "-c".into(), "printf png > tmp.png".into()]]),
            svg: None,
        };
        assert_eq!(render_all_latex_in(&api, &paths.media(), &toolchain).unwrap(), None);
        let expanded: anki_proto::card_rendering::ExtractLatexResponse = api
            .call(
                "extract_latex",
                &anki_proto::card_rendering::ExtractLatexRequest {
                    text: text.into(),
                    svg: false,
                    expand_clozes: true,
                },
            )
            .unwrap();
        for latex in &expanded.latex {
            assert!(paths.media().join(&latex.filename).exists(), "{}", latex.filename);
        }
        assert!(!expanded.latex.is_empty());
    }

    #[test]
    fn latex_messages_become_text() {
        assert_eq!(
            html_to_text("Error.<br><small><pre>a &lt;b&gt; &amp;&#x27;</pre></small>"),
            "Error.\na <b> &'"
        );
    }
}
