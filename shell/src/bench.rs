// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! `--bench`: the main window's reviewer driven by stdin, for the memory
//! harness (`shell/harness/bench.py`). It prints `ready` once the page is
//! up, then answers each `cycle <offset> <count>` line with `cycled` after
//! showing each card's question and answer, as `harness/stock/cycle.py`
//! drives the Qt reviewer. `seed <n>` adds n synthetic notes first if the
//! collection has fewer cards, so the check can run on an empty profile.
//! `latency` prints `latency <json>`: how long each side took to render in
//! the shell (`host`) and to show in the page (`page`, MathJax included),
//! in ms.

use std::io::BufRead;
use std::sync::mpsc;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;
use std::time::Instant;

use anki_proto::notes::AddNoteRequest;
use anki_proto::notes::Note;
use anki_proto::notetypes::NotetypeId;
use anyhow::Result;
use serde_json::json;
use serde_json::Value;

use crate::api::ApiError;
use crate::app::Ui;
use crate::app::MAIN;
use crate::main_window;
use crate::Shell;

const INTERVAL: Duration = Duration::from_millis(250);

/// Times "eval to shown" in the page, the same way in both apps
/// (`bench.py` injects it into the Qt reviewer too).
pub const PAGE_TIMING_JS: &str = include_str!("../harness/page-timing.js");

#[derive(Default)]
struct Timings {
    host_q: Vec<f64>,
    host_a: Vec<f64>,
}

static TIMINGS: Mutex<Option<Timings>> = Mutex::new(None);
static PAGE_REPLY: Mutex<Option<mpsc::Sender<String>>> = Mutex::new(None);

pub fn start(ui: &Arc<Ui>) -> Result<()> {
    *TIMINGS.lock().unwrap() = Some(Timings::default());
    let u = ui.clone();
    let started = std::sync::atomic::AtomicBool::new(false);
    ui.shell.bridge.register("domDone", move |_, label, _| {
        u.evals.dom_done(&u.app, label);
        main_window::dom_done(&u, label);
        if label == MAIN && !started.swap(true, std::sync::atomic::Ordering::SeqCst) {
            u.eval(MAIN, PAGE_TIMING_JS.into());
            let u = u.clone();
            std::thread::spawn(move || run_commands(&u));
        }
        Value::Null
    });
    ui.shell.bridge.register("benchLatency", |_, _, json| {
        if let Some(tx) = PAGE_REPLY.lock().unwrap().take() {
            let _ = tx.send(json.to_string());
        }
        Value::Null
    });
    main_window::start_bench_review(ui);
    Ok(())
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

fn run_commands(ui: &Arc<Ui>) {
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
            ["latency"] => println!("latency {}", latency(ui)),
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

/// Each card's question, then its answer, through the real reviewer.
fn show_cards(ui: &Arc<Ui>, ids: &[i64]) {
    for &cid in ids {
        let t0 = Instant::now();
        let effects = ui
            .main
            .reviewer
            .lock()
            .unwrap()
            .show_card(&ui.shell.api, cid);
        let host_q = t0.elapsed().as_secs_f64() * 1000.0;
        main_window::apply(ui, effects);
        std::thread::sleep(INTERVAL);
        let t0 = Instant::now();
        let effects = ui
            .main
            .reviewer
            .lock()
            .unwrap()
            .show_answer(&ui.shell.api, "");
        let host_a = t0.elapsed().as_secs_f64() * 1000.0;
        main_window::apply(ui, effects);
        if let Some(t) = TIMINGS.lock().unwrap().as_mut() {
            t.host_q.push(host_q);
            t.host_a.push(host_a);
        }
        std::thread::sleep(INTERVAL);
    }
}

fn latency(ui: &Ui) -> Value {
    let (tx, rx) = mpsc::channel();
    *PAGE_REPLY.lock().unwrap() = Some(tx);
    ui.eval(
        MAIN,
        "pycmd('benchLatency:' + JSON.stringify(window._benchLat || null));".into(),
    );
    let page: Value = rx
        .recv_timeout(Duration::from_secs(30))
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Value::Null);
    let t = TIMINGS.lock().unwrap();
    let t = t.as_ref();
    json!({
        "host": { "q": t.map(|t| t.host_q.clone()), "a": t.map(|t| t.host_a.clone()) },
        "page": page,
    })
}
