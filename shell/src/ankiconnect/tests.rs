// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::path::PathBuf;

use serde_json::json;
use serde_json::Value;

use super::actions::NATIVE;
use super::handle;
use crate::api::Api;
use crate::profile::ProfilePaths;
use crate::Shell;

pub(crate) fn temp_shell() -> (tempfile::TempDir, Shell) {
    let dir = tempfile::tempdir().unwrap();
    let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), PathBuf::new());
    shell
        .open_profile(ProfilePaths::new(dir.path(), "User 1"))
        .unwrap();
    (dir, shell)
}

fn call(shell: &Shell, action: &str, params: Value) -> Result<Value, String> {
    let out = handle(
        shell,
        &json!({"action": action, "version": 6, "params": params}),
    );
    match &out["error"] {
        Value::Null => Ok(out["result"].clone()),
        err => Err(err.as_str().unwrap().to_string()),
    }
}

fn ok(shell: &Shell, action: &str, params: Value) -> Value {
    call(shell, action, params).unwrap_or_else(|err| panic!("{action}: {err}"))
}

fn err(shell: &Shell, action: &str, params: Value) -> String {
    call(shell, action, params).expect_err(action)
}

fn basic(deck: &str, front: &str, back: &str) -> Value {
    json!({"deckName": deck, "modelName": "Basic", "fields": {"Front": front, "Back": back}})
}

fn add(shell: &Shell, front: &str) -> i64 {
    ok(
        shell,
        "addNote",
        json!({"note": basic("Default", front, "back")}),
    )
    .as_i64()
    .unwrap()
}

fn cards_of(shell: &Shell, nid: i64) -> Vec<i64> {
    serde_json::from_value(ok(
        shell,
        "findCards",
        json!({"query": format!("nid:{nid}")}),
    ))
    .unwrap()
}

#[test]
fn version_uses_the_requested_reply_format() {
    let (_dir, shell) = temp_shell();
    assert_eq!(
        handle(&shell, &json!({"action": "version", "version": 6})),
        json!({"result": 6, "error": null})
    );
    // version <= 4 (the default) replies with the bare result
    assert_eq!(handle(&shell, &json!({"action": "version"})), json!(6));
    // but errors always use the new format
    assert_eq!(
        handle(&shell, &json!({"action": "nope"})),
        json!({"result": null, "error": "unsupported action"})
    );
}

#[test]
fn unported_actions_say_so() {
    let (_dir, shell) = temp_shell();
    for action in [
        "guiBrowse",
        "sync",
        "exportPackage",
        "createModel",
        "loadProfile",
    ] {
        assert_eq!(
            err(&shell, action, json!({})),
            format!("{action} is not supported by the native shell yet")
        );
    }
}

#[test]
fn key_and_argument_errors_match_the_add_on() {
    let (_dir, shell) = temp_shell();
    let out = handle(
        &shell,
        &json!({"action": "version", "version": 6, "key": "x"}),
    );
    assert_eq!(out["error"], "valid api key must be provided");
    assert_eq!(
        err(&shell, "createDeck", json!({})),
        "AnkiConnect.createDeck() missing 1 required positional argument: 'deck'"
    );
    assert_eq!(
        err(&shell, "changeDeck", json!({})),
        "AnkiConnect.changeDeck() missing 2 required positional arguments: 'cards' and 'deck'"
    );
    assert_eq!(
        err(&shell, "deckNames", json!({"x": 1})),
        "AnkiConnect.deckNames() got an unexpected keyword argument 'x'"
    );
    assert_eq!(
        err(&shell, "deckNames", json!([])),
        "AnkiConnect.deckNames() argument after ** must be a mapping, not list"
    );
}

#[test]
fn actions_needing_a_collection_fail_without_one() {
    let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), PathBuf::new());
    assert_eq!(
        err(&shell, "deckNames", json!({})),
        "collection is not available"
    );
    assert_eq!(
        err(&shell, "getMediaDirPath", json!({})),
        "collection is not available"
    );
    assert_eq!(ok(&shell, "version", json!({})), json!(6));
    assert_eq!(ok(&shell, "getActiveProfile", json!({})), Value::Null);
}

