// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Card, scheduling and review-log actions.

use std::collections::HashMap;

use anki_proto::backend::backend_error::Kind;
use anki_proto::card_rendering::rendered_template_node::Value as Node;
use anki_proto::card_rendering::RenderedTemplateNode;
use anki_proto::cards::Card;
use anki_proto::cards::CardId;
use serde_json::json;
use serde_json::Map;
use serde_json::Value;

use super::actions;
use super::notes;
use super::params::Params;
use super::Ctx;
use super::Error;
use super::Result;

const BATCH: usize = 999;

pub(crate) fn find_cards(ctx: &Ctx, p: &Params) -> Result<Value> {
    match p.opt_str("query")? {
        None => Ok(json!([])),
        Some(query) => Ok(json!(notes::search(ctx, "search_cards", query)?)),
    }
}

/// `getCard()`, with the add-on's message for missing cards.
fn get_card(ctx: &Ctx, cid: i64) -> Result<Card> {
    ctx.call("get_card", &CardId { cid }).map_err(|err| {
        if err.not_found {
            Error::not_found(format!("Card was not found: {cid}"))
        } else {
            err
        }
    })
}

/// Run `f` per card, with `{}` for missing cards (or notes) so the output
/// lines up with the input.
fn per_card(ctx: &Ctx, p: &Params, f: impl Fn(Card) -> Result<Value>) -> Result<Value> {
    let mut out = vec![];
    for cid in p.ids("cards")? {
        match get_card(ctx, cid).and_then(&f) {
            Ok(value) => out.push(value),
            Err(err) if err.not_found => out.push(json!({})),
            Err(err) => return Err(err),
        }
    }
    Ok(Value::Array(out))
}

pub(crate) fn cards_info(ctx: &Ctx, p: &Params) -> Result<Value> {
    per_card(ctx, p, |card| {
        let note = notes::get_note(ctx, card.note_id)?;
        let nt = ctx.notetype(note.notetype_id)?;
        let states: anki_proto::scheduler::SchedulingStates =
            ctx.call("get_scheduling_states", &CardId { cid: card.id })?;
        let next: anki_proto::generic::StringList = ctx.call("describe_next_states", &states)?;
        let (question, answer) = render(ctx, card.id)?;
        Ok(json!({
            "cardId": card.id,
            "fields": notes::fields_json(ctx, &note)?,
            "fieldOrder": card.template_idx,
            "question": question,
            "answer": answer,
            "modelName": nt["name"],
            "ord": card.template_idx,
            "deckName": actions::deck_name_from_id(ctx, card.deck_id)?,
            "css": nt["css"],
            // ten times the ease percentage
            "factor": card.ease_factor,
            "interval": card.interval,
            "note": card.note_id,
            "type": card.ctype,
            "queue": card.queue,
            "due": card.due,
            "reps": card.reps,
            "lapses": card.lapses,
            "left": card.remaining_steps,
            "mod": card.mtime_secs,
            "nextReviews": next.vals,
            "flags": card.flags,
        }))
    })
}

/// `card.question()` / `card.answer()`: pylib's render, with style, and AV
/// tags as `[anki:play:...]` references.
fn render(ctx: &Ctx, cid: i64) -> Result<(String, String)> {
    let out = match ctx
        .col()?
        .call::<anki_proto::card_rendering::RenderCardResponse>(
            "render_existing_card",
            &anki_proto::card_rendering::RenderExistingCardRequest {
                card_id: cid,
                browser: false,
                // as pylib, which fills in FrontSide itself
                partial_render: true,
            },
        ) {
        Ok(out) => out,
        // pylib shows template errors in place of both sides, without css
        Err(err) if err.kind == Kind::TemplateParse as i32 => {
            let text = format!("<style></style>{}", err.message);
            return Ok((text.clone(), text));
        }
        Err(err) => return Err(err.into()),
    };
    let question = extract_av_tags(ctx, nodes_to_text(&out.question_nodes, None), true)?;
    let answer = extract_av_tags(
        ctx,
        nodes_to_text(&out.answer_nodes, Some(&question)),
        false,
    )?;
    let style = format!("<style>{}</style>", out.css);
    Ok((format!("{style}{question}"), format!("{style}{answer}")))
}

/// pylib's `apply_custom_filters()`, without any add-on filters.
fn nodes_to_text(nodes: &[RenderedTemplateNode], front_side: Option<&str>) -> String {
    let mut out = String::new();
    for node in nodes {
        match &node.value {
            Some(Node::Text(text)) => out += text,
            Some(Node::Replacement(r)) => match front_side {
                Some(front) if r.field_name == "FrontSide" => out += front,
                _ => out += &r.current_text,
            },
            None => {}
        }
    }
    out
}

