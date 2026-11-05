// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The reviewer driver, `qt/aqt/reviewer.py` in Rust (v3 scheduler only).
//!
//! It drives the stock `reviewer.js` page plus the bottom bar
//! (`reviewer-bottom.js`, in an iframe) and knows nothing about windows:
//! each step returns [Effect]s for the app to carry out, so it can be tested
//! against a collection without a webview.

use std::time::Duration;
use std::time::Instant;

use anki::prelude::I18n;
use anki_proto::card_rendering::av_tag::Value as AvValue;
use anki_proto::card_rendering::AvTag;
use anki_proto::cards::Card;
use anki_proto::collection::OpChanges;
use anki_proto::collection::OpChangesWithCount;
use anki_proto::deck_config::deck_config::config::AnswerAction;
use anki_proto::deck_config::deck_config::config::QuestionAction;
use anki_proto::deck_config::deck_config::Config as DeckConf;
use anki_proto::scheduler::bury_or_suspend_cards_request::Mode as BuryMode;
use anki_proto::scheduler::card_answer::Rating;
use anki_proto::scheduler::queued_cards::Queue;
use anki_proto::scheduler::CardAnswer;
use anki_proto::scheduler::QueuedCards;
use anki_proto::scheduler::SchedulingContext;
use anki_proto::scheduler::SchedulingStates;
use serde_json::json;
use serde_json::Value;

use crate::api::Api;
use crate::api::ApiError;
use crate::card_text;
use crate::card_text::RenderedCard;
use crate::card_text::TypeAnswer;

pub const MARKED_TAG: &str = "marked";

/// What the app should do after a reviewer step.
#[derive(Debug, Clone, PartialEq)]
pub enum Effect {
    /// Run in the reviewer page's main frame. Bottom bar calls go through
    /// `_shellBottomCall`, audio through `_shellAv`.
    Js(String),
    /// Load the reviewer page (before the first card).
    LoadPage,
    /// No cards left: back to the overview (which shows congrats).
    Overview,
    DeckBrowser,
    Tooltip(String),
    EditNote(i64),
    CardInfo(i64),
    DeckOptions(i64),
    /// Show the "More" menu.
    ContextMenu,
    /// Call [Reviewer::auto_advance_due] with `seq` after `after`.
    Timer {
        seq: u64,
        after: Duration,
    },
    /// Ask whether to continue after the timebox (cards, minutes).
    Timebox {
        cards: u32,
        minutes: u32,
    },
    /// The undo/redo state changed (menus).
    UndoChanged,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Question,
    Answer,
    /// Answering; waiting for the next card.
    Transition,
}

struct Current {
    card: Card,
    queue: Queue,
    counts: [u32; 3],
    /// What the answer buttons use: [Self::queue_states], changed by the
    /// custom scheduling code if there is any.
    states: SchedulingStates,
    /// As the queue gave them; custom scheduling starts from these each
    /// time the question is shown.
    queue_states: SchedulingStates,
    context: SchedulingContext,
    started: Instant,
    text: RenderedCard,
    conf: DeckConf,
    type_answer: TypeAnswer,
    typed: String,
    marked: bool,
    /// The note's mtime when shown, to tell whether an edit changed it.
    note_mtime: i64,
}

/// Collection-level review settings, read when review starts.
#[derive(Debug, Clone, Default)]
struct Prefs {
    hide_play_buttons: bool,
    interrupt_audio: bool,
    due_counts: bool,
    intervals_on_buttons: bool,
    timebox_secs: u32,
}

pub struct Reviewer {
    tr: I18n,
    side: Side,
    current: Option<Current>,
    previous_card: Option<i64>,
    prefs: Prefs,
    page_loaded: bool,
    mutation_key: String,
    mutation_js: String,
    states_mutated: bool,
    /// The ease buttons wait for the custom scheduling JS.
    ease_buttons_pending: bool,
    pub auto_advance: bool,
    seq: u64,
    timebox: Option<(Instant, u32)>,
    reps: u32,
    pub night_mode: bool,
    /// Space/Enter answer Good on the answer side (the old Anki's
    /// preference).
    pub spacebar_rates_card: bool,
    media_dir: Option<std::path::PathBuf>,
}

fn js(s: impl AsRef<str>) -> String {
    serde_json::to_string(s.as_ref()).unwrap()
}

fn bottom(call: &str, args: &[Value]) -> Effect {
    let args: Vec<String> = args.iter().map(Value::to_string).collect();
    Effect::Js(format!(
        "_shellBottomCall({}, {});",
        js(call),
        format_args!("[{}]", args.join(","))
    ))
}

impl Default for Reviewer {
    fn default() -> Self {
        Reviewer {
            tr: I18n::new(&["en"]),
            side: Side::Transition,
            current: None,
            previous_card: None,
            prefs: Prefs::default(),
            page_loaded: false,
            mutation_key: format!("{:x}", rand::random::<u64>()),
            mutation_js: String::new(),
            states_mutated: true,
            ease_buttons_pending: false,
            auto_advance: false,
            seq: 0,
            timebox: None,
            reps: 0,
            night_mode: false,
            spacebar_rates_card: true,
            media_dir: None,
        }
    }
}

