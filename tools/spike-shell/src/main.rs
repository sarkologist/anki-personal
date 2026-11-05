// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Spike: Anki's web UI and Rust backend in a native shell on the OS WebView,
//! without Python, Qt or a bundled Chromium. See PLAN.md.

use std::borrow::Cow;
use std::collections::HashMap;
use std::path::Path;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use std::time::Instant;

use anki::backend::init_backend;
use anki::backend::Backend;
use anki_proto::card_rendering::rendered_template_node::Value;
use anki_proto::card_rendering::RenderCardResponse;
use anki_proto::card_rendering::RenderExistingCardRequest;
use anki_proto::card_rendering::RenderedTemplateNode;
use prost::Message;
use tao::dpi::LogicalPosition;
use tao::dpi::LogicalSize;
use tao::event::Event;
use tao::event::WindowEvent;
use tao::event_loop::ControlFlow;
use tao::event_loop::EventLoopBuilder;
use tao::window::WindowBuilder;
use wry::http::header::CONTENT_TYPE;
use wry::http::Request;
use wry::http::Response;
use wry::WebViewBuilder;

mod editor;

const ORIGIN: &str = "anki://localhost/";

struct Args {
    collection: PathBuf,
    media: PathBuf,
    web: PathBuf,
    mode: String,
    search: String,
    x: f64,
    y: f64,
    width: f64,
    height: f64,
    cards: usize,
    interval_ms: u64,
    rounds: usize,
    note: i64,
}

fn parse_args() -> Args {
    let mut map = HashMap::new();
    let mut it = std::env::args().skip(1);
    while let Some(key) = it.next() {
        let value = it.next().unwrap_or_default();
        map.insert(key.trim_start_matches("--").to_string(), value);
    }
    let get = |k: &str, default: &str| map.get(k).cloned().unwrap_or(default.into());
    Args {
        collection: get("collection", "").into(),
        media: get("media", "").into(),
        web: get("web", "out/qt/_aqt/data/web").into(),
        mode: get("mode", "reviewer"),
        search: get("search", ""),
        x: get("x", "100").parse().unwrap(),
        y: get("y", "100").parse().unwrap(),
        width: get("width", "1600").parse().unwrap(),
        height: get("height", "1000").parse().unwrap(),
        cards: get("cards", "50").parse().unwrap(),
        interval_ms: get("interval-ms", "300").parse().unwrap(),
        rounds: get("rounds", "1").parse().unwrap(),
        note: get("note", "0").parse().unwrap(),
    }
}

/// The protobuf endpoints, keyed by snake_case method name.
struct Api {
    backend: Backend,
    methods: HashMap<String, (u32, u32)>,
}

impl Api {
    fn call<T: Message + Default>(&self, name: &str, input: &impl Message) -> T {
        let out = self.raw(name, &input.encode_to_vec()).expect(name);
        T::decode(out.as_slice()).unwrap()
    }

    fn raw(&self, name: &str, input: &[u8]) -> Result<Vec<u8>, Vec<u8>> {
        let Some(&(service, method)) = self.methods.get(name) else {
            return Err(format!("unknown method {name}").into_bytes());
        };
        self.backend.run_service_method(service, method, input)
    }
}

fn camel_to_snake(name: &str) -> String {
    let mut out = String::new();
    for c in name.chars() {
        if c.is_ascii_uppercase() {
            out.push('_');
            out.push(c.to_ascii_lowercase());
        } else {
            out.push(c);
        }
    }
    out
}

fn mime(path: &str) -> &'static str {
    match Path::new(path).extension().and_then(|e| e.to_str()) {
        Some("html") => "text/html",
        Some("js") | Some("mjs") => "text/javascript",
        Some("css") => "text/css",
        Some("json") | Some("map") => "application/json",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("gif") => "image/gif",
        Some("webp") => "image/webp",
        Some("woff") => "font/woff",
        Some("woff2") => "font/woff2",
        Some("ttf") => "font/ttf",
        Some("mp3") => "audio/mpeg",
        _ => "application/octet-stream",
    }
}

fn percent_decode(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(b) = u8::from_str_radix(&s[i + 1..i + 3], 16) {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn respond(status: u16, ctype: &str, body: Vec<u8>) -> Response<Cow<'static, [u8]>> {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, ctype)
        .header("Access-Control-Allow-Origin", "*")
        .body(Cow::Owned(body))
        .unwrap()
}

/// What mediasrv does: protobuf POSTs, bundled assets, media files.
fn handle(
    api: &Api,
    web: &Path,
    media: &Path,
    page: &str,
    req: Request<Vec<u8>>,
) -> Response<Cow<'static, [u8]>> {
    let path = percent_decode(req.uri().path().trim_start_matches('/'));
    if path.is_empty() {
        return respond(200, "text/html", page.as_bytes().to_vec());
    }
    if let Some(rest) = path.strip_prefix("_anki/") {
        if req.method() == "POST" {
            return match api.raw(&camel_to_snake(rest), req.body()) {
                Ok(out) => respond(200, "application/binary", out),
                Err(err) => respond(500, "text/plain", err),
            };
        }
        let file = web.join(rest);
        return match std::fs::read(&file) {
            Ok(data) => respond(200, mime(rest), data),
            Err(_) => {
                eprintln!("404 {path}");
                respond(404, "text/plain", vec![])
            }
        };
    }
    match std::fs::read(media.join(&path)) {
        Ok(data) => respond(200, mime(&path), data),
        Err(_) => respond(404, "text/plain", vec![]),
    }
}

