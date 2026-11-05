// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! `--bench`: the stock reviewer page driven by stdin, for the memory
//! harness (`shell/harness/bench.py`). It prints `ready` once the page is
//! up, then answers each `cycle <offset> <count>` line with `cycled` after
//! showing each card's question and answer, as the Qt reviewer drives
//! `reviewer.js`. `seed <n>` adds n synthetic notes first if the collection
//! has fewer cards, so the check can run on an empty profile.

use std::io::BufRead;
use std::sync::Arc;
use std::time::Duration;

use anki_proto::card_rendering::rendered_template_node::Value as Node;
use anki_proto::card_rendering::RenderCardResponse;
use anki_proto::card_rendering::RenderExistingCardRequest;
use anki_proto::card_rendering::RenderedTemplateNode;
use anki_proto::notes::AddNoteRequest;
use anki_proto::notes::Note;
use anki_proto::notetypes::NotetypeId;
use anyhow::Result;
use serde_json::Value;

use crate::api::ApiError;
use crate::app::Content;
use crate::app::Ui;
use crate::pages::WebviewKind;
use crate::Shell;

const LABEL: &str = "reviewer";
const INTERVAL: Duration = Duration::from_millis(250);

pub fn start(ui: &Arc<Ui>) -> Result<()> {
    let u = ui.clone();
    ui.shell.bridge.register("domDone", move |_, label, _| {
        u.evals.dom_done(&u.app, label);
        if label == LABEL {
            let u = u.clone();
            std::thread::spawn(move || run_commands(&u));
        }
        Value::Null
    });
    ui.open_window(
        LABEL,
        "Reviewer",
        WebviewKind::Reviewer,
        Content::Html(crate::pages::reviewer_html(&ui.shell.web)),
    )
}

fn card_ids(shell: &Shell) -> Vec<i64> {
    let ids: anki_proto::search::SearchResponse = shell
        .api
        .call(
            "search_cards",
            &anki_proto::search::SearchRequest {
                search: String::new(),
                order: None,
            },
        )
        .unwrap_or_default();
    ids.ids
}

fn run_commands(ui: &Ui) {
    println!("ready");
    for line in std::io::stdin().lock().lines() {
        let Ok(line) = line else { break };
        let parts: Vec<&str> = line.split_whitespace().collect();
        match parts[..] {
            ["seed", n] => {
                if let Err(err) = seed(&ui.shell, n.parse().unwrap_or(0)) {
                    eprintln!("seed failed: {err}");
                }
                println!("seeded");
            }
            ["cycle", offset, count] => {
                let ids = card_ids(&ui.shell);
                let offset: usize = offset.parse().unwrap_or(0);
                let end = (offset + count.parse::<usize>().unwrap_or(0)).min(ids.len());
                show_cards(ui, &ids[offset.min(end)..end]);
                println!("cycled");
            }
            ["quit"] => break,
            _ => eprintln!("unknown bench command: {line}"),
        }
    }
    ui.app.exit(0);
}

/// Synthetic Basic notes, with a little MathJax so the card renderer has
/// work to do.
fn seed(shell: &Shell, n: usize) -> Result<(), ApiError> {
    let have = card_ids(shell).len();
    if have >= n {
        return Ok(());
    }
    let ntid: NotetypeId = shell.api.call(
        "get_notetype_id_by_name",
        &anki_proto::generic::String {
            val: "Basic".into(),
        },
    )?;
    for i in have..n {
        let mut note: Note = shell.api.call("new_note", &ntid)?;
        note.fields[0] = format!("Question {i}: what is \\(x^{{{i}}} \\cdot x\\)?");
        note.fields[1] = format!("\\(x^{{{}}}\\) <b>answer</b> {i}", i + 1);
        let _: anki_proto::notes::AddNoteResponse = shell.api.call(
            "add_note",
            &AddNoteRequest {
                note: Some(note),
                deck_id: 1,
            },
        )?;
    }
    Ok(())
}

fn show_cards(ui: &Ui, ids: &[i64]) {
    for &cid in ids {
        let Ok(out) = ui.shell.api.call::<RenderCardResponse>(
            "render_existing_card",
            &RenderExistingCardRequest {
                card_id: cid,
                browser: false,
                partial_render: false,
            },
        ) else {
            continue;
        };
        let style = format!("<style>{}</style>", out.css);
        let q = style.clone() + &nodes_to_text(&out.question_nodes);
        let a = style + &nodes_to_text(&out.answer_nodes);
        ui.eval(
            LABEL,
            format!(
                "_showQuestion({}, {}, 'card card1');",
                js_string(&q),
                js_string(&a)
            ),
        );
        std::thread::sleep(INTERVAL);
        ui.eval(
            LABEL,
            format!("_showAnswer({}, 'card card1');", js_string(&a)),
        );
        std::thread::sleep(INTERVAL);
    }
}

fn nodes_to_text(nodes: &[RenderedTemplateNode]) -> String {
    let mut out = String::new();
    for node in nodes {
        match &node.value {
            Some(Node::Text(t)) => out += t,
            Some(Node::Replacement(r)) => out += &r.current_text,
            None => {}
        }
    }
    strip_av_tags(&out)
}

fn strip_av_tags(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(start) = rest.find("[anki:play:") {
        out += &rest[..start];
        rest = match rest[start..].find(']') {
            Some(end) => &rest[start + end + 1..],
            None => "",
        };
    }
    out + rest
}

fn js_string(s: &str) -> String {
    serde_json::to_string(s).unwrap()
}