impl Reviewer {
    pub fn side(&self) -> Option<Side> {
        self.current.as_ref().map(|_| self.side)
    }

    pub fn card_id(&self) -> Option<i64> {
        self.current.as_ref().map(|c| c.card.id)
    }

    pub fn note_id(&self) -> Option<i64> {
        self.current.as_ref().map(|c| c.card.note_id)
    }

    pub fn previous_card_id(&self) -> Option<i64> {
        self.previous_card
    }

    pub fn flag(&self) -> u32 {
        self.current.as_ref().map_or(0, |c| c.card.flags & 0b111)
    }

    pub fn marked(&self) -> bool {
        self.current.as_ref().is_some_and(|c| c.marked)
    }

    /// `Reviewer.show` plus `col.startTimebox()` (done on "study").
    pub fn start(&mut self, api: &Api, media_dir: std::path::PathBuf) -> Vec<Effect> {
        self.prefs = read_prefs(api);
        self.media_dir = Some(media_dir);
        self.mutation_js = api
            .call::<anki_proto::generic::String>(
                "get_config_string",
                &anki_proto::config::GetConfigStringRequest {
                    key: anki_proto::config::config_key::String::CardStateCustomizer as i32,
                },
            )
            .map(|s| s.val)
            .unwrap_or_default();
        self.page_loaded = false;
        self.current = None;
        self.previous_card = None;
        self.timebox = Some((Instant::now(), 0));
        self.reps = 0;
        self.next_card(api)
    }

    /// Leaving the reviewer.
    pub fn stop(&mut self) -> Vec<Effect> {
        self.current = None;
        self.auto_advance = false;
        self.seq += 1;
        vec![Effect::Js("_shellAv.stop();".into())]
    }

    /// The page reports it has loaded (its `domDone`).
    pub fn page_ready(&mut self, api: &Api) -> Vec<Effect> {
        self.page_loaded = true;
        if self.current.is_some() {
            self.show_question(api)
        } else {
            vec![]
        }
    }

    pub fn next_card(&mut self, api: &Api) -> Vec<Effect> {
        if let Some(c) = &self.current {
            self.previous_card = Some(c.card.id);
        }
        self.current = None;
        self.seq += 1;
        match self.fetch(api) {
            Ok(Some(current)) => self.current = Some(current),
            Ok(None) => return vec![Effect::Js("_shellAv.stop();".into()), Effect::Overview],
            Err(err) => {
                tracing::error!(%err, "fetching the next card failed");
                return vec![Effect::Tooltip(err.message), Effect::Overview];
            }
        }
        if !self.page_loaded {
            return vec![Effect::LoadPage];
        }
        self.show_question(api)
    }

    /// The memory bench (`crate::bench`): the reviewer page for showing
    /// given cards, as `shell/harness/stock/cycle.py` drives the Qt one.
    pub fn start_for_bench(&mut self, api: &Api, media_dir: std::path::PathBuf) -> Vec<Effect> {
        self.prefs = read_prefs(api);
        self.media_dir = Some(media_dir);
        self.page_loaded = false;
        self.current = None;
        vec![Effect::LoadPage]
    }

    /// Show `card_id`'s question, whatever the queue says (the bench).
    pub fn show_card(&mut self, api: &Api, card_id: i64) -> Vec<Effect> {
        let current = (|| -> Result<Current, ApiError> {
            let card: Card = api.call("get_card", &anki_proto::cards::CardId { cid: card_id })?;
            let states: SchedulingStates = api.call(
                "get_scheduling_states",
                &anki_proto::cards::CardId { cid: card_id },
            )?;
            let (marked, note_mtime) = note_info(api, card.note_id)?;
            Ok(Current {
                queue: Queue::Review,
                counts: [0, 0, 0],
                queue_states: states.clone(),
                states,
                note_mtime,
                context: SchedulingContext::default(),
                started: Instant::now(),
                text: self.render(api, &card)?,
                conf: deck_conf(api, current_deck_id(&card))?,
                type_answer: TypeAnswer::default(),
                typed: String::new(),
                marked,
                card,
            })
        })();
        match current {
            Ok(current) => {
                self.current = Some(current);
                self.show_question(api)
            }
            Err(err) => vec![Effect::Tooltip(err.message)],
        }
    }

