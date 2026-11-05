// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Action dispatch, plus the miscellaneous, deck and model actions.

use std::sync::LazyLock;
use std::time::SystemTime;

use anki_proto::decks::DeckNameId;
use regex::Regex;
use serde_json::json;
use serde_json::Map;
use serde_json::Value;

use super::cards;
use super::handle_with;
use super::media;
use super::notes;
use super::params::Params;
use super::Ctx;
use super::Error;
use super::Result;
use super::API_VERSION;

/// The actions the shell implements, for `apiReflect`.
pub(crate) const NATIVE: &[&str] = &[
    "version",
    "requestPermission",
    "apiReflect",
    "multi",
    "getProfiles",
    "getActiveProfile",
    "reloadCollection",
    "deckNames",
    "deckNamesAndIds",
    "deckNameFromId",
    "getDeckStats",
    "getDeckConfig",
    "createDeck",
    "changeDeck",
    "modelNames",
    "modelNamesAndIds",
    "modelNameFromId",
    "findModelsById",
    "findModelsByName",
    "modelFieldNames",
    "modelFieldsOnTemplates",
    "modelTemplates",
    "modelStyling",
    "findNotes",
    "notesInfo",
    "notesModTime",
    "addNote",
    "addNotes",
    "canAddNote",
    "canAddNotes",
    "canAddNoteWithErrorDetail",
    "canAddNotesWithErrorDetail",
    "updateNoteFields",
    "updateNote",
    "updateNoteTags",
    "getNoteTags",
    "deleteNotes",
    "addTags",
    "removeTags",
    "getTags",
    "findCards",
    "cardsInfo",
    "cardsModTime",
    "cardsToNotes",
    "suspend",
    "unsuspend",
    "suspended",
    "areSuspended",
    "setDueDate",
    "forgetCards",
    "getReviewsOfCards",
    "storeMediaFile",
    "retrieveMediaFile",
    "getMediaFilesNames",
    "getMediaDirPath",
    "deleteMediaFile",
];

/// The add-on's other actions, which get a clearer error than an unknown
/// one.
const NOT_YET: &[&str] = &[
    "answerCards",
    "areDue",
    "cardReviews",
    "clearUnusedTags",
    "cloneDeckConfigId",
    "createModel",
    "deleteDecks",
    "exportPackage",
    "findAndReplaceInModels",
    "getCollectionStatsHTML",
    "getDecks",
    "getEaseFactors",
    "getIntervals",
    "getLatestReviewID",
    "getNumCardsReviewedByDay",
    "getNumCardsReviewedToday",
    "guiAddCards",
    "guiAnswerCard",
    "guiBrowse",
    "guiCheckDatabase",
    "guiCurrentCard",
    "guiDeckBrowser",
    "guiDeckOverview",
    "guiDeckReview",
    "guiEditNote",
    "guiExitAnki",
    "guiImportFile",
    "guiPlayAudio",
    "guiReviewActive",
    "guiSelectCard",
    "guiSelectNote",
    "guiSelectedNotes",
    "guiShowAnswer",
    "guiShowQuestion",
    "guiStartCardTimer",
    "guiUndo",
    "importPackage",
    "insertReviews",
    "loadProfile",
    "modelFieldAdd",
    "modelFieldDescriptions",
    "modelFieldFonts",
    "modelFieldRemove",
    "modelFieldRename",
    "modelFieldReposition",
    "modelFieldSetDescription",
    "modelFieldSetFont",
    "modelFieldSetFontSize",
    "modelTemplateAdd",
    "modelTemplateRemove",
    "modelTemplateRename",
    "modelTemplateReposition",
    "relearnCards",
    "removeDeckConfigId",
    "removeEmptyNotes",
    "replaceTags",
    "replaceTagsInAllNotes",
    "saveDeckConfig",
    "setDeckConfigId",
    "setEaseFactors",
    "setSpecificValueOfCard",
    "sync",
    "updateModelStyling",
    "updateModelTemplates",
    "updateNoteModel",
];

