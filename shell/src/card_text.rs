// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Card text as the reviewer shows it, which is Python in the Qt app:
//! - rendering with AV tag extraction (`TemplateRenderContext.render`) and
//!   LaTeX (the `card_did_render` hook),
//! - `prepare_card_text_for_display` (media filename escaping, play buttons),
//! - the `[[type:]]` filters from `reviewer.py`.

use std::sync::LazyLock;

use anki_proto::card_rendering::av_tag::Value as AvValue;
use anki_proto::card_rendering::rendered_template_node::Value as Node;
use anki_proto::card_rendering::AvTag;
use anki_proto::card_rendering::ExtractAvTagsRequest;
use anki_proto::card_rendering::ExtractAvTagsResponse;
use anki_proto::card_rendering::RenderCardResponse;
use anki_proto::card_rendering::RenderExistingCardRequest;
use anki_proto::card_rendering::RenderedTemplateNode;
use regex::Captures;
use regex::Regex;

use crate::api::Api;
use crate::api::ApiError;

/// A rendered card: text without styles, and its sounds/TTS.
#[derive(Debug, Clone, Default)]
pub struct RenderedCard {
    pub question: String,
    pub answer: String,
    pub question_av: Vec<AvTag>,
    pub answer_av: Vec<AvTag>,
    pub css: String,
}

impl RenderedCard {
    pub fn question_and_style(&self) -> String {
        format!("<style>{}</style>{}", self.css, self.question)
    }

    pub fn answer_and_style(&self) -> String {
        format!("<style>{}</style>{}", self.css, self.answer)
    }
}

/// Render an existing card for review. `latex` turns LaTeX in a side's text
/// into images (see `crate::latex`).
pub fn render(
    api: &Api,
    card_id: i64,
    latex: impl Fn(&str) -> String,
) -> Result<RenderedCard, ApiError> {
    let out: RenderCardResponse = api.call(
        "render_existing_card",
        &RenderExistingCardRequest {
            card_id,
            browser: false,
            partial_render: false,
        },
    )?;
    let side = |nodes: &[RenderedTemplateNode], question_side| {
        api.call::<ExtractAvTagsResponse>(
            "extract_av_tags",
            &ExtractAvTagsRequest {
                text: nodes_to_text(nodes),
                question_side,
            },
        )
    };
    let q = side(&out.question_nodes, true)?;
    let a = side(&out.answer_nodes, false)?;
    Ok(RenderedCard {
        question: latex(&q.text),
        answer: latex(&a.text),
        question_av: q.av_tags,
        answer_av: a.av_tags,
        css: out.css,
    })
}

pub fn nodes_to_text(nodes: &[RenderedTemplateNode]) -> String {
    let mut out = String::new();
    for node in nodes {
        match &node.value {
            Some(Node::Text(t)) => out += t,
            Some(Node::Replacement(r)) => out += &r.current_text,
            None => {}
        }
    }
    out
}

static AV_REF: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\[anki:(play:(.):(\d+))\]").unwrap());

/// `aqt.sound.av_refs_to_play_icons`: `[anki:play:q:0]` becomes a button
/// that sends `play:q:0`.
pub fn av_refs_to_play_icons(text: &str) -> String {
    AV_REF
        .replace_all(text, |caps: &Captures| {
            format!(
                r#"
<a class="replay-button soundLink" href=# onclick="pycmd('{}'); return false;" draggable="false">
    <svg class="playImage" viewBox="0 0 64 64" version="1.1">
        <circle cx="32" cy="32" r="29" />
        <path d="M56.502,32.301l-37.502,20.101l0.329,-40.804l37.173,20.703Z" />
    </svg>
</a>"#,
                &caps[1]
            )
        })
        .into_owned()
}

pub fn strip_av_refs(text: &str) -> String {
    AV_REF.replace_all(text, "").into_owned()
}

/// `media.escape_media_filenames`: percent-encode media references.
pub fn escape_media_filenames(api: &Api, text: &str) -> Result<String, ApiError> {
    let out: anki_proto::generic::String = api.call(
        "encode_iri_paths",
        &anki_proto::generic::String { val: text.into() },
    )?;
    Ok(out.val)
}

/// `AnkiQt.prepare_card_text_for_display`.
pub fn prepare_for_display(
    api: &Api,
    text: &str,
    hide_play_buttons: bool,
) -> Result<String, ApiError> {
    let text = escape_media_filenames(api, text)?;
    Ok(if hide_play_buttons {
        strip_av_refs(&text)
    } else {
        av_refs_to_play_icons(&text)
    })
}