    fn fetch(&self, api: &Api) -> Result<Option<Current>, ApiError> {
        let mut queued: QueuedCards = api.call(
            "get_queued_cards",
            &anki_proto::scheduler::GetQueuedCardsRequest {
                fetch_limit: 1,
                intraday_learning_only: false,
            },
        )?;
        if queued.cards.is_empty() {
            return Ok(None);
        }
        let top = queued.cards.remove(0);
        let card = top.card.unwrap_or_default();
        let mut states = top.states.unwrap_or_default();
        if let Some(cur) = states.current.as_mut() {
            cur.custom_data = Some(card.custom_data.clone());
        }
        let conf = deck_conf(api, current_deck_id(&card))?;
        let text = self.render(api, &card)?;
        let (marked, note_mtime) = note_info(api, card.note_id)?;
        Ok(Some(Current {
            queue_states: states.clone(),
            note_mtime,
            queue: Queue::try_from(top.queue).unwrap_or(Queue::Review),
            counts: [queued.new_count, queued.learning_count, queued.review_count],
            states,
            context: top.context.unwrap_or_default(),
            started: Instant::now(),
            text,
            conf,
            type_answer: TypeAnswer::default(),
            typed: String::new(),
            marked,
            card,
        }))
    }

    fn render(&self, api: &Api, card: &Card) -> Result<RenderedCard, ApiError> {
        let note: anki_proto::notes::Note =
            api.call("get_note", &anki_proto::notes::NoteId { nid: card.note_id })?;
        let latex = crate::latex::latex_config(api, note.notetype_id).unwrap_or_default();
        let media = self.media_dir.clone().unwrap_or_default();
        card_text::render(api, card.id, |text| {
            let (html, errors) = crate::latex::render_latex(api, &media, text, &latex);
            if errors.is_empty() {
                html
            } else {
                html + &errors.join("\n")
            }
        })
    }

    fn body_class(&self, ord: u32) -> String {
        let night = if self.night_mode {
            " nightMode night_mode"
        } else {
            ""
        };
        format!("card card{} isMac{night}", ord + 1)
    }

    fn prepare(&self, api: &Api, text: &str) -> String {
        card_text::prepare_for_display(api, text, self.prefs.hide_play_buttons)
            .unwrap_or_else(|_| text.to_string())
    }

    pub fn show_question(&mut self, api: &Api) -> Vec<Effect> {
        let Some(c) = self.current.as_mut() else {
            return vec![];
        };
        self.side = Side::Question;
        self.reps += 1;
        c.typed.clear();
        // custom scheduling (below) starts again from the queue's states
        c.states = c.queue_states.clone();
        // the [[type:]] input, and the expected answer for later
        let note = api
            .call::<anki_proto::notes::Note>(
                "get_note",
                &anki_proto::notes::NoteId {
                    nid: c.card.note_id,
                },
            )
            .unwrap_or_default();
        let notetype = api
            .call::<anki_proto::notetypes::Notetype>(
                "get_notetype",
                &anki_proto::notetypes::NotetypeId {
                    ntid: note.notetype_id,
                },
            )
            .unwrap_or_default();
        let ord = c.card.template_idx;
        let tr = &self.tr;
        let (q, ta) = card_text::type_answer_question_filter(
            &c.text.question_and_style(),
            |r| {
                let idx = notetype.fields.iter().position(|f| f.name == r.field)?;
                let field = &notetype.fields[idx];
                let mut content = note.fields.get(idx).cloned().unwrap_or_default();
                if r.cloze {
                    content = api
                        .call::<anki_proto::generic::String>(
                            "extract_cloze_for_typing",
                            &anki_proto::card_rendering::ExtractClozeForTypingRequest {
                                text: content,
                                ordinal: ord + 1,
                            },
                        )
                        .map(|s| s.val)
                        .unwrap_or_default();
                    if content.is_empty() {
                        return None;
                    }
                }
                let config = field.config.clone().unwrap_or_default();
                Some((content, config.font_name, config.font_size))
            },
            |missing| match missing {
                card_text::Missing::EmptyCloze => {
                    tr.studying_please_run_toolsempty_cards().into_owned()
                }
                card_text::Missing::UnknownField(f) => {
                    tr.studying_type_answer_unknown_field(f).into_owned()
                }
            },
        );
        c.type_answer = ta;
        let q = self.prepare_ref(api, &q);
        let c = self.current.as_ref().unwrap();
        let a =
            card_text::escape_media_filenames(api, &c.text.answer_and_style()).unwrap_or_default();
        let mut out = vec![];
        // audio
        let sounds = if c.conf.disable_autoplay {
            vec![]
        } else {
            c.text.question_av.clone()
        };
        out.push(self.play(&sounds));
        // custom scheduling
        if !self.mutation_js.is_empty() {
            self.states_mutated = false;
            out.push(Effect::Js(state_mutation_js(
                &self.mutation_key,
                &self.mutation_js,
            )));
        } else {
            self.states_mutated = true;
        }
        let c = self.current.as_ref().unwrap();
        out.push(Effect::Js(format!(
            "_showQuestion({}, {}, {});",
            js(&q),
            js(&a),
            js(self.body_class(c.card.template_idx))
        )));
        out.extend(self.flag_and_mark());
        out.push(self.answer_button(api));
        out.extend(self.auto_advance_timer(true));
        out
    }

    fn prepare_ref(&self, api: &Api, text: &str) -> String {
        self.prepare(api, text)
    }

