// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Note and tag actions.

use std::collections::HashMap;

use anki::text::strip_html_preserving_media_filenames;
use anki_proto::notes::Note;
use anki_proto::notes::NoteId;
use serde_json::json;
use serde_json::Map;
use serde_json::Value;
use sha1::Digest;
use sha1::Sha1;

use super::actions;
use super::media;
use super::params::invalid;
use super::params::py_repr;
use super::params::Params;
use super::Ctx;
use super::Error;
use super::Result;

/// The `in (...)` batch size: SQLite's default variable limit.
const BATCH: usize = 999;

pub(crate) fn find_notes(ctx: &Ctx, p: &Params) -> Result<Value> {
    match p.opt_str("query")? {
        None => Ok(json!([])),
        Some(query) => Ok(json!(search(ctx, "search_notes", query)?)),
    }
}

pub(crate) fn search(ctx: &Ctx, method: &str, query: &str) -> Result<Vec<i64>> {
    let out: anki_proto::search::SearchResponse = ctx.call(
        method,
        &anki_proto::search::SearchRequest {
            search: query.into(),
            order: None,
        },
    )?;
    Ok(out.ids)
}

/// `getNote()`, with the add-on's message for missing notes.
pub(crate) fn get_note(ctx: &Ctx, nid: i64) -> Result<Note> {
    ctx.call("get_note", &NoteId { nid }).map_err(|err| {
        if err.not_found {
            Error::not_found(format!("Note was not found: {nid}"))
        } else {
            err
        }
    })
}

/// `{name: {"value", "order"}}` for a note's fields.
pub(crate) fn fields_json(ctx: &Ctx, note: &Note) -> Result<Value> {
    let nt = ctx.notetype(note.notetype_id)?;
    let mut fields = Map::new();
    for field in nt["flds"].as_array().into_iter().flatten() {
        let order = field["ord"].as_u64().unwrap_or_default() as usize;
        fields.insert(
            field["name"].as_str().unwrap_or_default().into(),
            json!({
                "value": note.fields.get(order).cloned().unwrap_or_default(),
                "order": order,
            }),
        );
    }
    Ok(Value::Object(fields))
}

pub(crate) fn notes_info(ctx: &Ctx, p: &Params) -> Result<Value> {
    let nids = match (p.opt_ids("notes")?, p.opt_str("query")?) {
        (_, Some(query)) => search(ctx, "search_notes", query)?,
        (Some(nids), None) => nids,
        (None, None) => return Err("Must provide either \"notes\" or a \"query\"".into()),
    };
    let mut cards: HashMap<i64, Vec<Value>> = HashMap::new();
    for batch in nids.chunks(BATCH) {
        let sql = format!(
            "select id, nid from cards where nid in ({}) order by ord",
            vec!["?"; batch.len()].join(",")
        );
        let args: Vec<Value> = batch.iter().map(|&nid| json!(nid)).collect();
        for row in ctx.query(&sql, &args)? {
            if let [cid, nid] = row.as_slice() {
                cards
                    .entry(nid.as_i64().unwrap_or_default())
                    .or_default()
                    .push(cid.clone());
            }
        }
    }
    let profile = ctx.shell.open_profile_paths().map(|p| p.name);
    let mut out = vec![];
    for nid in nids {
        let note = match get_note(ctx, nid) {
            Ok(note) => note,
            Err(err) if err.not_found => {
                out.push(json!({}));
                continue;
            }
            Err(err) => return Err(err),
        };
        let nt = ctx.notetype(note.notetype_id)?;
        out.push(json!({
            "noteId": note.id,
            "profile": profile,
            "tags": note.tags,
            "fields": fields_json(ctx, &note)?,
            "modelName": nt["name"],
            "mod": note.mtime_secs,
            "cards": cards.remove(&nid).unwrap_or_default(),
        }));
    }
    Ok(Value::Array(out))
}

pub(crate) fn notes_mod_time(ctx: &Ctx, p: &Params) -> Result<Value> {
    let mut out = vec![];
    for nid in p.ids("notes")? {
        match get_note(ctx, nid) {
            Ok(note) => out.push(json!({"noteId": note.id, "mod": note.mtime_secs})),
            Err(err) if err.not_found => out.push(json!({})),
            Err(err) => return Err(err),
        }
    }
    Ok(Value::Array(out))
}