pub(crate) fn run(ctx: &Ctx, name: &str, params: &Value) -> Result<Value> {
    // (required, optional) parameter names
    let p = |required: &[&str], optional: &[&str]| Params::parse(name, params, required, optional);
    match name {
        "version" => p(&[], &[]).map(|_| json!(API_VERSION)),
        "requestPermission" => request_permission(ctx, &p(&["origin", "allowed"], &[])?),
        "apiReflect" => api_reflect(&p(&[], &["scopes", "actions"])?),
        "multi" => multi(ctx, &p(&["actions"], &[])?),
        "getProfiles" => p(&[], &[]).map(|_| profiles(ctx)),
        "getActiveProfile" => {
            p(&[], &[])?;
            Ok(json!(ctx.shell.open_profile_paths().map(|p| p.name)))
        }
        "reloadCollection" => {
            p(&[], &[])?;
            ctx.col()?;
            Ok(Value::Null)
        }

        "deckNames" => {
            p(&[], &[])?;
            Ok(json!(deck_names(ctx)?))
        }
        "deckNamesAndIds" => {
            p(&[], &[])?;
            let decks: Map<_, _> = all_decks(ctx)?
                .into_iter()
                .map(|d| (d.name, json!(d.id)))
                .collect();
            Ok(Value::Object(decks))
        }
        "deckNameFromId" => {
            deck_name_from_id(ctx, p(&["deckId"], &[])?.id("deckId")?).map(Value::from)
        }
        "getDeckStats" => deck_stats(ctx, &p(&["decks"], &[])?),
        "getDeckConfig" => deck_config(ctx, p(&["deck"], &[])?.str("deck")?),
        "createDeck" => deck_id_or_create(ctx, p(&["deck"], &[])?.str("deck")?).map(Value::from),
        "changeDeck" => {
            let p = p(&["cards", "deck"], &[])?;
            let deck_id = deck_id_or_create(ctx, p.str("deck")?)?;
            ctx.run(
                "set_deck",
                &anki_proto::cards::SetDeckRequest {
                    card_ids: p.ids("cards")?,
                    deck_id,
                },
            )?;
            Ok(Value::Null)
        }

        "modelNames" => {
            p(&[], &[])?;
            Ok(json!(notetype_names(ctx)?
                .into_iter()
                .map(|n| n.name)
                .collect::<Vec<_>>()))
        }
        "modelNamesAndIds" => {
            p(&[], &[])?;
            let models: Map<_, _> = notetype_names(ctx)?
                .into_iter()
                .map(|n| (n.name, json!(n.id)))
                .collect();
            Ok(Value::Object(models))
        }
        "modelNameFromId" => {
            let id = p(&["modelId"], &[])?.id("modelId")?;
            Ok(notetype_by_id(ctx, id)?["name"].clone())
        }
        "findModelsById" => {
            let ids = p(&["modelIds"], &[])?.ids("modelIds")?;
            let models: Result<Vec<_>> = ids
                .into_iter()
                .map(|id| Ok((*notetype_by_id(ctx, id)?).clone()))
                .collect();
            Ok(Value::Array(models?))
        }
        "findModelsByName" => {
            let p = p(&["modelNames"], &[])?;
            let models: Result<Vec<_>> = p
                .list("modelNames")?
                .iter()
                .map(|name| {
                    let name = name.as_str().unwrap_or_default();
                    Ok((*ctx.notetype_named(name)?).clone())
                })
                .collect();
            Ok(Value::Array(models?))
        }
        "modelFieldNames" => {
            let nt = ctx.notetype_named(p(&["modelName"], &[])?.str("modelName")?)?;
            Ok(json!(field_names(&nt)))
        }
        "modelFieldsOnTemplates" => {
            let nt = ctx.notetype_named(p(&["modelName"], &[])?.str("modelName")?)?;
            Ok(fields_on_templates(&nt))
        }
        "modelTemplates" => {
            let nt = ctx.notetype_named(p(&["modelName"], &[])?.str("modelName")?)?;
            let templates: Map<_, _> = templates(&nt)
                .map(|t| {
                    (
                        str_of(t, "name"),
                        json!({"Front": t["qfmt"], "Back": t["afmt"]}),
                    )
                })
                .collect();
            Ok(Value::Object(templates))
        }
        "modelStyling" => {
            let nt = ctx.notetype_named(p(&["modelName"], &[])?.str("modelName")?)?;
            Ok(json!({"css": nt["css"]}))
        }

        "findNotes" => notes::find_notes(ctx, &p(&[], &["query"])?),
        "notesInfo" => notes::notes_info(ctx, &p(&[], &["notes", "query"])?),
        "notesModTime" => notes::notes_mod_time(ctx, &p(&["notes"], &[])?),
        "addNote" => notes::add_note(ctx, p(&["note"], &[])?.object("note")?).map(Value::from),
        "addNotes" => notes::add_notes(ctx, &p(&["notes"], &[])?),
        "canAddNote" => Ok(json!(
            notes::can_add(ctx, p(&["note"], &[])?.get("note")).is_ok()
        )),
        "canAddNotes" => notes::can_add_notes(ctx, &p(&["notes"], &[])?, false),
        "canAddNoteWithErrorDetail" => {
            Ok(notes::can_add_detail(ctx, p(&["note"], &[])?.get("note")))
        }
        "canAddNotesWithErrorDetail" => notes::can_add_notes(ctx, &p(&["notes"], &[])?, true),
        "updateNoteFields" => {
            notes::update_note_fields(ctx, p(&["note"], &[])?.object("note")?).map(|_| Value::Null)
        }
        "updateNote" => {
            notes::update_note(ctx, p(&["note"], &[])?.object("note")?).map(|_| Value::Null)
        }
        "updateNoteTags" => {
            let p = p(&["note", "tags"], &[])?;
            notes::update_note_tags(ctx, p.id("note")?, p.get("tags")).map(|_| Value::Null)
        }
        "getNoteTags" => Ok(json!(
            notes::get_note(ctx, p(&["note"], &[])?.id("note")?)?.tags
        )),
        "deleteNotes" => {
            notes::delete_notes(ctx, p(&["notes"], &[])?.ids("notes")?).map(|_| Value::Null)
        }
        "addTags" => {
            let p = p(&["notes", "tags"], &["add"])?;
            notes::add_tags(ctx, &p, p.flag("add", true))
        }
        "removeTags" => notes::add_tags(ctx, &p(&["notes", "tags"], &[])?, false),
        "getTags" => {
            p(&[], &[])?;
            let tags: anki_proto::generic::StringList =
                ctx.call("all_tags", &anki_proto::generic::Empty {})?;
            Ok(json!(tags.vals))
        }

        "findCards" => cards::find_cards(ctx, &p(&[], &["query"])?),
        "cardsInfo" => cards::cards_info(ctx, &p(&["cards"], &[])?),
        "cardsModTime" => cards::cards_mod_time(ctx, &p(&["cards"], &[])?),
        "cardsToNotes" => cards::cards_to_notes(ctx, &p(&["cards"], &[])?),
        "suspend" => {
            let p = p(&["cards"], &["suspend"])?;
            cards::suspend(ctx, p.ids("cards")?, p.flag("suspend", true)).map(Value::from)
        }
        "unsuspend" => {
            cards::suspend(ctx, p(&["cards"], &[])?.ids("cards")?, false).map(|_| Value::Null)
        }
        "suspended" => cards::suspended(ctx, p(&["card"], &[])?.id("card")?).map(Value::from),
        "areSuspended" => cards::are_suspended(ctx, &p(&["cards"], &[])?),
        "setDueDate" => cards::set_due_date(ctx, &p(&["cards", "days"], &[])?),
        "forgetCards" => cards::forget_cards(ctx, &p(&["cards"], &[])?),
        "getReviewsOfCards" => cards::reviews_of_cards(ctx, &p(&["cards"], &[])?),

        "storeMediaFile" => media::store_media_file(
            ctx,
            &p(
                &["filename"],
                &["data", "path", "url", "skipHash", "deleteExisting"],
            )?,
        ),
        "retrieveMediaFile" => {
            media::retrieve_media_file(ctx, p(&["filename"], &[])?.str("filename")?)
        }
        "getMediaFilesNames" => {
            let p = p(&[], &["pattern"])?;
            media::media_file_names(ctx, p.opt_str("pattern")?.unwrap_or("*"))
        }
        "getMediaDirPath" => {
            p(&[], &[])?;
            media::media_dir_path(ctx)
        }
        "deleteMediaFile" => media::delete_media_file(ctx, p(&["filename"], &[])?.str("filename")?)
            .map(|_| Value::Null),

        _ if NOT_YET.contains(&name) => {
            Err(format!("{name} is not supported by the native shell yet").into())
        }
        _ => Err("unsupported action".into()),
    }
}