    fn flag_and_mark(&self) -> Vec<Effect> {
        vec![
            Effect::Js(format!("_drawFlag({});", self.flag())),
            Effect::Js(format!("_drawMark({});", self.marked())),
        ]
    }

    fn play(&self, tags: &[AvTag]) -> Effect {
        Effect::Js(format!(
            "_shellAv.play({}, {});",
            av_tags_json(tags),
            self.prefs.interrupt_audio
        ))
    }

    fn remaining(&self) -> String {
        let Some(c) = &self.current else {
            return String::new();
        };
        if !self.prefs.due_counts {
            return String::new();
        }
        let idx = match c.queue {
            Queue::New => 0,
            Queue::Learning => 1,
            Queue::Review => 2,
        };
        let counts: Vec<String> = c
            .counts
            .iter()
            .enumerate()
            .map(|(i, n)| {
                if i == idx {
                    format!("<u>{n}</u>")
                } else {
                    n.to_string()
                }
            })
            .collect();
        format!(
            "\n<span class=new-count>{}</span> +\n<span class=learn-count>{}</span> +\n\
             <span class=review-count>{}</span>\n",
            counts[0], counts[1], counts[2]
        )
    }

    fn answer_button(&self, _api: &Api) -> Effect {
        let c = self.current.as_ref().unwrap();
        let middle = format!(
            "\n<button title=\"{}\" id=\"ansbut\" onclick='pycmd(\"ans\");'>{}<span \
             class=stattxt>{}</span></button>",
            htmlescape::encode_attribute(
                &self
                    .tr
                    .actions_shortcut_key(self.tr.studying_space().into_owned())
            ),
            self.tr.studying_show_answer(),
            self.remaining()
        );
        let middle = format!(
            "<table cellpadding=0><tr><td class=stat2 align=center>{middle}</td></tr></table>"
        );
        let max_time = if c.conf.show_timer {
            c.conf.cap_answer_time_to_secs
        } else {
            0
        };
        bottom("showQuestion", &[json!(middle), json!(max_time)])
    }

    /// The "ans" command (button, Enter in the type box, or a shortcut):
    /// first fetch what was typed.
    pub fn request_answer(&self) -> Vec<Effect> {
        if self.side() != Some(Side::Question) {
            return vec![];
        }
        vec![Effect::Js(
            "pycmd('typedAnswer:' + JSON.stringify(\
             typeof getTypedAnswer === 'function' ? (getTypedAnswer() ?? '') : ''));"
                .into(),
        )]
    }

    /// `_onTypedAnswer` + `_showAnswer`.
    pub fn show_answer(&mut self, api: &Api, typed: &str) -> Vec<Effect> {
        if self.side() != Some(Side::Question) {
            return vec![];
        }
        let c = self.current.as_mut().unwrap();
        c.typed = typed.to_string();
        self.side = Side::Answer;
        let c = self.current.as_ref().unwrap();
        let a = card_text::type_answer_answer_filter(
            &c.text.answer_and_style(),
            &c.type_answer,
            &c.typed,
            |expected, provided, combining| {
                api.call::<anki_proto::generic::String>(
                    "compare_answer",
                    &anki_proto::card_rendering::CompareAnswerRequest {
                        expected: expected.into(),
                        provided: provided.into(),
                        combining,
                    },
                )
                .map(|s| s.val)
                .unwrap_or_default()
            },
        );
        let a = self.prepare(api, &a);
        let sounds = if c.conf.disable_autoplay {
            vec![]
        } else {
            c.text.answer_av.clone()
        };
        let mut out = vec![
            self.play(&sounds),
            Effect::Js(format!("_showAnswer({});", js(&a))),
        ];
        out.extend(self.ease_buttons(api));
        out.extend(self.auto_advance_timer(false));
        out
    }

    /// `_showEaseButtons`, once the custom scheduling has run.
    fn ease_buttons(&mut self, api: &Api) -> Vec<Effect> {
        if !self.states_mutated {
            self.ease_buttons_pending = true;
            return vec![];
        }
        self.ease_buttons_pending = false;
        let c = self.current.as_ref().unwrap();
        let labels: Vec<String> = api
            .call::<anki_proto::generic::StringList>("describe_next_states", &c.states)
            .map(|l| l.vals)
            .unwrap_or_default();
        vec![bottom(
            "showAnswer",
            &[
                json!(self.answer_buttons_html(&labels)),
                json!(c.conf.stop_timer_on_answer),
            ],
        )]
    }

    fn answer_buttons_html(&self, labels: &[String]) -> String {
        let tr = &self.tr;
        let buttons = [
            (1, tr.studying_again()),
            (2, tr.studying_hard()),
            (3, tr.studying_good()),
            (4, tr.studying_easy()),
        ];
        let mut buf = String::from("<center><table cellpadding=0 cellspacing=0><tr>");
        for (ease, label) in buttons {
            let extra = if ease == 3 { "id=\"defease\" " } else { "" };
            let due = if self.prefs.intervals_on_buttons {
                format!(
                    "<span class=\"nobold\">{}</span>",
                    labels.get(ease - 1).cloned().unwrap_or_default()
                )
            } else {
                String::new()
            };
            buf += &format!(
                "\n<td align=center><button {extra}data-ease=\"{ease}\" \
                 onclick='pycmd(\"ease{ease}\");'>{label}{due}</button></td>"
            );
        }
        buf + "</tr></table>"
    }