#[test]
fn every_native_action_is_dispatched_and_reflected() {
    let (_dir, shell) = temp_shell();
    for &action in NATIVE {
        if let Err(msg) = call(&shell, action, json!({})) {
            assert!(
                msg != "unsupported action" && !msg.contains("native shell"),
                "{action}: {msg}"
            );
        }
    }
    let mut sorted = NATIVE.to_vec();
    sorted.sort_unstable();
    assert_eq!(
        ok(&shell, "apiReflect", json!({"scopes": ["actions"]})),
        json!({"scopes": ["actions"], "actions": sorted})
    );
    assert_eq!(
        ok(
            &shell,
            "apiReflect",
            json!({"scopes": ["actions"], "actions": ["addNote", "guiBrowse", "x"]})
        ),
        json!({"scopes": ["actions"], "actions": ["addNote"]})
    );
    assert_eq!(
        err(&shell, "apiReflect", json!({"scopes": null})),
        "scopes has invalid value"
    );
}

#[test]
fn profiles() {
    let (dir, shell) = temp_shell();
    assert_eq!(ok(&shell, "getActiveProfile", json!({})), json!("User 1"));
    std::fs::create_dir_all(dir.path().join("Other")).unwrap();
    std::fs::write(dir.path().join("Other/collection.anki2"), b"").unwrap();
    std::fs::create_dir_all(dir.path().join("addons21")).unwrap();
    assert_eq!(
        ok(&shell, "getProfiles", json!({})),
        json!(["Other", "User 1"])
    );
}

#[test]
fn decks() {
    let (_dir, shell) = temp_shell();
    let id = ok(&shell, "createDeck", json!({"deck": "A::B"}));
    assert!(id.as_i64().unwrap() > 1);
    // creating again returns the same deck
    assert_eq!(ok(&shell, "createDeck", json!({"deck": "A::B"})), id);
    assert_eq!(
        ok(&shell, "deckNames", json!({})),
        json!(["A", "A::B", "Default"])
    );
    let ids = ok(&shell, "deckNamesAndIds", json!({}));
    assert_eq!(ids["A::B"], id);
    assert_eq!(ids["Default"], 1);

    let conf = ok(&shell, "getDeckConfig", json!({"deck": "Default"}));
    assert_eq!(conf["id"], 1);
    assert_eq!(conf["dyn"], false);
    assert!(conf["new"]["perDay"].is_number(), "{conf}");
    assert_eq!(ok(&shell, "getDeckConfig", json!({"deck": "nope"})), false);

    let nid = add(&shell, "front");
    let cards = cards_of(&shell, nid);
    assert_eq!(
        ok(&shell, "changeDeck", json!({"cards": cards, "deck": "C"})),
        Value::Null
    );
    assert_eq!(
        ok(&shell, "findCards", json!({"query": "deck:C"})),
        json!(cards)
    );

    // the deck tree hides an empty Default deck
    add(&shell, "in default");
    let stats = ok(
        &shell,
        "getDeckStats",
        json!({"decks": ["C", "Default", "missing"]}),
    );
    let c = ids_of_deck(&shell, "C");
    assert_eq!(
        stats[c.to_string()],
        json!({"deck_id": c, "name": "C", "new_count": 1, "learn_count": 0,
               "review_count": 0, "total_in_deck": 1})
    );
    assert_eq!(stats["1"]["name"], "Default");
    assert_eq!(stats.as_object().unwrap().len(), 2);
}

fn ids_of_deck(shell: &Shell, name: &str) -> i64 {
    ok(shell, "deckNamesAndIds", json!({}))[name]
        .as_i64()
        .unwrap()
}