/// A `play:q:1` command's side and index.
pub fn parse_play_ref(cmd: &str) -> Option<(bool, usize)> {
    let mut parts = cmd.split(':');
    let side = parts.next()?;
    let idx = parts.next()?.parse().ok()?;
    match side {
        "q" => Some((true, idx)),
        "a" => Some((false, idx)),
        _ => None,
    }
}

pub fn av_tag_is_tts(tag: &AvTag) -> bool {
    matches!(tag.value, Some(AvValue::Tts(_)))
}

// Typed answers
//////////////////////////////////////////////////////////////////////////

static TYPE_ANS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\[\[type:(.+?)\]\]").unwrap());

/// What a `[[type:...]]` reference asks for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TypeAnswerRef {
    pub field: String,
    pub cloze: bool,
    /// False for `nc:` (compare without combining characters).
    pub combining: bool,
}

pub fn find_type_answer(text: &str) -> Option<TypeAnswerRef> {
    let caps = TYPE_ANS.captures(text)?;
    let mut fld = caps[1].to_string();
    let mut cloze = false;
    let mut combining = true;
    // as reviewer.py: `split(":")[1]` keeps only the next segment
    if fld.starts_with("cloze:") {
        cloze = true;
        fld = fld.split(':').nth(1).unwrap_or_default().to_string();
    }
    if fld.starts_with("nc:") {
        combining = false;
        fld = fld.split(':').nth(1).unwrap_or_default().to_string();
    }
    Some(TypeAnswerRef {
        field: fld,
        cloze,
        combining,
    })
}

/// The expected answer for the current card, found while filtering the
/// question.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct TypeAnswer {
    /// None: no `[[type:]]` on the card, or its field doesn't exist.
    pub expected: Option<String>,
    pub font: String,
    pub size: u32,
    pub combining: bool,
}

/// How the question filter reports a missing field.
pub enum Missing {
    UnknownField(String),
    EmptyCloze,
}

/// `typeAnsQuestionFilter`. `field` looks up a field's (content, font,
/// size), already narrowed to the cloze when `cloze`; `warn` builds the
/// warning text.
pub fn type_answer_question_filter(
    text: &str,
    field: impl Fn(&TypeAnswerRef) -> Option<(String, String, u32)>,
    warn: impl Fn(Missing) -> String,
) -> (String, TypeAnswer) {
    let Some(r) = find_type_answer(text) else {
        return (text.to_string(), TypeAnswer::default());
    };
    let mut ta = TypeAnswer {
        combining: r.combining,
        ..Default::default()
    };
    match field(&r) {
        None => {
            let msg = warn(if r.cloze {
                Missing::EmptyCloze
            } else {
                Missing::UnknownField(r.field.clone())
            });
            let out = TYPE_ANS
                .replace_all(text, regex::NoExpand(&msg))
                .into_owned();
            (out, ta)
        }
        Some((content, _, _)) if content.is_empty() => {
            ta.expected = Some(String::new());
            (TYPE_ANS.replace_all(text, "").into_owned(), ta)
        }
        Some((content, font, size)) => {
            let input = format!(
                "\n<center>\n<input type=text id=typeans onkeypress=\"_typeAnsPress();\"\n   \
                 style=\"font-family: '{font}'; font-size: {size}px;\">\n</center>\n"
            );
            ta.expected = Some(content);
            ta.font = font;
            ta.size = size;
            (
                TYPE_ANS
                    .replace_all(text, regex::NoExpand(&input))
                    .into_owned(),
                ta,
            )
        }
    }
}