    /// The page's `statesMutated`.
    pub fn states_mutated(&mut self, api: &Api) -> Vec<Effect> {
        self.states_mutated = true;
        if self.ease_buttons_pending && self.side() == Some(Side::Answer) {
            return self.ease_buttons(api);
        }
        vec![]
    }

    pub fn scheduling_states(&self) -> Option<(SchedulingStates, SchedulingContext)> {
        self.current
            .as_ref()
            .map(|c| (c.states.clone(), c.context.clone()))
    }

    /// `set_scheduling_states`: only from this card's mutation script.
    pub fn set_scheduling_states(&mut self, key: &str, states: SchedulingStates) {
        if key != self.mutation_key {
            return;
        }
        if let Some(c) = self.current.as_mut() {
            c.states = states;
        }
    }

    /// `_answerCard`.
    pub fn answer(&mut self, api: &Api, ease: u8) -> Vec<Effect> {
        if self.side() != Some(Side::Answer) || !(1..=4).contains(&ease) {
            return vec![];
        }
        let c = self.current.as_ref().unwrap();
        let states = &c.states;
        let (rating, new_state) = match ease {
            1 => (Rating::Again, &states.again),
            2 => (Rating::Hard, &states.hard),
            3 => (Rating::Good, &states.good),
            _ => (Rating::Easy, &states.easy),
        };
        let answer = CardAnswer {
            card_id: c.card.id,
            current_state: states.current.clone(),
            new_state: new_state.clone(),
            rating: rating as i32,
            answered_at_millis: chrono::Utc::now().timestamp_millis(),
            milliseconds_taken: c.started.elapsed().as_millis() as u32,
        };
        self.side = Side::Transition;
        if let Err(err) = api.call::<OpChanges>("answer_card", &answer) {
            tracing::error!(%err, "answering failed");
            self.side = Side::Answer;
            return vec![Effect::Tooltip(err.message)];
        }
        let mut out = vec![Effect::UndoChanged];
        if let Some(state) = &answer.new_state {
            let leech = api
                .call::<anki_proto::generic::Bool>("state_is_leech", state)
                .is_ok_and(|b| b.val);
            if leech {
                let suspended = api
                    .call::<Card>(
                        "get_card",
                        &anki_proto::cards::CardId {
                            cid: answer.card_id,
                        },
                    )
                    .is_ok_and(|c| c.queue < 0);
                let mut msg = self.tr.studying_card_was_a_leech().into_owned();
                if suspended {
                    msg += &format!(" {}", self.tr.studying_it_has_been_suspended());
                }
                out.push(Effect::Tooltip(msg));
            }
        }
        if let Some((cards, minutes)) = self.timebox_reached(api) {
            self.previous_card = Some(answer.card_id);
            self.current = None;
            out.push(Effect::Timebox { cards, minutes });
            return out;
        }
        out.extend(self.next_card(api));
        out
    }

    /// `col.timeboxReached`: (cards, minutes) once the time limit passes.
    fn timebox_reached(&mut self, _api: &Api) -> Option<(u32, u32)> {
        let limit = self.prefs.timebox_secs;
        let (start, reps) = self.timebox.as_mut()?;
        *reps += 1;
        if limit == 0 || start.elapsed().as_secs() < limit as u64 {
            return None;
        }
        Some((*reps, ((limit as f64) / 60.0).round() as u32))
    }

    /// After the timebox prompt: continue (restarting it) or finish.
    pub fn timebox_answered(&mut self, api: &Api, keep_going: bool) -> Vec<Effect> {
        if !keep_going {
            return vec![Effect::DeckBrowser];
        }
        self.timebox = Some((Instant::now(), 0));
        self.next_card(api)
    }

    /// Space/Enter (`onEnterKey`): show the answer, or answer with the
    /// focused button (`selectedAnswerButton`), else the default (Good).
    pub fn enter_key(&self) -> Vec<Effect> {
        match self.side() {
            Some(Side::Question) => self.request_answer(),
            Some(Side::Answer) if self.spacebar_rates_card => vec![Effect::Js(
                "pycmd('ease' + (['1','2','3','4'].includes(String(_shellBottomSelected())) \
                 ? _shellBottomSelected() : '3'));"
                    .into(),
            )],
            _ => vec![],
        }
    }

