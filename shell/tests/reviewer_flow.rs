// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The reviewer driver against a real collection, without a webview: the
//! effects it asks for, and what ends up in the collection.

use anki_proto::notes::AddNoteRequest;
use anki_proto::notes::AddNoteResponse;
use anki_proto::notes::Note;
use anki_proto::notetypes::NotetypeId;
use anki_shell::api::Api;
use anki_shell::profile::ProfilePaths;
use anki_shell::reviewer::Effect;
use anki_shell::reviewer::Reviewer;
use anki_shell::reviewer::Side;
use serde_json::json;

struct Col {
    api: Api,
    paths: ProfilePaths,
    _dir: tempfile::TempDir,
}

fn collection() -> Col {
    let dir = tempfile::tempdir().unwrap();
    let paths = ProfilePaths::new(dir.path(), "test");
    std::fs::create_dir_all(paths.media()).unwrap();
    let api = Api::new(vec!["en".into()]).unwrap();
    anki_shell::profile::open_collection(&api, &paths).unwrap();
    Col {
        api,
        paths,
        _dir: dir,
    }
}

fn add_note(api: &Api, notetype: &str, fields: &[&str]) -> i64 {
    let ntid: NotetypeId = api
        .call(
            "get_notetype_id_by_name",
            &anki_proto::generic::String {
                val: notetype.into(),
            },
        )
        .unwrap();
    let mut note: Note = api.call("new_note", &ntid).unwrap();
    for (i, f) in fields.iter().enumerate() {
        note.fields[i] = f.to_string();
    }
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

fn count(api: &Api, sql: &str) -> i64 {
    api.query(sql, &[]).unwrap()[0][0].as_i64().unwrap()
}

fn js(effects: &[Effect]) -> String {
    effects
        .iter()
        .filter_map(|e| match e {
            Effect::Js(js) => Some(js.as_str()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// A reviewer showing the first card's question.
fn started(col: &Col) -> (Reviewer, Vec<Effect>) {
    let mut r = Reviewer::default();
    let effects = r.start(&col.api, col.paths.media());
    assert_eq!(effects, vec![Effect::LoadPage]);
    let effects = r.page_ready(&col.api);
    (r, effects)
}

fn reveal(col: &Col, r: &mut Reviewer, typed: &str) -> Vec<Effect> {
    let ask = js(&r.request_answer());
    assert!(ask.contains("pycmd('typedAnswer:'"), "{ask}");
    r.show_answer(&col.api, typed)
}

#[test]
fn shows_question_then_answer_then_answers() {
    let col = collection();
    add_note(&col.api, "Basic", &["front one", "back one"]);
    add_note(&col.api, "Basic", &["front two", "back two"]);
    let (mut r, effects) = started(&col);
    let page = js(&effects);
    assert!(page.contains("_showQuestion("), "{page}");
    assert!(page.contains("front"));
    assert!(page.contains("_shellBottomCall(\"showQuestion\""));
    assert!(page.contains("card card1 isMac"));
    assert_eq!(r.side(), Some(Side::Question));

    // can't answer from the question side
    assert!(r.answer(&col.api, 3).is_empty());
    assert_eq!(count(&col.api, "select count() from revlog"), 0);

    let page = js(&reveal(&col, &mut r, ""));
    assert!(page.contains("_showAnswer("));
    assert!(page.contains("back"));
    for ease in 1..=4 {
        assert!(
            page.contains(&format!("pycmd(\\\"ease{ease}\\\")")),
            "{page}"
        );
    }
    let first = r.card_id().unwrap();
    let page = js(&r.answer(&col.api, 3));
    assert_eq!(count(&col.api, "select count() from revlog"), 1);
    assert!(page.contains("_showQuestion("));
    assert_ne!(r.card_id(), Some(first));
    assert_eq!(r.previous_card_id(), Some(first));
}

#[test]
fn undo_brings_the_answered_card_back() {
    let col = collection();
    add_note(&col.api, "Basic", &["one", "1"]);
    add_note(&col.api, "Basic", &["two", "2"]);
    let (mut r, _) = started(&col);
    let first = r.card_id().unwrap();
    reveal(&col, &mut r, "");
    r.answer(&col.api, 3);
    assert_ne!(r.card_id(), Some(first));
    let effects = r.undo(&col.api, false);
    assert!(effects.contains(&Effect::UndoChanged));
    assert_eq!(count(&col.api, "select count() from revlog"), 0);
    assert_eq!(r.card_id(), Some(first));
    assert_eq!(r.side(), Some(Side::Question));
    // and redo answers it again
    r.undo(&col.api, true);
    assert_eq!(count(&col.api, "select count() from revlog"), 1);
}

#[test]
fn the_last_card_leads_to_the_overview() {
    let col = collection();
    add_note(&col.api, "Basic", &["only", "1"]);
    let (mut r, _) = started(&col);
    reveal(&col, &mut r, "");
    // Easy graduates a new card, so nothing is left today
    let effects = r.answer(&col.api, 4);
    assert!(effects.contains(&Effect::Overview), "{effects:?}");
    assert_eq!(r.side(), None);
}

#[test]
fn flags_and_marks_toggle() {
    let col = collection();
    let nid = add_note(&col.api, "Basic", &["q", "a"]);
    let (mut r, _) = started(&col);
    let cid = r.card_id().unwrap();
    let flags = |col: &Col| count(&col.api, &format!("select flags from cards where id={cid}"));
    assert!(js(&r.set_flag(&col.api, 2)).contains("_drawFlag(2)"));
    assert_eq!(flags(&col), 2);
    // the same flag again clears it
    assert!(js(&r.set_flag(&col.api, 2)).contains("_drawFlag(0)"));
    assert_eq!(flags(&col), 0);

    assert!(js(&r.toggle_mark(&col.api)).contains("_drawMark(true)"));
    let tags = |col: &Col| {
        col.api
            .query(&format!("select tags from notes where id={nid}"), &[])
            .unwrap()[0][0]
            .clone()
    };
    assert_eq!(tags(&col), json!(" marked "));
    assert!(js(&r.toggle_mark(&col.api)).contains("_drawMark(false)"));
    assert_eq!(tags(&col), json!(""));
}

#[test]
fn bury_and_suspend_move_on() {
    let col = collection();
    add_note(&col.api, "Basic", &["one", "1"]);
    add_note(&col.api, "Basic", &["two", "2"]);
    let (mut r, _) = started(&col);
    let first = r.card_id().unwrap();
    let effects = r.bury_or_suspend(&col.api, false, false);
    assert!(effects
        .iter()
        .any(|e| matches!(e, Effect::Tooltip(t) if t.contains("buried"))));
    assert_eq!(
        count(
            &col.api,
            &format!("select queue from cards where id={first}")
        ),
        -3
    );
    let second = r.card_id().unwrap();
    assert_ne!(second, first);
    r.bury_or_suspend(&col.api, false, true);
    assert_eq!(
        count(
            &col.api,
            &format!("select queue from cards where id={second}")
        ),
        -1
    );
    assert_eq!(r.side(), None);
}

#[test]
fn typed_answers_are_compared() {
    let col = collection();
    add_note(
        &col.api,
        "Basic (type in the answer)",
        &["capital", "Paris"],
    );
    let (mut r, effects) = started(&col);
    assert!(js(&effects).contains("id=typeans"));
    let page = js(&reveal(&col, &mut r, "Pairs"));
    // compare_answer's markup for a wrong answer
    assert!(
        page.contains("typeBad") || page.contains("typeMissed"),
        "{page}"
    );
    assert!(!page.contains("[[type:"));
}

#[test]
fn custom_scheduling_holds_the_ease_buttons() {
    let col = collection();
    add_note(&col.api, "Basic", &["q", "a"]);
    let _: anki_proto::collection::OpChanges = col
        .api
        .call(
            "set_config_string",
            &anki_proto::config::SetConfigStringRequest {
                key: anki_proto::config::config_key::String::CardStateCustomizer as i32,
                value: "states.good.normal.review.scheduledDays = 99;".into(),
                undoable: false,
            },
        )
        .unwrap();
    let (mut r, effects) = started(&col);
    let page = js(&effects);
    assert!(page.contains("anki.mutateNextCardStates"), "{page}");
    // the answer shows, but its buttons wait for statesMutated
    let page = js(&reveal(&col, &mut r, ""));
    assert!(page.contains("_showAnswer("));
    assert!(!page.contains("showAnswer\", ["));
    // a wrong key changes nothing
    let (states, _) = r.scheduling_states().unwrap();
    r.set_scheduling_states("not the key", Default::default());
    assert_eq!(r.scheduling_states().unwrap().0, states);
    let page = js(&r.states_mutated(&col.api));
    assert!(page.contains("_shellBottomCall(\"showAnswer\""), "{page}");
}

#[test]
fn auto_advance_shows_the_answer_after_the_delay() {
    let col = collection();
    add_note(&col.api, "Basic", &["q", "a"]);
    // 1s to show the question in the default preset
    let mut conf: anki_proto::deck_config::DeckConfig = col
        .api
        .call(
            "get_deck_config",
            &anki_proto::deck_config::DeckConfigId { dcid: 1 },
        )
        .unwrap();
    let c = conf.config.as_mut().unwrap();
    c.seconds_to_show_question = 1.0;
    c.wait_for_audio = false;
    let _: anki_proto::collection::OpChanges = col
        .api
        .call(
            "update_deck_configs",
            &anki_proto::deck_config::UpdateDeckConfigsRequest {
                target_deck_id: 1,
                configs: vec![conf],
                mode: 0,
                ..Default::default()
            },
        )
        .unwrap();
    let (mut r, _) = started(&col);
    let effects = r.toggle_auto_advance();
    let seq = effects
        .iter()
        .find_map(|e| match e {
            Effect::Timer { seq, after } => {
                assert_eq!(after.as_secs(), 1);
                Some(*seq)
            }
            _ => None,
        })
        .expect("a timer");
    assert!(js(&r.auto_advance_due(seq)).contains(&format!("autoAdvanceNow:{seq}")));
    // a stale timer does nothing
    assert!(r.auto_advance_due(seq + 100).is_empty());
    assert!(js(&r.auto_advance_now(&col.api, seq)).contains("typedAnswer"));
}

#[test]
fn custom_scheduling_starts_from_the_queue_states_on_each_showing() {
    let col = collection();
    add_note(&col.api, "Basic", &["q", "a"]);
    let (mut r, _) = started(&col);
    let (queued, _) = r.scheduling_states().unwrap();
    // the page's mutation code sends changed states with the key
    let mut mutated = queued.clone();
    mutated.good = mutated.easy.clone();
    let key = r.mutation_key().to_string();
    r.set_scheduling_states(&key, mutated.clone());
    assert_eq!(r.scheduling_states().unwrap().0, mutated);
    // showing the question again (a redraw) mustn't apply it to its own
    // output a second time
    r.redraw(&col.api);
    assert_eq!(r.scheduling_states().unwrap().0, queued);
}

#[test]
fn closing_the_editor_without_changes_doesnt_redraw() {
    let col = collection();
    let nid = add_note(&col.api, "Basic", &["q", "a"]);
    let (mut r, _) = started(&col);
    assert!(r.redraw_if_note_changed(&col.api).is_empty());
    let mut note: Note = col
        .api
        .call("get_note", &anki_proto::notes::NoteId { nid })
        .unwrap();
    note.fields[0] = "edited".into();
    // mtimes are in seconds
    std::thread::sleep(std::time::Duration::from_millis(1100));
    let _: anki_proto::collection::OpChanges = col
        .api
        .call(
            "update_notes",
            &anki_proto::notes::UpdateNotesRequest {
                notes: vec![note],
                skip_undo_entry: false,
            },
        )
        .unwrap();
    assert!(js(&r.redraw_if_note_changed(&col.api)).contains("edited"));
}

#[test]
fn space_rates_the_card_only_if_the_preference_says_so() {
    let col = collection();
    add_note(&col.api, "Basic", &["q", "a"]);
    let (mut r, _) = started(&col);
    reveal(&col, &mut r, "");
    assert!(js(&r.enter_key()).contains("pycmd('ease'"));
    r.spacebar_rates_card = false;
    assert!(r.enter_key().is_empty());
}

#[test]
fn redrawing_the_answer_shows_only_the_answer() {
    let col = collection();
    add_note(&col.api, "Basic", &["q", "a"]);
    let (mut r, _) = started(&col);
    reveal(&col, &mut r, "");
    let page = js(&r.redraw(&col.api));
    assert!(page.contains("_showAnswer("));
    assert!(!page.contains("_showQuestion("));
    assert_eq!(r.side(), Some(Side::Answer));
}

#[test]
fn an_add_on_suspending_the_shown_card_moves_on() {
    let col = collection();
    add_note(&col.api, "Basic", &["one", "1"]);
    add_note(&col.api, "Basic", &["two", "2"]);
    let (mut r, _) = started(&col);
    let first = r.card_id().unwrap();
    // unrelated changes leave the card alone
    assert!(r.op_executed(&col.api).is_empty());
    let _: anki_proto::collection::OpChangesWithCount = col
        .api
        .call(
            "bury_or_suspend_cards",
            &anki_proto::scheduler::BuryOrSuspendCardsRequest {
                card_ids: vec![first],
                note_ids: vec![],
                mode: 0,
            },
        )
        .unwrap();
    assert!(js(&r.op_executed(&col.api)).contains("two"));
    assert_ne!(r.card_id(), Some(first));
}

#[test]
fn answering_tells_add_ons_the_card_and_ease() {
    let col = collection();
    add_note(&col.api, "Basic", &["one", "1"]);
    add_note(&col.api, "Basic", &["two", "2"]);
    let (mut r, _) = started(&col);
    let card_id = r.card_id().unwrap();
    reveal(&col, &mut r, "");
    let effects = r.answer(&col.api, 3);
    assert!(
        effects.contains(&Effect::Answered { card_id, ease: 3 }),
        "{effects:?}"
    );
}