fn request_permission(ctx: &Ctx, p: &Params) -> Result<Value> {
    // The add-on asks the user about unknown origins; the shell has no
    // prompt yet, so they're denied.
    Ok(if p.flag("allowed", false) {
        json!({
            "permission": "granted",
            "requireApikey": ctx.config.api_key.as_deref().is_some_and(|k| !k.is_empty()),
            "version": API_VERSION,
        })
    } else {
        json!({"permission": "denied"})
    })
}

fn api_reflect(p: &Params) -> Result<Value> {
    let Some(scopes) = p.get("scopes").and_then(Value::as_array) else {
        return Err("scopes has invalid value".into());
    };
    let actions = match p.get("actions") {
        None => None,
        Some(Value::Array(actions)) => Some(actions),
        Some(_) => return Err("actions has invalid value".into()),
    };
    let mut out = Map::new();
    let mut out_scopes = vec![];
    if scopes.iter().any(|s| s == "actions") {
        let names: Vec<&str> = match actions {
            // like Python's dir(): sorted
            None => {
                let mut names = NATIVE.to_vec();
                names.sort_unstable();
                names
            }
            Some(actions) => actions
                .iter()
                .filter_map(Value::as_str)
                .filter(|a| NATIVE.contains(a))
                .collect(),
        };
        out_scopes.push("actions");
        out.insert("actions".into(), json!(names));
    }
    out.insert("scopes".into(), json!(out_scopes));
    Ok(Value::Object(out))
}