    /// Redraw after the note was edited (`_redraw_current_card`).
    pub fn redraw(&mut self, api: &Api) -> Vec<Effect> {
        let Some(card_id) = self.card_id() else {
            return vec![];
        };
        let card: Card = match api.call("get_card", &anki_proto::cards::CardId { cid: card_id }) {
            Ok(card) => card,
            // deleted meanwhile
            Err(_) => return self.next_card(api),
        };
        let text = match self.render(api, &card) {
            Ok(text) => text,
            Err(err) => return vec![Effect::Tooltip(err.message)],
        };
        let (marked, note_mtime) = note_info(api, card.note_id).unwrap_or_default();
        let c = self.current.as_mut().unwrap();
        c.card = card;
        c.text = text;
        c.marked = marked;
        c.note_mtime = note_mtime;
        match self.side {
            // as `_redraw_current_card`: the answer again, keeping the
            // (already mutated) states
            Side::Answer => {
                let typed = c.typed.clone();
                self.side = Side::Question;
                self.show_answer(api, &typed)
            }
            _ => {
                self.reps -= 1;
                self.show_question(api)
            }
        }
    }

    /// After the editor closed: redraw only if the note was changed.
    pub fn redraw_if_note_changed(&mut self, api: &Api) -> Vec<Effect> {
        let Some(c) = &self.current else {
            return vec![];
        };
        let changed =
            note_info(api, c.card.note_id).map_or(true, |(_, mtime)| mtime != c.note_mtime);
        if changed {
            self.redraw(api)
        } else {
            vec![]
        }
    }

    /// The key the page's custom scheduling code must send back (tests).
    #[doc(hidden)]
    pub fn mutation_key(&self) -> &str {
        &self.mutation_key
    }

    pub fn replay(&self) -> Vec<Effect> {
        let Some(c) = &self.current else {
            return vec![];
        };
        let tags = match self.side {
            Side::Question => c.text.question_av.clone(),
            Side::Answer => {
                let mut tags = vec![];
                if !c.conf.skip_question_when_replaying_answer {
                    tags.extend(c.text.question_av.iter().cloned());
                }
                tags.extend(c.text.answer_av.iter().cloned());
                tags
            }
            Side::Transition => return vec![],
        };
        vec![Effect::Js(format!(
            "_shellAv.play({}, true);",
            av_tags_json(&tags)
        ))]
    }

    /// A play button (`play:q:1`).
    pub fn play_clicked(&self, cmd: &str) -> Vec<Effect> {
        let (Some(c), Some((question, idx))) = (&self.current, card_text::parse_play_ref(cmd))
        else {
            return vec![];
        };
        let tags = if question {
            &c.text.question_av
        } else {
            &c.text.answer_av
        };
        match tags.get(idx) {
            Some(tag) => vec![Effect::Js(format!(
                "_shellAv.play({}, true);",
                av_tags_json(std::slice::from_ref(tag))
            ))],
            None => vec![],
        }
    }

    // Card and note actions
    //////////////////////////////////////////////////////////////////////

    /// Toggle flag `flag` (1-7).
    pub fn set_flag(&mut self, api: &Api, flag: u32) -> Vec<Effect> {
        let Some(card_id) = self.card_id() else {
            return vec![];
        };
        let flag = if self.flag() == flag { 0 } else { flag };
        if let Err(err) = api.call::<OpChangesWithCount>(
            "set_flag",
            &anki_proto::cards::SetFlagRequest {
                card_ids: vec![card_id],
                flag,
            },
        ) {
            return vec![Effect::Tooltip(err.message)];
        }
        let c = self.current.as_mut().unwrap();
        c.card.flags = (c.card.flags & !0b111) | flag;
        vec![
            Effect::Js(format!("_drawFlag({flag});")),
            Effect::UndoChanged,
        ]
    }

    pub fn toggle_mark(&mut self, api: &Api) -> Vec<Effect> {
        let Some(nid) = self.note_id() else {
            return vec![];
        };
        let method = if self.marked() {
            "remove_note_tags"
        } else {
            "add_note_tags"
        };
        if let Err(err) = api.call::<OpChangesWithCount>(
            method,
            &anki_proto::tags::NoteIdsAndTagsRequest {
                note_ids: vec![nid],
                tags: MARKED_TAG.into(),
            },
        ) {
            return vec![Effect::Tooltip(err.message)];
        }
        let c = self.current.as_mut().unwrap();
        c.marked = !c.marked;
        vec![
            Effect::Js(format!("_drawMark({});", c.marked)),
            Effect::UndoChanged,
        ]
    }

    /// Bury or suspend the card or its note, then move on.
    pub fn bury_or_suspend(&mut self, api: &Api, note: bool, suspend: bool) -> Vec<Effect> {
        let (Some(cid), Some(nid)) = (self.card_id(), self.note_id()) else {
            return vec![];
        };
        let (card_ids, note_ids) = if note {
            (vec![], vec![nid])
        } else {
            (vec![cid], vec![])
        };
        let mode = if suspend {
            BuryMode::Suspend
        } else {
            BuryMode::BuryUser
        };
        let res = api.call::<OpChangesWithCount>(
            "bury_or_suspend_cards",
            &anki_proto::scheduler::BuryOrSuspendCardsRequest {
                card_ids,
                note_ids,
                mode: mode as i32,
            },
        );
        let count = match res {
            Ok(res) => res.count,
            Err(err) => return vec![Effect::Tooltip(err.message)],
        };
        let msg = match (suspend, note) {
            (true, true) => self.tr.studying_note_suspended().into_owned(),
            (true, false) => self.tr.studying_card_suspended().into_owned(),
            (false, _) => self.tr.studying_cards_buried(count).into_owned(),
        };
        let mut out = vec![Effect::Tooltip(msg), Effect::UndoChanged];
        out.extend(self.next_card(api));
        out
    }