/// Python's `note[key]` on the request's note dict.
fn key<'a>(note: &'a Map<String, Value>, key: &str) -> Result<&'a Value> {
    note.get(key).ok_or_else(|| format!("'{key}'").into())
}

fn str_key<'a>(note: &'a Map<String, Value>, name: &str) -> Result<&'a str> {
    key(note, name)?
        .as_str()
        .ok_or_else(|| invalid(name, "a string"))
}

fn field_value<'a>(name: &str, value: &'a Value) -> Result<&'a str> {
    value
        .as_str()
        .ok_or_else(|| invalid(&format!("fields.{name}"), "a string"))
}

fn tag_list(value: &Value) -> Result<Vec<String>> {
    match value {
        Value::String(tags) => Ok(tags.split_whitespace().map(Into::into).collect()),
        Value::Array(tags) => tags
            .iter()
            .map(|t| {
                t.as_str()
                    .map(Into::into)
                    .ok_or_else(|| invalid("tags", "strings"))
            })
            .collect(),
        _ => Err(invalid("tags", "a list of strings")),
    }
}

/// The add-on's `createNote`: a new note ready to add to the returned deck,
/// or why it can't be added. Like the add-on, this stores any media the
/// note refers to.
fn create_note(ctx: &Ctx, note: &Map<String, Value>) -> Result<(Note, i64)> {
    let model_name = str_key(note, "modelName")?;
    let nt = ctx
        .notetype_by_name(model_name)?
        .ok_or_else(|| format!("model was not found: {model_name}"))?;
    let deck_name = str_key(note, "deckName")?;
    let deck_id = actions::deck_id(ctx, deck_name)?
        .ok_or_else(|| format!("deck was not found: {deck_name}"))?;

    let mut new: Note = ctx.call(
        "new_note",
        &anki_proto::notetypes::NotetypeId {
            ntid: nt["id"].as_i64().unwrap_or_default(),
        },
    )?;
    if let Some(tags) = note.get("tags") {
        new.tags = tag_list(tags)?;
    }
    let names = actions::field_names(&nt);
    let fields = key(note, "fields")?
        .as_object()
        .ok_or_else(|| invalid("fields", "an object"))?;
    for (name, value) in fields {
        if let Some(idx) = names
            .iter()
            .position(|n| n.to_lowercase() == name.to_lowercase())
        {
            new.fields[idx] = field_value(name, value)?.into();
        }
    }
    media::add_media_from_note(ctx, &mut new.fields, &names, note);

    let options = note.get("options").and_then(Value::as_object);
    let option = |name: &str| options.and_then(|o| o.get(name));
    let scope_options = option("duplicateScopeOptions").and_then(Value::as_object);
    let scope_option = |name: &str| scope_options.and_then(|o| o.get(name));
    let bool_option = |value: Option<&Value>, message: &str| -> Result<bool> {
        match value {
            None => Ok(false),
            Some(Value::Bool(b)) => Ok(*b),
            Some(_) => Err(message.into()),
        }
    };
    let allow_duplicate = bool_option(
        option("allowDuplicate"),
        "option parameter \"allowDuplicate\" must be boolean",
    )?;
    let check_children = bool_option(
        scope_option("checkChildren"),
        "option parameter \"duplicateScopeOptions.checkChildren\" must be boolean",
    )?;
    let check_all_models = bool_option(
        scope_option("checkAllModels"),
        "option parameter \"duplicateScopeOptions.checkAllModels\" must be boolean",
    )?;
    let deck_scope = option("duplicateScope").and_then(Value::as_str) == Some("deck");
    let scope_deck = scope_option("deckName").and_then(Value::as_str);

    let state = if !deck_scope && !check_all_models {
        let check: anki_proto::notes::NoteFieldsCheckResponse =
            ctx.call("note_fields_check", &new)?;
        check.state
    } else {
        duplicate_or_empty_in_scope(
            ctx,
            &new,
            deck_scope.then_some(deck_id),
            scope_deck,
            check_children,
            check_all_models,
        )?
    };
    match state {
        0 => Ok((new, deck_id)),
        1 => Err("cannot create note because it is empty".into()),
        2 if allow_duplicate => Ok((new, deck_id)),
        2 => Err("cannot create note because it is a duplicate".into()),
        _ => Err("cannot create note for unknown reason".into()),
    }
}