#[test]
fn models() {
    let (_dir, shell) = temp_shell();
    let names = ok(&shell, "modelNames", json!({}));
    assert!(
        names.as_array().unwrap().contains(&json!("Basic")),
        "{names}"
    );
    let ids = ok(&shell, "modelNamesAndIds", json!({}));
    assert!(ids["Basic"].as_i64().unwrap() > 0);
    let models = ok(&shell, "findModelsByName", json!({"modelNames": ["Basic"]}));
    assert_eq!(models[0]["name"], "Basic");
    assert_eq!(models[0]["id"], ids["Basic"]);
    assert_eq!(
        err(&shell, "findModelsByName", json!({"modelNames": ["Nope"]})),
        "model was not found: Nope"
    );
    assert_eq!(
        ok(&shell, "modelFieldNames", json!({"modelName": "Basic"})),
        json!(["Front", "Back"])
    );
    assert_eq!(
        err(&shell, "modelFieldNames", json!({"modelName": "Nope"})),
        "model was not found: Nope"
    );
    assert_eq!(
        ok(
            &shell,
            "modelFieldsOnTemplates",
            json!({"modelName": "Basic (and reversed card)"})
        ),
        json!({"Card 1": [["Front"], ["Back"]], "Card 2": [["Back"], ["Front"]]})
    );
    let templates = ok(&shell, "modelTemplates", json!({"modelName": "Basic"}));
    assert_eq!(templates["Card 1"]["Front"], "{{Front}}");
    let styling = ok(&shell, "modelStyling", json!({"modelName": "Basic"}));
    assert!(styling["css"].as_str().unwrap().contains(".card"));
}

#[test]
fn adding_notes_checks_duplicates_like_the_add_on() {
    let (_dir, shell) = temp_shell();
    let nid = add(&shell, "one");
    assert_eq!(
        err(
            &shell,
            "addNote",
            json!({"note": basic("Default", "one", "x")})
        ),
        "cannot create note because it is a duplicate"
    );
    assert_eq!(
        err(
            &shell,
            "addNote",
            json!({"note": basic("Default", "", "x")})
        ),
        "cannot create note because it is empty"
    );
    assert_eq!(
        err(&shell, "addNote", json!({"note": basic("Nope", "a", "x")})),
        "deck was not found: Nope"
    );
    let mut note = basic("Default", "one", "x");
    note["modelName"] = json!("Nope");
    assert_eq!(
        err(&shell, "addNote", json!({"note": note})),
        "model was not found: Nope"
    );
    // field names are matched case-insensitively; tags are kept
    let mut note = json!({"deckName": "Default", "modelName": "Basic",
                          "fields": {"front": "two"}, "tags": ["t1"]});
    let two = ok(&shell, "addNote", json!({"note": note.clone()}));
    assert_eq!(
        ok(&shell, "getNoteTags", json!({"note": two})),
        json!(["t1"])
    );
    note["options"] = json!({"allowDuplicate": true});
    assert!(ok(&shell, "addNote", json!({"note": note})).is_i64());

    // deck-scoped duplicate check: a copy in another deck is fine
    ok(&shell, "createDeck", json!({"deck": "Other"}));
    let mut other = basic("Other", "one", "x");
    other["options"] = json!({"duplicateScope": "deck"});
    assert_eq!(
        ok(
            &shell,
            "canAddNotes",
            json!({"notes": [other.clone(), basic("Default", "one", "")]})
        ),
        json!([true, false])
    );
    let other_nid = ok(&shell, "addNote", json!({"note": other.clone()}));
    assert_eq!(
        err(&shell, "addNote", json!({"note": other})),
        "cannot create note because it is a duplicate"
    );

    // addNotes adds all or nothing, listing the errors Python-style
    let before = ok(&shell, "findNotes", json!({"query": ""}));
    assert_eq!(
        err(
            &shell,
            "addNotes",
            json!({"notes": [basic("Default", "three", ""), basic("Default", "one", "")]})
        ),
        "['cannot create note because it is a duplicate']"
    );
    assert_eq!(ok(&shell, "findNotes", json!({"query": ""})), before);
    let added = ok(
        &shell,
        "addNotes",
        json!({"notes": [basic("Default", "three", ""), basic("Default", "four", "")]}),
    );
    assert_eq!(added.as_array().unwrap().len(), 2);
    assert!(nid > 0 && other_nid.is_i64());
}