fn multi(ctx: &Ctx, p: &Params) -> Result<Value> {
    // each action replies in its own requested format, as the add-on's
    // `multi` runs each through `handler`
    Ok(Value::Array(
        p.list("actions")?
            .iter()
            .map(|action| handle_with(ctx.shell, ctx.config, action))
            .collect(),
    ))
}

/// The old Anki's profile list lives in `prefs21.db`; the profile folders
/// beside the open one are a close stand-in.
fn profiles(ctx: &Ctx) -> Value {
    let Some(open) = ctx.shell.open_profile_paths() else {
        return json!([]);
    };
    let Some(base) = open.folder.parent() else {
        return json!([open.name]);
    };
    let mut names: Vec<String> = std::fs::read_dir(base)
        .into_iter()
        .flatten()
        .flatten()
        .filter(|entry| entry.path().join("collection.anki2").exists())
        .filter_map(|entry| entry.file_name().into_string().ok())
        .collect();
    if !names.contains(&open.name) {
        names.push(open.name);
    }
    names.sort_unstable();
    json!(names)
}

pub(crate) fn all_decks(ctx: &Ctx) -> Result<Vec<DeckNameId>> {
    let names: anki_proto::decks::DeckNames = ctx.call(
        "get_deck_names",
        &anki_proto::decks::GetDeckNamesRequest {
            skip_empty_default: false,
            include_filtered: true,
        },
    )?;
    Ok(names.entries)
}

fn deck_names(ctx: &Ctx) -> Result<Vec<String>> {
    Ok(all_decks(ctx)?.into_iter().map(|d| d.name).collect())
}

pub(crate) fn deck_name_from_id(ctx: &Ctx, id: i64) -> Result<String> {
    ctx.deck_names()?
        .get(&id)
        .cloned()
        .ok_or_else(|| format!("deck was not found: {id}").into())
}

/// `decks.id_for_name()`.
pub(crate) fn deck_id(ctx: &Ctx, name: &str) -> Result<Option<i64>> {
    match ctx.call::<anki_proto::decks::DeckId>(
        "get_deck_id_by_name",
        &anki_proto::generic::String { val: name.into() },
    ) {
        Ok(id) => Ok(Some(id.did)),
        Err(err) if err.not_found => Ok(None),
        Err(err) => Err(err),
    }
}

/// `decks.id()`, which adds missing decks.
fn deck_id_or_create(ctx: &Ctx, name: &str) -> Result<i64> {
    if let Some(id) = deck_id(ctx, name)? {
        return Ok(id);
    }
    let mut deck: anki_proto::decks::Deck = ctx.call("new_deck", &anki_proto::generic::Empty {})?;
    deck.name = name.into();
    let added: anki_proto::collection::OpChangesWithId = ctx.call("add_deck", &deck)?;
    Ok(added.id)
}