/// `AnkiWebView.stdHtml`, minus Qt.
fn std_html(web: &Path, body: &str, css: &[&str], js: &[&str], default_css: bool) -> String {
    let mut head = format!("<base href=\"{ORIGIN}\">");
    if default_css {
        let webview_css = std::fs::read_to_string(web.join("css/webview.css")).unwrap();
        head += &format!("<style>{webview_css}</style>");
        head += "<style>body { zoom: 1; background-color: var(--canvas); } \
                 html { font-family: \"Helvetica\"; }</style>";
    }
    for c in css {
        head += &format!("<link rel=\"stylesheet\" type=\"text/css\" href=\"_anki/{c}\">");
    }
    for j in ["js/webview.js", "js/vendor/jquery.min.js"]
        .iter()
        .chain(js)
    {
        head += &format!("<script src=\"_anki/{j}\"></script>");
    }
    format!(
        "<!doctype html><html class=\"mac\" dir=\"ltr\" data-bs-theme=\"light\">\
         <head><meta charset=\"utf-8\">{head}</head><body class=\"isMac\">{body}</body></html>"
    )
}

fn nodes_to_text(nodes: &[RenderedTemplateNode]) -> String {
    let mut out = String::new();
    for node in nodes {
        match &node.value {
            Some(Value::Text(t)) => out += t,
            Some(Value::Replacement(r)) => out += &r.current_text,
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
        match rest[start..].find(']') {
            Some(end) => rest = &rest[start + end + 1..],
            None => {
                rest = "";
            }
        }
    }
    out + rest
}

fn js_string(s: &str) -> String {
    serde_json::to_string(s).unwrap()
}

#[derive(Debug)]
enum UserEvent {
    Eval(String),
    Exit,
}

fn main() {
    let t0 = Instant::now();
    let args = parse_args();
    let methods: HashMap<String, (u32, u32)> =
        serde_json::from_str::<HashMap<String, (u32, u32)>>(include_str!("../methods.json"))
            .unwrap();

    let init = anki_proto::backend::BackendInit {
        preferred_langs: vec!["en".into()],
        locale_folder_path: String::new(),
        server: false,
    };
    let backend = init_backend(&init.encode_to_vec()).unwrap();
    let api = Arc::new(Api { backend, methods });

    // the backend gets a scratch media folder; card media is only read, by us
    let scratch_media = args.collection.with_extension("media");
    std::fs::create_dir_all(&scratch_media).unwrap();
    let _: anki_proto::generic::Empty = api.call(
        "open_collection",
        &anki_proto::collection::OpenCollectionRequest {
            collection_path: args.collection.to_string_lossy().into(),
            media_folder_path: scratch_media.to_string_lossy().into(),
            media_db_path: args
                .collection
                .with_extension("mdb")
                .to_string_lossy()
                .into(),
        },
    );
    eprintln!("[{:>6}ms] collection open", t0.elapsed().as_millis());

    let web = std::fs::canonicalize(&args.web).unwrap();
    let page = match args.mode.as_str() {
        "editor" => std_html(&web, "", &["css/editor.css"], &["js/editor.js"], false),
        _ => std_html(
            &web,
            "<div id=\"_mark\" hidden>&#x2605;</div><div id=\"_flag\" hidden>&#x2691;</div>\
             <div id=\"qa\" dir=\"auto\"></div>",
            &["css/reviewer.css"],
            &["js/reviewer.js"],
            true,
        ),
    };

    let event_loop = EventLoopBuilder::<UserEvent>::with_user_event().build();
    let proxy = event_loop.create_proxy();
    let window = WindowBuilder::new()
        .with_title("Anki spike")
        .with_inner_size(LogicalSize::new(args.width, args.height))
        .with_position(LogicalPosition::new(args.x, args.y))
        .with_focused(false)
        .build(&event_loop)
        .unwrap();

    let handler_api = api.clone();
    let media = args.media.clone();
    let handler_web = web.clone();
    let ipc_proxy = proxy.clone();
    let driver = Driver {
        api: api.clone(),
        proxy: proxy.clone(),
        args_search: args.search.clone(),
        cards: args.cards,
        interval: Duration::from_millis(args.interval_ms),
        rounds: args.rounds,
        mode: args.mode.clone(),
        note: args.note,
        t0,
    };
    let driver = Arc::new(driver);
    let webview = WebViewBuilder::new()
        .with_custom_protocol("anki".into(), move |_id, req| {
            handle(&handler_api, &handler_web, &media, &page, req)
        })
        .with_initialization_script(
            "window.pycmd = window.bridgeCommand = function (arg, cb) { \
                 window.ipc.postMessage(String(arg)); \
                 if (cb) { cb(null); } return false; }; \
             window.__errors = []; \
             window.addEventListener('error', (e) => { window.__errors.push(String(e.message)); \
                 window.ipc.postMessage('spike:js error: ' + e.message); }); \
             window.addEventListener('unhandledrejection', (e) => { window.__errors.push(String(e.reason)); \
                 window.ipc.postMessage('spike:js rejection: ' + e.reason); }); \
             document.addEventListener('DOMContentLoaded', () => pycmd('domDone'));",
        )
        .with_ipc_handler(move |req: Request<String>| {
            let msg = req.body();
            if msg == "domDone" {
                let driver = driver.clone();
                std::thread::spawn(move || driver.run());
            } else if let Some(rest) = msg.strip_prefix("spike:") {
                eprintln!("[{:>6}ms] {rest}", t0.elapsed().as_millis());
                if rest == "exit" {
                    let _ = ipc_proxy.send_event(UserEvent::Exit);
                }
            }
        })
        .with_url(ORIGIN)
        .build(&window)
        .unwrap();

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::UserEvent(UserEvent::Eval(js)) => {
                let _ = webview.evaluate_script(&js);
            }
            Event::UserEvent(UserEvent::Exit)
            | Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => *control_flow = ControlFlow::Exit,
            _ => {}
        }
    });
}