#[test]
fn notes_info_and_updates() {
    let (_dir, shell) = temp_shell();
    let nid = add(&shell, "front");
    let cards = cards_of(&shell, nid);
    let info = ok(&shell, "notesInfo", json!({"notes": [nid, 123]}));
    let mod_ = info[0]["mod"].clone();
    assert_eq!(
        info,
        json!([{
            "noteId": nid,
            "profile": "User 1",
            "tags": [],
            "fields": {"Front": {"value": "front", "order": 0},
                       "Back": {"value": "back", "order": 1}},
            "modelName": "Basic",
            "mod": mod_,
            "cards": cards,
        }, {}])
    );
    assert_eq!(
        ok(&shell, "notesInfo", json!({"query": "front"}))[0]["noteId"],
        nid
    );
    assert_eq!(
        err(&shell, "notesInfo", json!({})),
        "Must provide either \"notes\" or a \"query\""
    );
    assert_eq!(
        ok(&shell, "notesModTime", json!({"notes": [nid, 123]})),
        json!([{"noteId": nid, "mod": mod_}, {}])
    );
    assert_eq!(
        ok(&shell, "cardsToNotes", json!({"cards": cards})),
        json!([nid])
    );

    ok(
        &shell,
        "updateNoteFields",
        json!({"note": {"id": nid, "fields": {"Back": "new", "Missing": "x"}}}),
    );
    let info = ok(&shell, "notesInfo", json!({"notes": [nid]}));
    assert_eq!(info[0]["fields"]["Back"]["value"], "new");
    assert_eq!(info[0]["fields"]["Front"]["value"], "front");
    assert_eq!(
        err(
            &shell,
            "updateNoteFields",
            json!({"note": {"id": 123, "fields": {}}})
        ),
        "Note was not found: 123"
    );

    ok(
        &shell,
        "updateNote",
        json!({"note": {"id": nid, "fields": {"Front": "f2"}, "tags": ["a", "b"]}}),
    );
    let info = ok(&shell, "notesInfo", json!({"notes": [nid]}));
    assert_eq!(info[0]["fields"]["Front"]["value"], "f2");
    assert_eq!(info[0]["tags"], json!(["a", "b"]));
    assert_eq!(
        err(&shell, "updateNote", json!({"note": {"id": nid}})),
        "Must provide a \"fields\" or \"tags\" property."
    );

    assert_eq!(
        ok(&shell, "deleteNotes", json!({"notes": [nid]})),
        Value::Null
    );
    assert_eq!(ok(&shell, "findNotes", json!({"query": ""})), json!([]));
    assert_eq!(ok(&shell, "findNotes", json!({})), json!([]));
}

#[test]
fn tags() {
    let (_dir, shell) = temp_shell();
    let a = add(&shell, "a");
    let b = add(&shell, "b");
    ok(&shell, "addTags", json!({"notes": [a, b], "tags": "x y"}));
    ok(&shell, "removeTags", json!({"notes": [b], "tags": "x"}));
    assert_eq!(
        ok(&shell, "getNoteTags", json!({"note": a})),
        json!(["x", "y"])
    );
    assert_eq!(ok(&shell, "getNoteTags", json!({"note": b})), json!(["y"]));
    assert_eq!(ok(&shell, "getTags", json!({})), json!(["x", "y"]));
    ok(&shell, "updateNoteTags", json!({"note": a, "tags": "z"}));
    assert_eq!(ok(&shell, "getNoteTags", json!({"note": a})), json!(["z"]));
    assert_eq!(
        err(&shell, "updateNoteTags", json!({"note": a, "tags": [1]})),
        "Must provide tags as a list of strings"
    );
}