    pub fn delete_note(&mut self, api: &Api) -> Vec<Effect> {
        let Some(nid) = self.note_id() else {
            return vec![];
        };
        let res = api.call::<OpChangesWithCount>(
            "remove_notes",
            &anki_proto::notes::RemoveNotesRequest {
                note_ids: vec![nid],
                card_ids: vec![],
            },
        );
        let count = match res {
            Ok(res) => res.count,
            Err(err) => return vec![Effect::Tooltip(err.message)],
        };
        let mut out = vec![
            Effect::Tooltip(self.tr.browsing_cards_deleted(count).into_owned()),
            Effect::UndoChanged,
        ];
        out.extend(self.next_card(api));
        out
    }

    /// Undo or redo, then show whatever is now at the top of the queue.
    pub fn undo(&mut self, api: &Api, redo: bool) -> Vec<Effect> {
        let method = if redo { "redo" } else { "undo" };
        match api.call::<anki_proto::collection::OpChangesAfterUndo>(
            method,
            &anki_proto::generic::Empty {},
        ) {
            Ok(out) => {
                let mut effects = vec![
                    Effect::Tooltip(if redo {
                        self.tr.undo_redo_action(out.operation).into_owned()
                    } else {
                        self.tr.undo_action_undone(out.operation).into_owned()
                    }),
                    Effect::UndoChanged,
                ];
                if self.current.is_some() {
                    // the previous card isn't the one being shown anymore
                    self.current = None;
                    effects.extend(self.next_card(api));
                }
                effects
            }
            Err(err) => vec![Effect::Tooltip(err.message)],
        }
    }

    pub fn edit(&self) -> Vec<Effect> {
        self.note_id().map(Effect::EditNote).into_iter().collect()
    }

    pub fn card_info(&self, previous: bool) -> Vec<Effect> {
        let cid = if previous {
            self.previous_card
        } else {
            self.card_id()
        };
        cid.map(Effect::CardInfo).into_iter().collect()
    }

    pub fn options(&self) -> Vec<Effect> {
        self.current
            .as_ref()
            .map(|c| Effect::DeckOptions(current_deck_id(&c.card)))
            .into_iter()
            .collect()
    }

    // Auto advance
    //////////////////////////////////////////////////////////////////////

    pub fn toggle_auto_advance(&mut self) -> Vec<Effect> {
        self.auto_advance = !self.auto_advance;
        let msg = if self.auto_advance {
            self.tr.actions_auto_advance_activated()
        } else {
            self.tr.actions_auto_advance_deactivated()
        };
        let mut out = vec![Effect::Tooltip(msg.into_owned())];
        match self.side() {
            Some(Side::Question) => out.extend(self.auto_advance_timer(true)),
            Some(Side::Answer) => out.extend(self.auto_advance_timer(false)),
            _ => {}
        }
        out
    }

    fn auto_advance_timer(&mut self, question: bool) -> Vec<Effect> {
        self.seq += 1;
        let Some(c) = &self.current else {
            return vec![];
        };
        if !self.auto_advance {
            return vec![];
        }
        let secs = if question {
            c.conf.seconds_to_show_question
        } else {
            c.conf.seconds_to_show_answer
        };
        if secs <= 0.0 {
            return vec![];
        }
        vec![Effect::Timer {
            seq: self.seq,
            after: Duration::from_secs_f32(secs),
        }]
    }

    /// A timer from [Effect::Timer] fired. With "wait for audio", the page
    /// is asked to call back when audio is done (`autoAdvanceNow`).
    pub fn auto_advance_due(&mut self, seq: u64) -> Vec<Effect> {
        if seq != self.seq || !self.auto_advance || self.current.is_none() {
            return vec![];
        }
        let c = self.current.as_ref().unwrap();
        if c.conf.wait_for_audio {
            return vec![Effect::Js(format!(
                "_shellAv.whenIdle(() => pycmd('autoAdvanceNow:{seq}'));"
            ))];
        }
        vec![Effect::Js(format!("pycmd('autoAdvanceNow:{seq}');"))]
    }