struct Driver {
    api: Arc<Api>,
    proxy: tao::event_loop::EventLoopProxy<UserEvent>,
    args_search: String,
    cards: usize,
    interval: Duration,
    rounds: usize,
    mode: String,
    note: i64,
    t0: Instant,
}

impl Driver {
    fn eval(&self, js: String) {
        let _ = self.proxy.send_event(UserEvent::Eval(js));
    }

    fn run(&self) {
        eprintln!("[{:>6}ms] page ready", self.t0.elapsed().as_millis());
        match self.mode.as_str() {
            "editor" => editor::run(self),
            "idle" => {}
            "stdin" => self.run_commands(),
            _ => self.run_reviewer(),
        }
    }

    fn card_ids(&self) -> Vec<i64> {
        let ids: anki_proto::search::SearchResponse = self.api.call(
            "search_cards",
            &anki_proto::search::SearchRequest {
                search: self.args_search.clone(),
                order: None,
            },
        );
        ids.ids
    }

    /// For the benchmark harness: `cycle <offset> <count>` lines on stdin.
    fn run_commands(&self) {
        let ids = self.card_ids();
        println!("ready");
        for line in std::io::stdin().lines() {
            let line = line.unwrap();
            let parts: Vec<usize> = line
                .split_whitespace()
                .skip(1)
                .filter_map(|p| p.parse().ok())
                .collect();
            if let [offset, count] = parts[..] {
                self.show_cards(&ids[offset..offset + count], false);
            }
            println!("cycled");
        }
    }

    /// Show question, then answer, for each card, the way the Qt reviewer
    /// drives reviewer.js.
    fn run_reviewer(&self) {
        let ids: Vec<i64> = self.card_ids().into_iter().take(self.cards).collect();
        eprintln!("cycling {} cards x {} rounds", ids.len(), self.rounds);
        for round in 0..self.rounds {
            self.show_cards(&ids, round == 0);
            self.eval(format!("pycmd('spike:round {} done')", round + 1));
        }
    }

    fn show_cards(&self, ids: &[i64], mark_first: bool) {
        {
            for (i, &cid) in ids.iter().enumerate() {
                let out: RenderCardResponse = self.api.call(
                    "render_existing_card",
                    &RenderExistingCardRequest {
                        card_id: cid,
                        browser: false,
                        partial_render: false,
                    },
                );
                let style = format!("<style>{}</style>", out.css);
                let q = style.clone() + &nodes_to_text(&out.question_nodes);
                let a = style + &nodes_to_text(&out.answer_nodes);
                let first = mark_first && i == 0;
                let shown = if first {
                    "requestAnimationFrame(() => requestAnimationFrame(\
                     () => pycmd('spike:first card painted')));"
                } else {
                    ""
                };
                self.eval(format!(
                    "_showQuestion({}, {}, 'card card1'); {shown}",
                    js_string(&q),
                    js_string(&a)
                ));
                std::thread::sleep(self.interval);
                self.eval(format!("_showAnswer({}, 'card card1');", js_string(&a)));
                std::thread::sleep(self.interval);
            }
        }
    }
}