/// The add-on's `isNoteDuplicateOrEmptyInScope`: 1 if the first field is
/// empty, 2 if it's a duplicate in scope, 0 otherwise.
fn duplicate_or_empty_in_scope(
    ctx: &Ctx,
    note: &Note,
    deck: Option<i64>,
    scope_deck: Option<&str>,
    check_children: bool,
    check_all_models: bool,
) -> Result<i32> {
    let first = note.fields.first().map(String::as_str).unwrap_or_default();
    if first.trim().is_empty() {
        return Ok(1);
    }
    let decks = match deck {
        None => None,
        Some(mut did) => {
            if let Some(name) = scope_deck {
                match actions::deck_id(ctx, name)? {
                    Some(id) => did = id,
                    // an unknown deck can't hold a duplicate
                    None => return Ok(0),
                }
            }
            let mut dids = vec![did];
            if check_children {
                let names = ctx.deck_names()?;
                if let Some(parent) = names.get(&did) {
                    let prefix = format!("{parent}::");
                    dids.extend(
                        names
                            .iter()
                            .filter(|(_, name)| name.starts_with(&prefix))
                            .map(|(id, _)| *id),
                    );
                }
            }
            Some(dids)
        }
    };
    let mut sql = "select id from notes where csum=?".to_string();
    let mut args = vec![json!(field_checksum(first))];
    if note.id != 0 {
        sql += " and id!=?";
        args.push(json!(note.id));
    }
    if !check_all_models {
        sql += " and mid=?";
        args.push(json!(note.notetype_id));
    }
    for row in ctx.query(&sql, &args)? {
        let Some(decks) = &decks else {
            return Ok(2);
        };
        let nid = row.first().cloned().unwrap_or_default();
        for card in ctx.query("select did from cards where nid=?", &[nid])? {
            if let Some(did) = card.first().and_then(Value::as_i64) {
                if decks.contains(&did) {
                    return Ok(2);
                }
            }
        }
    }
    Ok(0)
}

/// `anki.utils.field_checksum()`, as stored in the notes table.
fn field_checksum(text: &str) -> u32 {
    let digest = Sha1::digest(strip_html_preserving_media_filenames(text).as_bytes());
    u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]])
}

pub(crate) fn add_note(ctx: &Ctx, note: &Map<String, Value>) -> Result<i64> {
    let (note, deck_id) = create_note(ctx, note)?;
    let added: anki_proto::notes::AddNoteResponse = ctx.call(
        "add_note",
        &anki_proto::notes::AddNoteRequest {
            note: Some(note),
            deck_id,
        },
    )?;
    if added.changes.map_or(0, |c| c.count) < 1 {
        return Err(
            "The field values you have provided would make an empty question on all cards.".into(),
        );
    }
    Ok(added.note_id)
}

pub(crate) fn add_notes(ctx: &Ctx, p: &Params) -> Result<Value> {
    let mut added = vec![];
    let mut errors = vec![];
    for note in p.list("notes")? {
        let result = match note.as_object() {
            Some(note) => add_note(ctx, note),
            None => Err(invalid("notes", "a list of objects")),
        };
        match result {
            Ok(nid) => added.push(nid),
            Err(err) => errors.push(json!(err.message)),
        }
    }
    if !errors.is_empty() {
        // all or nothing
        delete_notes(ctx, added)?;
        return Err(py_repr(&Value::Array(errors)).into());
    }
    Ok(json!(added))
}

pub(crate) fn can_add(ctx: &Ctx, note: Option<&Value>) -> Result<()> {
    let note = note
        .and_then(Value::as_object)
        .ok_or_else(|| invalid("note", "an object"))?;
    create_note(ctx, note).map(|_| ())
}

pub(crate) fn can_add_detail(ctx: &Ctx, note: Option<&Value>) -> Value {
    match can_add(ctx, note) {
        Ok(()) => json!({"canAdd": true}),
        Err(err) => json!({"canAdd": false, "error": err.message}),
    }
}