    pub fn auto_advance_now(&mut self, api: &Api, seq: u64) -> Vec<Effect> {
        if seq != self.seq || !self.auto_advance {
            return vec![];
        }
        let Some(c) = &self.current else {
            return vec![];
        };
        match self.side {
            Side::Question => match QuestionAction::try_from(c.conf.question_action) {
                Ok(QuestionAction::ShowReminder) => vec![Effect::Tooltip(
                    self.tr.studying_question_time_elapsed().into_owned(),
                )],
                _ => self.request_answer(),
            },
            Side::Answer => match AnswerAction::try_from(c.conf.answer_action) {
                Ok(AnswerAction::AnswerAgain) => self.answer(api, 1),
                Ok(AnswerAction::AnswerHard) => self.answer(api, 2),
                Ok(AnswerAction::AnswerGood) => self.answer(api, 3),
                Ok(AnswerAction::ShowReminder) => vec![Effect::Tooltip(
                    self.tr.studying_answer_time_elapsed().into_owned(),
                )],
                _ => self.bury_or_suspend(api, false, false),
            },
            Side::Transition => vec![],
        }
    }

    /// The window lost focus: Qt stops auto advance then.
    pub fn disable_auto_advance(&mut self) {
        self.auto_advance = false;
        self.seq += 1;
    }
}

fn read_prefs(api: &Api) -> Prefs {
    let prefs: anki_proto::config::Preferences = api
        .call("get_preferences", &anki_proto::generic::Empty {})
        .unwrap_or_default();
    let r = prefs.reviewing.unwrap_or_default();
    Prefs {
        hide_play_buttons: r.hide_audio_play_buttons,
        interrupt_audio: r.interrupt_audio_when_answering,
        due_counts: r.show_remaining_due_counts,
        intervals_on_buttons: r.show_intervals_on_buttons,
        timebox_secs: r.time_limit_secs,
    }
}

/// `card.current_deck_id()`: the home deck of a card in a filtered deck.
pub fn current_deck_id(card: &Card) -> i64 {
    if card.original_deck_id != 0 {
        card.original_deck_id
    } else {
        card.deck_id
    }
}

/// `config_dict_for_deck_id`.
fn deck_conf(api: &Api, did: i64) -> Result<DeckConf, ApiError> {
    let deck: anki_proto::decks::Deck = api.call("get_deck", &anki_proto::decks::DeckId { did })?;
    let conf_id = match deck.kind {
        Some(anki_proto::decks::deck::Kind::Normal(n)) => n.config_id,
        _ => 1,
    };
    let conf: anki_proto::deck_config::DeckConfig = api.call(
        "get_deck_config",
        &anki_proto::deck_config::DeckConfigId { dcid: conf_id },
    )?;
    Ok(conf.config.unwrap_or_default())
}

/// Whether the note is marked, and its mtime.
fn note_info(api: &Api, nid: i64) -> Result<(bool, i64), ApiError> {
    let note: anki_proto::notes::Note = api.call("get_note", &anki_proto::notes::NoteId { nid })?;
    let marked = note.tags.iter().any(|t| t.eq_ignore_ascii_case(MARKED_TAG));
    Ok((marked, note.mtime_secs as i64))
}

/// The av tags as the page player takes them.
pub fn av_tags_json(tags: &[AvTag]) -> String {
    let tags: Vec<Value> = tags
        .iter()
        .filter_map(|t| match &t.value {
            Some(AvValue::SoundOrVideo(file)) => Some(json!({ "file": file })),
            Some(AvValue::Tts(tts)) => Some(json!({ "tts": {
                "text": tts.field_text,
                "lang": tts.lang,
                "voices": tts.voices,
                "speed": tts.speed,
            }})),
            None => None,
        })
        .collect();
    Value::Array(tags).to_string()
}

/// `RUN_STATE_MUTATION`. The user's code is evaluated from a string, so
/// a syntax error in it is caught and the ease buttons still show.
fn state_mutation_js(key: &str, code: &str) -> String {
    let body = format!(
        "anki.mutateNextCardStates({}, async (states, customData, ctx) => {{\n{code}\n}})\
         .finally(() => bridgeCommand('statesMutated'));",
        js(key)
    );
    format!(
        "try {{ (0, eval)({}); }} catch (e) {{ console.error(e); bridgeCommand('statesMutated'); }}",
        js(body)
    )
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn av_tags_serialize_for_the_player() {
        let tags = vec![
            AvTag {
                value: Some(AvValue::SoundOrVideo("a b.mp3".into())),
            },
            AvTag {
                value: Some(AvValue::Tts(anki_proto::card_rendering::TtsTag {
                    field_text: "bonjour".into(),
                    lang: "fr_FR".into(),
                    voices: vec!["Amelie".into()],
                    speed: 1.2,
                    other_args: vec![],
                })),
            },
        ];
        let v: Value = serde_json::from_str(&av_tags_json(&tags)).unwrap();
        assert_eq!(v[0]["file"], "a b.mp3");
        assert_eq!(v[1]["tts"]["lang"], "fr_FR");
        assert_eq!(v[1]["tts"]["voices"][0], "Amelie");
    }

    #[test]
    fn state_mutation_survives_syntax_errors() {
        let out = state_mutation_js("k", "states.good = {");
        assert!(out.starts_with("try { (0, eval)("));
        assert!(out.contains("catch (e)"));
        // the user's code is inside a JS string, not in the script itself
        assert!(!out.contains("states.good = {\n"));
    }
}