#[test]
fn suspending() {
    let (_dir, shell) = temp_shell();
    let nid = add(&shell, "a");
    let cid = cards_of(&shell, nid)[0];
    assert_eq!(ok(&shell, "suspended", json!({"card": cid})), false);
    assert_eq!(ok(&shell, "suspend", json!({"cards": [cid]})), true);
    // nothing left to change
    assert_eq!(ok(&shell, "suspend", json!({"cards": [cid]})), false);
    assert_eq!(ok(&shell, "suspended", json!({"card": cid})), true);
    assert_eq!(
        ok(&shell, "areSuspended", json!({"cards": [cid, 123]})),
        json!([true, null])
    );
    assert_eq!(
        err(&shell, "suspended", json!({"card": 123})),
        "Card was not found: 123"
    );
    assert_eq!(
        ok(&shell, "unsuspend", json!({"cards": [cid]})),
        Value::Null
    );
    assert_eq!(ok(&shell, "suspended", json!({"card": cid})), false);
}

#[test]
fn cards_info_renders_question_and_answer() {
    let (_dir, shell) = temp_shell();
    let nid = add(&shell, "front <b>text</b>");
    let cid = cards_of(&shell, nid)[0];
    let info = ok(&shell, "cardsInfo", json!({"cards": [cid, 123]}));
    let card = &info[0];
    let css = card["css"].as_str().unwrap();
    assert!(css.contains(".card"));
    assert_eq!(
        card["question"],
        format!("<style>{css}</style>front <b>text</b>")
    );
    let answer = card["answer"].as_str().unwrap();
    assert!(answer.starts_with(&format!("<style>{css}</style>front <b>text</b>")));
    assert!(answer.ends_with("back"), "{answer}");
    assert_eq!(card["cardId"], cid);
    assert_eq!(card["note"], nid);
    assert_eq!(card["deckName"], "Default");
    assert_eq!(card["modelName"], "Basic");
    assert_eq!(card["fields"]["Front"]["order"], 0);
    assert_eq!(card["nextReviews"].as_array().unwrap().len(), 4);
    for key in [
        "fieldOrder",
        "ord",
        "factor",
        "interval",
        "type",
        "queue",
        "due",
        "reps",
        "lapses",
        "left",
        "mod",
        "flags",
    ] {
        assert!(card[key].is_number(), "{key}: {card}");
    }
    assert_eq!(card.as_object().unwrap().len(), 21);
    assert_eq!(info[1], json!({}));
}

#[test]
fn sound_tags_are_kept_as_play_references() {
    let (_dir, shell) = temp_shell();
    let nid = add(&shell, "hi [sound:a.mp3]");
    let cid = cards_of(&shell, nid)[0];
    let info = ok(&shell, "cardsInfo", json!({"cards": [cid]}));
    assert!(
        info[0]["question"]
            .as_str()
            .unwrap()
            .ends_with("hi [anki:play:q:0]"),
        "{}",
        info[0]["question"]
    );
}

#[test]
fn scheduling_and_reviews() {
    let (_dir, shell) = temp_shell();
    let nid = add(&shell, "a");
    let cid = cards_of(&shell, nid)[0];
    assert_eq!(
        ok(&shell, "getReviewsOfCards", json!({"cards": [cid]})),
        json!({cid.to_string(): []})
    );
    assert_eq!(
        ok(&shell, "setDueDate", json!({"cards": [cid], "days": "3"})),
        true
    );
    let card = &ok(&shell, "cardsInfo", json!({"cards": [cid]}))[0];
    assert_eq!(card["type"], 2);
    let reviews = ok(&shell, "getReviewsOfCards", json!({"cards": [cid]}));
    let review = &reviews[cid.to_string()][0];
    let keys: Vec<_> = review.as_object().unwrap().keys().cloned().collect();
    assert_eq!(
        keys,
        ["ease", "factor", "id", "ivl", "lastIvl", "time", "type", "usn"]
    );
    assert_eq!(
        ok(&shell, "forgetCards", json!({"cards": [cid]})),
        Value::Null
    );
    let card = &ok(&shell, "cardsInfo", json!({"cards": [cid]}))[0];
    assert_eq!(card["type"], 0);
    assert_eq!(ok(&shell, "reloadCollection", json!({})), Value::Null);
}