pub(crate) fn can_add_notes(ctx: &Ctx, p: &Params, detail: bool) -> Result<Value> {
    Ok(Value::Array(
        p.list("notes")?
            .iter()
            .map(|note| {
                if detail {
                    can_add_detail(ctx, Some(note))
                } else {
                    json!(can_add(ctx, Some(note)).is_ok())
                }
            })
            .collect(),
    ))
}

pub(crate) fn update_note_fields(ctx: &Ctx, note: &Map<String, Value>) -> Result<()> {
    let nid = key(note, "id")?
        .as_i64()
        .ok_or_else(|| invalid("id", "an integer"))?;
    let mut existing = get_note(ctx, nid)?;
    let names = actions::field_names(&*ctx.notetype(existing.notetype_id)?);
    let fields = key(note, "fields")?
        .as_object()
        .ok_or_else(|| invalid("fields", "an object"))?;
    for (name, value) in fields {
        if let Some(idx) = names.iter().position(|n| n == name) {
            existing.fields[idx] = field_value(name, value)?.into();
        }
    }
    media::add_media_from_note(ctx, &mut existing.fields, &names, note);
    ctx.run(
        "update_notes",
        &anki_proto::notes::UpdateNotesRequest {
            notes: vec![existing],
            skip_undo_entry: true,
        },
    )
}

pub(crate) fn update_note(ctx: &Ctx, note: &Map<String, Value>) -> Result<()> {
    let mut updated = false;
    if note.contains_key("fields") {
        update_note_fields(ctx, note)?;
        updated = true;
    }
    if let Some(tags) = note.get("tags") {
        let nid = key(note, "id")?
            .as_i64()
            .ok_or_else(|| invalid("id", "an integer"))?;
        update_note_tags(ctx, nid, Some(tags))?;
        updated = true;
    }
    if !updated {
        return Err("Must provide a \"fields\" or \"tags\" property.".into());
    }
    Ok(())
}

pub(crate) fn update_note_tags(ctx: &Ctx, nid: i64, tags: Option<&Value>) -> Result<()> {
    let tags: Vec<&str> = match tags {
        Some(Value::String(tag)) => vec![tag],
        Some(Value::Array(tags)) => tags
            .iter()
            .map(Value::as_str)
            .collect::<Option<_>>()
            .ok_or("Must provide tags as a list of strings")?,
        _ => return Err("Must provide tags as a list of strings".into()),
    };
    // one tag at a time, as the add-on does, so the backend's tag matching
    // and case handling apply the same way
    for old in get_note(ctx, nid)?.tags {
        bulk_tags(ctx, "remove_note_tags", vec![nid], old)?;
    }
    for new in tags {
        bulk_tags(ctx, "add_note_tags", vec![nid], new.into())?;
    }
    Ok(())
}

fn bulk_tags(ctx: &Ctx, method: &str, note_ids: Vec<i64>, tags: String) -> Result<()> {
    ctx.run(
        method,
        &anki_proto::tags::NoteIdsAndTagsRequest { note_ids, tags },
    )
}

pub(crate) fn add_tags(ctx: &Ctx, p: &Params, add: bool) -> Result<Value> {
    let tags = match p.get("tags") {
        Some(Value::String(tags)) => tags.clone(),
        Some(tags @ Value::Array(_)) => tag_list(tags)?.join(" "),
        _ => return Err(invalid("tags", "a string")),
    };
    let method = if add {
        "add_note_tags"
    } else {
        "remove_note_tags"
    };
    bulk_tags(ctx, method, p.ids("notes")?, tags)?;
    Ok(Value::Null)
}

pub(crate) fn delete_notes(ctx: &Ctx, note_ids: Vec<i64>) -> Result<()> {
    ctx.run(
        "remove_notes",
        &anki_proto::notes::RemoveNotesRequest {
            note_ids,
            card_ids: vec![],
        },
    )
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn checksum_matches_rslib() {
        // rslib's field_checksum() test values
        assert_eq!(field_checksum("test"), 2840236005);
        assert_eq!(field_checksum("今日"), 1464653051);
        assert_eq!(field_checksum("<b>test</b>"), 2840236005);
    }
}