fn extract_av_tags(ctx: &Ctx, text: String, question_side: bool) -> Result<String> {
    let out: anki_proto::card_rendering::ExtractAvTagsResponse = ctx.call(
        "extract_av_tags",
        &anki_proto::card_rendering::ExtractAvTagsRequest {
            text,
            question_side,
        },
    )?;
    Ok(out.text)
}

pub(crate) fn cards_mod_time(ctx: &Ctx, p: &Params) -> Result<Value> {
    per_card(ctx, p, |card| {
        Ok(json!({"cardId": card.id, "mod": card.mtime_secs}))
    })
}

pub(crate) fn cards_to_notes(ctx: &Ctx, p: &Params) -> Result<Value> {
    let ids: Vec<String> = p.ids("cards")?.iter().map(i64::to_string).collect();
    let sql = format!(
        "select distinct nid from cards where id in ({})",
        ids.join(",")
    );
    Ok(Value::Array(
        ctx.query(&sql, &[])?
            .into_iter()
            .filter_map(|row| row.into_iter().next())
            .collect(),
    ))
}

pub(crate) fn suspended(ctx: &Ctx, cid: i64) -> Result<bool> {
    Ok(get_card(ctx, cid)?.queue == -1)
}

pub(crate) fn are_suspended(ctx: &Ctx, p: &Params) -> Result<Value> {
    let mut out = vec![];
    for cid in p.ids("cards")? {
        match suspended(ctx, cid) {
            Ok(s) => out.push(json!(s)),
            Err(err) if err.not_found => out.push(Value::Null),
            Err(err) => return Err(err),
        }
    }
    Ok(Value::Array(out))
}

/// Whether any card changed state.
pub(crate) fn suspend(ctx: &Ctx, mut cards: Vec<i64>, suspend: bool) -> Result<bool> {
    // The add-on removes already-done cards while iterating over the list,
    // which skips the card after each removal; that's kept, since it decides
    // the result.
    let mut i = 0;
    while i < cards.len() {
        let cid = cards[i];
        if suspended(ctx, cid)? == suspend {
            if let Some(pos) = cards.iter().position(|&c| c == cid) {
                cards.remove(pos);
            }
        }
        i += 1;
    }
    if cards.is_empty() {
        return Ok(false);
    }
    if suspend {
        ctx.run(
            "bury_or_suspend_cards",
            &anki_proto::scheduler::BuryOrSuspendCardsRequest {
                card_ids: cards,
                note_ids: vec![],
                mode: anki_proto::scheduler::bury_or_suspend_cards_request::Mode::Suspend as i32,
            },
        )?;
    } else {
        ctx.run(
            "restore_buried_and_suspended_cards",
            &anki_proto::cards::CardIds { cids: cards },
        )?;
    }
    Ok(true)
}

pub(crate) fn set_due_date(ctx: &Ctx, p: &Params) -> Result<Value> {
    let days = match p.get("days") {
        Some(Value::String(days)) => days.clone(),
        Some(Value::Number(days)) => days.to_string(),
        _ => return Err(super::params::invalid("days", "a string")),
    };
    ctx.run(
        "set_due_date",
        &anki_proto::scheduler::SetDueDateRequest {
            card_ids: p.ids("cards")?,
            days,
            config_key: None,
        },
    )?;
    Ok(json!(true))
}

pub(crate) fn forget_cards(ctx: &Ctx, p: &Params) -> Result<Value> {
    ctx.run(
        "schedule_cards_as_new",
        &anki_proto::scheduler::ScheduleCardsAsNewRequest {
            card_ids: p.ids("cards")?,
            log: true,
            restore_position: true,
            reset_counts: false,
            context: None,
        },
    )?;
    Ok(Value::Null)
}

pub(crate) fn reviews_of_cards(ctx: &Ctx, p: &Params) -> Result<Value> {
    const COLUMNS: [&str; 9] = [
        "cid", "id", "usn", "ease", "ivl", "lastIvl", "factor", "time", "type",
    ];
    let cards = p.ids("cards")?;
    let mut reviews: HashMap<i64, Vec<Value>> = HashMap::new();
    for batch in cards.chunks(BATCH) {
        let sql = format!(
            "select {} from revlog where cid in ({})",
            COLUMNS.join(", "),
            vec!["?"; batch.len()].join(",")
        );
        let args: Vec<Value> = batch.iter().map(|&cid| json!(cid)).collect();
        for row in ctx.query(&sql, &args)? {
            let cid = row.first().and_then(Value::as_i64).unwrap_or_default();
            let review: Map<_, _> = COLUMNS[1..]
                .iter()
                .map(|c| c.to_string())
                .zip(row.into_iter().skip(1))
                .collect();
            reviews.entry(cid).or_default().push(Value::Object(review));
        }
    }
    let out: Map<_, _> = cards
        .iter()
        .map(|cid| {
            (
                cid.to_string(),
                Value::Array(reviews.get(cid).cloned().unwrap_or_default()),
            )
        })
        .collect();
    Ok(Value::Object(out))
}