#[test]
fn media_files() {
    let (dir, shell) = temp_shell();
    let media = dir.path().join("User 1/collection.media");
    assert_eq!(
        ok(&shell, "getMediaDirPath", json!({})),
        json!(std::path::absolute(&media).unwrap())
    );
    // "hello" in base64
    let stored = ok(
        &shell,
        "storeMediaFile",
        json!({"filename": "a.txt", "data": "aGVsbG8="}),
    );
    assert_eq!(stored, "a.txt");
    assert_eq!(std::fs::read(media.join("a.txt")).unwrap(), b"hello");
    assert_eq!(
        ok(&shell, "retrieveMediaFile", json!({"filename": "a.txt"})),
        "aGVsbG8="
    );
    // the same name is replaced rather than renamed
    ok(
        &shell,
        "storeMediaFile",
        json!({"filename": "a.txt", "data": "Ynll"}),
    );
    assert_eq!(std::fs::read(media.join("a.txt")).unwrap(), b"bye");
    // skipHash is the md5 of the data
    assert_eq!(
        ok(
            &shell,
            "storeMediaFile",
            json!({"filename": "b.txt", "data": "aGVsbG8=",
                   "skipHash": "5d41402abc4b2a76b9719d911017c592"})
        ),
        Value::Null
    );
    let src = dir.path().join("src.png");
    std::fs::write(&src, b"png").unwrap();
    ok(
        &shell,
        "storeMediaFile",
        json!({"filename": "c.png", "path": src}),
    );
    assert_eq!(
        ok(&shell, "getMediaFilesNames", json!({"pattern": "*.txt"})),
        json!(["a.txt"])
    );
    assert_eq!(
        ok(&shell, "getMediaFilesNames", json!({})),
        json!(["a.txt", "c.png"])
    );
    assert_eq!(
        err(&shell, "storeMediaFile", json!({"filename": "d.png"})),
        "You must provide a \"data\", \"path\", or \"url\" field."
    );
    assert_eq!(
        err(
            &shell,
            "storeMediaFile",
            json!({"filename": "d.png", "url": "http://example.com/a.png"})
        ),
        "storeMediaFile with a url is not supported by the native shell yet"
    );
    assert_eq!(
        ok(&shell, "deleteMediaFile", json!({"filename": "a.txt"})),
        Value::Null
    );
    assert_eq!(
        ok(&shell, "retrieveMediaFile", json!({"filename": "a.txt"})),
        false
    );
}

#[test]
fn adding_a_note_with_a_picture() {
    let (dir, shell) = temp_shell();
    let mut note = basic("Default", "pic", "");
    note["picture"] = json!([{"filename": "p.png", "data": "cG5n", "fields": ["Back"]}]);
    let nid = ok(&shell, "addNote", json!({"note": note}));
    let info = ok(&shell, "notesInfo", json!({"notes": [nid]}));
    assert_eq!(info[0]["fields"]["Back"]["value"], "<img src=\"p.png\">");
    assert!(dir.path().join("User 1/collection.media/p.png").exists());
}

#[test]
fn multi_replies_per_action_version() {
    let (_dir, shell) = temp_shell();
    assert_eq!(
        ok(
            &shell,
            "multi",
            json!({"actions": [
                {"action": "version"},
                {"action": "version", "version": 6},
                {"action": "nope", "version": 6},
            ]})
        ),
        json!([6, {"result": 6, "error": null}, {"result": null, "error": "unsupported action"}])
    );
}

#[test]
fn request_permission() {
    let (_dir, shell) = temp_shell();
    assert_eq!(
        ok(
            &shell,
            "requestPermission",
            json!({"origin": "http://localhost", "allowed": true})
        ),
        json!({"permission": "granted", "requireApikey": false, "version": 6})
    );
    // there's no prompt in the shell, so unknown origins are denied
    assert_eq!(
        ok(
            &shell,
            "requestPermission",
            json!({"origin": "https://example.com", "allowed": false})
        ),
        json!({"permission": "denied"})
    );
}