fn deck_stats(ctx: &Ctx, p: &Params) -> Result<Value> {
    // The add-on looks names up with `decks.id()`, adding any that are
    // missing; a stats query shouldn't do that, so they're skipped.
    let mut wanted = vec![];
    for name in p.list("decks")? {
        if let Some(id) = deck_id(ctx, name.as_str().unwrap_or_default())? {
            wanted.push(id);
        }
    }
    let now = SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map_or(0, |d| d.as_secs() as i64);
    let tree: anki_proto::decks::DeckTreeNode =
        ctx.call("deck_tree", &anki_proto::decks::DeckTreeRequest { now })?;
    let mut out = Map::new();
    let mut stack = vec![&tree];
    while let Some(node) = stack.pop() {
        if wanted.contains(&node.deck_id) {
            out.insert(
                node.deck_id.to_string(),
                json!({
                    "deck_id": node.deck_id,
                    "name": node.name,
                    "new_count": node.new_count,
                    "learn_count": node.learn_count,
                    "review_count": node.review_count,
                    "total_in_deck": node.total_in_deck,
                }),
            );
        }
        stack.extend(node.children.iter().rev());
    }
    Ok(Value::Object(out))
}

/// `decks.config_dict_for_deck_id()`: the options group, or a filtered
/// deck's own settings.
fn deck_config(ctx: &Ctx, name: &str) -> Result<Value> {
    if !deck_names(ctx)?.iter().any(|n| n == name) {
        return Ok(json!(false));
    }
    let Some(did) = deck_id(ctx, name)? else {
        return Ok(json!(false));
    };
    let deck = legacy(ctx.call("get_deck_legacy", &anki_proto::decks::DeckId { did })?)?;
    let Some(conf_id) = deck.get("conf").and_then(Value::as_i64) else {
        return Ok(deck);
    };
    let config = |dcid| -> Result<Option<Value>> {
        match ctx.call(
            "get_deck_config_legacy",
            &anki_proto::deck_config::DeckConfigId { dcid },
        ) {
            Ok(json) => legacy(json).map(Some),
            Err(err) if err.not_found => Ok(None),
            Err(err) => Err(err),
        }
    };
    let mut conf = match config(conf_id)? {
        Some(conf) => conf,
        None => config(1)?.ok_or("default deck options are missing")?,
    };
    conf["dyn"] = json!(false);
    Ok(conf)
}

pub(crate) fn legacy(json: anki_proto::generic::Json) -> Result<Value> {
    serde_json::from_slice(&json.json).map_err(|e| Error::from(e.to_string()))
}

fn notetype_names(ctx: &Ctx) -> Result<Vec<anki_proto::notetypes::NotetypeNameId>> {
    let names: anki_proto::notetypes::NotetypeNames =
        ctx.call("get_notetype_names", &anki_proto::generic::Empty {})?;
    Ok(names.entries)
}

fn notetype_by_id(ctx: &Ctx, id: i64) -> Result<std::rc::Rc<Value>> {
    match ctx.notetype(id) {
        Err(err) if err.not_found => Err(format!("model was not found: {id}").into()),
        other => other,
    }
}

pub(crate) fn field_names(nt: &Value) -> Vec<String> {
    nt["flds"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|f| str_of(f, "name"))
        .collect()
}

fn templates(nt: &Value) -> impl Iterator<Item = &Value> {
    nt["tmpls"].as_array().into_iter().flatten()
}

fn str_of(value: &Value, key: &str) -> String {
    value[key].as_str().unwrap_or_default().to_string()
}

/// The add-on's take on `aqt.clayout`'s field detection.
fn fields_on_templates(nt: &Value) -> Value {
    static FIELD: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\{\{[^#/}]+?\}\}").unwrap());
    let mut out = Map::new();
    for template in templates(nt) {
        let mut sides: Vec<Vec<String>> = vec![];
        for side in ["qfmt", "afmt"] {
            let mut fields = vec![];
            for m in FIELD.find_iter(template[side].as_str().unwrap_or_default()) {
                let name: String = m.as_str().chars().filter(|c| !"{}".contains(*c)).collect();
                let name = name.rsplit(':').next().unwrap_or_default().to_string();
                if name == "FrontSide" || side == "afmt" && sides[0].contains(&name) {
                    continue;
                }
                fields.push(name);
            }
            sides.push(fields);
        }
        out.insert(str_of(template, "name"), json!(sides));
    }
    Value::Object(out)
}