/// `typeAnsAnswerFilter`. `compare` is the backend's `compare_answer`
/// output for (expected, provided, combining).
pub fn type_answer_answer_filter(
    text: &str,
    ta: &TypeAnswer,
    provided: &str,
    compare: impl Fn(&str, &str, bool) -> String,
) -> String {
    let expected = match &ta.expected {
        Some(e) if !e.is_empty() => e,
        _ => return TYPE_ANS.replace_all(text, "").into_owned(),
    };
    let stripped = text.replace("<hr id=answer>", "");
    let had_hr = stripped.len() != text.len();
    if had_hr && !TYPE_ANS.is_match(&stripped) {
        return text.to_string();
    }
    let output = compare(expected, provided, ta.combining);
    let mut repl = format!(
        "\n<div style=\"font-family: '{}'; font-size: {}px\">{}</div>",
        ta.font, ta.size, output
    );
    if had_hr {
        // as reviewer.py: keep the separator before the comparison when
        // the answer uses {{FrontSide}}
        repl = format!("<hr id=answer>{repl}");
    }
    TYPE_ANS
        .replace_all(&stripped, regex::NoExpand(&repl))
        .into_owned()
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn play_refs_become_buttons_or_go() {
        let text = "a[anki:play:q:0]b[anki:play:a:12]";
        let out = av_refs_to_play_icons(text);
        assert!(out.starts_with("a\n<a class=\"replay-button soundLink\""));
        assert!(out.contains("pycmd('play:q:0')"));
        assert!(out.contains("pycmd('play:a:12')"));
        assert!(!out.contains("[anki:"));
        assert_eq!(strip_av_refs(text), "ab");
    }

    #[test]
    fn play_commands_parse() {
        assert_eq!(parse_play_ref("q:3"), Some((true, 3)));
        assert_eq!(parse_play_ref("a:0"), Some((false, 0)));
        assert_eq!(parse_play_ref("x:0"), None);
        assert_eq!(parse_play_ref("q:"), None);
    }

    #[test]
    fn type_refs_parse_like_reviewer_py() {
        let r = |s: &str| find_type_answer(s).unwrap();
        assert_eq!(
            r("x[[type:Back]]y"),
            TypeAnswerRef {
                field: "Back".into(),
                cloze: false,
                combining: true
            }
        );
        assert_eq!(
            r("[[type:cloze:Text]]"),
            TypeAnswerRef {
                field: "Text".into(),
                cloze: true,
                combining: true
            }
        );
        assert_eq!(
            r("[[type:nc:Back]]"),
            TypeAnswerRef {
                field: "Back".into(),
                cloze: false,
                combining: false
            }
        );
        assert_eq!(find_type_answer("no ref"), None);
    }

    fn back_field(r: &TypeAnswerRef) -> Option<(String, String, u32)> {
        match r.field.as_str() {
            "Back" => Some(("chat".into(), "Arial".into(), 20)),
            "Empty" => Some((String::new(), "Arial".into(), 20)),
            _ => None,
        }
    }

    fn warn(m: Missing) -> String {
        match m {
            Missing::UnknownField(f) => format!("unknown {f}"),
            Missing::EmptyCloze => "empty cloze".into(),
        }
    }

    #[test]
    fn question_filter_adds_the_input() {
        let (out, ta) = type_answer_question_filter("Q [[type:Back]]", back_field, warn);
        assert!(out.starts_with("Q \n<center>\n<input type=text id=typeans"));
        assert!(out.contains("font-family: 'Arial'; font-size: 20px;"));
        assert_eq!(ta.expected.as_deref(), Some("chat"));
        assert!(ta.combining);
    }

    #[test]
    fn question_filter_warns_or_removes() {
        let (out, ta) = type_answer_question_filter("Q [[type:Nope]]", back_field, warn);
        assert_eq!(out, "Q unknown Nope");
        assert_eq!(ta.expected, None);
        let (out, _) = type_answer_question_filter("[[type:cloze:Nope]]", back_field, warn);
        assert_eq!(out, "empty cloze");
        let (out, ta) = type_answer_question_filter("Q [[type:Empty]]", back_field, warn);
        assert_eq!(out, "Q ");
        assert_eq!(ta.expected.as_deref(), Some(""));
        // `$` in the warning must not be treated as a capture reference
        let (out, _) = type_answer_question_filter("[[type:a$0]]", back_field, warn);
        assert_eq!(out, "unknown a$0");
    }

    fn compare(e: &str, p: &str, c: bool) -> String {
        format!("cmp({e},{p},{c})")
    }

    fn ta() -> TypeAnswer {
        TypeAnswer {
            expected: Some("chat".into()),
            font: "Arial".into(),
            size: 20,
            combining: true,
        }
    }

    #[test]
    fn answer_filter_shows_the_comparison() {
        let out = type_answer_answer_filter("A [[type:Back]]", &ta(), "chta", compare);
        assert_eq!(
            out,
            "A \n<div style=\"font-family: 'Arial'; font-size: 20px\">cmp(chat,chta,true)</div>"
        );
    }

    #[test]
    fn answer_filter_keeps_the_separator_before_the_comparison() {
        let text = "Q [[type:Back]]<hr id=answer>A";
        let out = type_answer_answer_filter(text, &ta(), "x", compare);
        assert_eq!(
            out,
            "Q <hr id=answer>\n<div style=\"font-family: 'Arial'; font-size: 20px\">\
             cmp(chat,x,true)</div>A"
        );
        // separator but no type ref: unchanged
        let text = "Q<hr id=answer>A";
        assert_eq!(type_answer_answer_filter(text, &ta(), "x", compare), text);
    }

    #[test]
    fn answer_filter_removes_refs_without_an_expected_answer() {
        let none = TypeAnswer::default();
        assert_eq!(
            type_answer_answer_filter("A [[type:Back]]", &none, "x", compare),
            "A "
        );
    }
}
