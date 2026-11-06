// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The `anki://` protocol: what `qt/aqt/mediasrv.py` serves over HTTP.
//!
//! - `POST /_anki/<camelMethod>`: page API calls, gated per webview kind
//! - `GET /_anki/legacyPageData?id=<label>`: the HTML the shell assembled
//! - `/_anki/...`, SvelteKit pages: bundled web assets
//! - `POST /_shell/bridge`: `pycmd`/`bridgeCommand` messages
//! - anything else: a file in the collection's media folder

use std::path::Component;
use std::path::Path;
use std::path::PathBuf;

use tauri::http::header::CONTENT_SECURITY_POLICY;
use tauri::http::header::CONTENT_TYPE;
use tauri::http::Request;
use tauri::http::Response;
use tauri::http::StatusCode;

use crate::api::camel_to_snake;
use crate::pages::WebviewKind;
use crate::pages::ORIGIN;
use crate::Shell;

pub const BRIDGE_PATH: &str = "_shell/bridge";
/// Sent by the bridge script. A custom header can't be sent by a no-cors
/// request, so content from other origins (an iframe in a field, say) can't
/// reach the bridge.
pub const BRIDGE_HEADER: &str = "X-Anki-Bridge";

/// Backend methods any page with API access may call
/// (`mediasrv.exposed_backend_list`).
const EXPOSED_BACKEND: &[&str] = &[
    // CollectionService
    "latest_progress",
    "get_custom_colours",
    // DeckService
    "get_deck_names",
    // I18nService
    "i18n_resources",
    // ImportExportService
    "get_csv_metadata",
    "get_import_anki_package_presets",
    // NotesService
    "get_field_names",
    "get_note",
    // NotetypesService
    "get_notetype_names",
    "get_change_notetype_info",
    // StatsService
    "card_stats",
    "get_review_logs",
    "graphs",
    "get_graph_preferences",
    "set_graph_preferences",
    // TagsService
    "complete_tag",
    // ImageOcclusionService
    "get_image_for_occlusion",
    "add_image_occlusion_note",
    "get_image_occlusion_note",
    "update_image_occlusion_note",
    "get_image_occlusion_fields",
    // SchedulerService
    "compute_fsrs_params",
    "compute_optimal_retention",
    "set_wants_abort",
    "evaluate_params_legacy",
    "get_optimal_retention_parameters",
    "simulate_fsrs_review",
    "simulate_fsrs_workload",
    // DeckConfigService
    "get_ignored_before_count",
    "get_retention_workload",
];

/// `mediasrv.post_handler_list` entries that are plain backend calls.
const FORWARDED_POST_HANDLERS: &[&str] = &[
    "congrats_info",
    "get_deck_configs_for_update",
    "update_deck_configs",
    "import_csv",
    "import_anki_package",
    "import_json_file",
    "import_json_string",
];

/// `mediasrv.post_handler_list` entries that need the shell's UI state
/// (the reviewer, open dialogs); they go to [Shell::frontend].
const SHELL_POST_HANDLERS: &[&str] = &[
    "get_scheduling_states_with_context",
    "set_scheduling_states",
    "change_notetype",
    "import_done",
    "search_in_browser",
    "deck_options_require_close",
    "deck_options_ready",
    "save_custom_colours",
];

/// The only endpoints pages without API access may call (the reviewer and
/// previewer show card templates, which may contain third-party JS).
const NO_ACCESS_WHITELIST: &[&str] = &[
    "getSchedulingStatesWithContext",
    "setSchedulingStates",
    "i18nResources",
    "congratsInfo",
];

/// What the main window's own pages may call (the deck browser and
/// overview; `deckbrowser.py`/`overview.py` did this in Python). The
/// reviewer, in the same webview, gets only [NO_ACCESS_WHITELIST]: the
/// shell changes the webview's kind before it navigates.
const MAIN_PAGE_BACKEND: &[&str] = &[
    "deck_tree",
    "set_deck_collapsed",
    "rename_deck",
    "remove_decks",
    "reparent_decks",
    "new_deck",
    "add_deck",
    "get_deck",
    "get_current_deck",
    "set_current_deck",
    "get_queued_cards",
    "studied_today",
    "render_markdown",
    "rebuild_filtered_deck",
    "empty_filtered_deck",
    "unbury_deck",
    "sched_timing_today",
    "congrats_info",
    "i18n_resources",
    "get_undo_status",
];

/// The main window's own pages (the ones with [MAIN_PAGE_BACKEND]); they
/// can't be framed (by card content, say).
const MAIN_PAGE_ROUTES: &[&str] = &["deck-browser", "deck-overview"];

/// SvelteKit routes, served through the SvelteKit fallback page.
const SVELTEKIT_PAGES: &[&str] = &[
    "deck-browser",
    "deck-overview",
    "toolbar",
    "graphs",
    "congrats",
    "card-info",
    "change-notetype",
    "deck-options",
    "import-anki-package",
    "import-csv",
    "import-page",
    "image-occlusion",
    "preferences",
];

pub type HttpResponse = Response<Vec<u8>>;

fn respond(status: StatusCode, ctype: &str, body: impl Into<Vec<u8>>) -> HttpResponse {
    Response::builder()
        .status(status)
        .header(CONTENT_TYPE, ctype)
        .body(body.into())
        .unwrap()
}

fn text(status: StatusCode, msg: impl Into<String>) -> HttpResponse {
    respond(status, "text/plain", msg.into())
}

pub fn handle(shell: &Shell, label: &str, req: &Request<Vec<u8>>) -> HttpResponse {
    let path = percent_decode(req.uri().path().trim_start_matches('/'));
    let post = req.method() == "POST";
    tracing::debug!(label, method = %req.method(), path, "request");
    if post && path == BRIDGE_PATH {
        let origin = req.headers().get("Origin").and_then(|v| v.to_str().ok());
        if !req.headers().contains_key(BRIDGE_HEADER)
            || origin.is_some_and(|o| o != ORIGIN.trim_end_matches('/'))
        {
            tracing::warn!(label, ?origin, "rejected bridge call");
            return text(
                StatusCode::FORBIDDEN,
                "bridge calls come from Anki pages only",
            );
        }
        let cmd = String::from_utf8_lossy(req.body());
        let out = shell.bridge.dispatch(shell, label, &cmd);
        return respond(StatusCode::OK, "application/json", out.to_string());
    }

    let mut path = path;
    let first = path.split('/').next().unwrap_or_default();
    if MAIN_PAGE_ROUTES.contains(&first) {
        let mut resp = bundled_file(&shell.web, "sveltekit/index.html", shell.night.get());
        resp.headers_mut().insert(
            CONTENT_SECURITY_POLICY,
            "frame-ancestors 'none'".parse().unwrap(),
        );
        return resp;
    }
    if SVELTEKIT_PAGES.contains(&first) {
        path = format!("_anki/sveltekit/_app/{path}");
    } else if let Some(rest) = path.strip_prefix("_app/") {
        path = format!("_anki/sveltekit/_app/{rest}");
    }

    if let Some(rest) = path.strip_prefix("_anki/") {
        if !rest.contains('/') {
            if post {
                return handle_post(shell, label, rest, req);
            }
            if rest == "legacyPageData" {
                return legacy_page(shell, label, req);
            }
        }
        return bundled_file(&shell.web, &remap_legacy_path(rest), shell.night.get());
    }
    if let Some(rest) = path.strip_prefix("_addons/") {
        return match shell.addon_files.resolve(rest) {
            Some((folder, file)) => themed(file_response(&folder, &file), shell.night.get()),
            None => text(StatusCode::NOT_FOUND, format!("not exported: {rest}")),
        };
    }
    match shell.media_folder() {
        Some(media) => local_file(&media, &path),
        None => text(StatusCode::NOT_FOUND, "collection not open"),
    }
}

/// Where webviews may navigate: Anki's own pages, and the web for frames in
/// cards and fields (a YouTube embed, say). The check can't tell frames
/// apart, so link clicks in the main frame go to the browser through the
/// bridge script instead (`AnkiWebPage.acceptNavigationRequest`).
pub fn allowed_navigation(url: &tauri::Url) -> bool {
    match url.scheme() {
        "anki" => url.host_str() == Some("localhost"),
        "http" | "https" | "about" | "data" | "blob" => true,
        _ => false,
    }
}

/// Old top-level `/_anki/foo.css` style references.
fn remap_legacy_path(rest: &str) -> String {
    let (dir, file) = match rest.rsplit_once('/') {
        Some((dir, file)) => (dir, file),
        None => ("", rest),
    };
    let (base, ext) = file.rsplit_once('.').unwrap_or((file, ""));
    match (dir, ext) {
        ("", "css") => format!("css/{file}"),
        ("", "js") if ["jquery-ui", "jquery", "plot"].contains(&base) => {
            format!("js/vendor/{file}")
        }
        ("", "js") => format!("js/{file}"),
        ("js/vendor", "js") if base == "jquery" || base == "jquery-ui" => {
            format!("js/vendor/{base}.min.js")
        }
        _ => rest.to_string(),
    }
}

fn handle_post(shell: &Shell, label: &str, endpoint: &str, req: &Request<Vec<u8>>) -> HttpResponse {
    // guard against opaque requests (mediasrv does the same)
    let ctype = req
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or_default();
    // a sandboxed frame (an add-on's web view) has no API
    if req
        .headers()
        .get("Origin")
        .is_some_and(|o| o != ORIGIN.trim_end_matches('/'))
    {
        tracing::warn!(endpoint, label, "rejected API call from another origin");
        return text(StatusCode::FORBIDDEN, "no API access");
    }
    if ctype != "application/binary" {
        tracing::warn!(
            endpoint,
            label,
            "rejected API call without binary content type"
        );
        return text(StatusCode::FORBIDDEN, "unexpected content type");
    }
    let kind = shell.pages.get(label).map(|p| p.kind);
    let method = camel_to_snake(endpoint);
    if kind == Some(WebviewKind::Main) {
        // The kind changes before the webview navigates, so the document
        // leaving (the reviewer, with card JS) still runs as `Main` for a
        // moment: the API also needs the capability that only the new
        // document got, in its URL.
        let cap = shell.pages.get(label).and_then(|p| p.cap);
        let sent = req
            .headers()
            .get(crate::pages::CAP_HEADER)
            .and_then(|v| v.to_str().ok());
        if cap.is_none() || sent != cap.as_deref() {
            tracing::warn!(
                endpoint,
                label,
                "rejected API call from a non-main document"
            );
            return text(StatusCode::FORBIDDEN, "no API access");
        }
        if !MAIN_PAGE_BACKEND.contains(&method.as_str()) {
            tracing::warn!(endpoint, label, "rejected API call from the main window");
            return text(StatusCode::FORBIDDEN, "no API access");
        }
        if shell.media_folder().is_none() {
            return text(StatusCode::NOT_FOUND, "collection not open");
        }
        return match shell.api.raw(&method, req.body()) {
            Ok(data) if data.is_empty() => text(StatusCode::NO_CONTENT, ""),
            Ok(data) => respond(StatusCode::OK, "application/binary", data),
            Err(err) => text(StatusCode::INTERNAL_SERVER_ERROR, err.message),
        };
    }
    if !kind.is_some_and(WebviewKind::has_api_access) && !NO_ACCESS_WHITELIST.contains(&endpoint) {
        tracing::warn!(
            endpoint,
            label,
            ?kind,
            "rejected API call from page without access"
        );
        return text(StatusCode::FORBIDDEN, "no API access");
    }
    if shell.media_folder().is_none() {
        return text(StatusCode::NOT_FOUND, "collection not open");
    }

    let result = if SHELL_POST_HANDLERS.contains(&method.as_str()) {
        shell.frontend(label, &method, req.body())
    } else if EXPOSED_BACKEND.contains(&method.as_str())
        || FORWARDED_POST_HANDLERS.contains(&method.as_str())
        || kind.is_some_and(|k| k.own_backend().contains(&method.as_str()))
    {
        shell.api.raw(&method, req.body()).map_err(|e| e.message)
    } else {
        return text(StatusCode::NOT_FOUND, format!("{endpoint} not found"));
    };
    match result {
        Ok(data) if data.is_empty() => text(StatusCode::NO_CONTENT, ""),
        Ok(data) => respond(StatusCode::OK, "application/binary", data),
        Err(err) => {
            tracing::warn!(endpoint, %err, "API call failed");
            text(StatusCode::INTERNAL_SERVER_ERROR, err)
        }
    }
}

fn legacy_page(shell: &Shell, label: &str, req: &Request<Vec<u8>>) -> HttpResponse {
    let id = req
        .uri()
        .query()
        .unwrap_or_default()
        .split('&')
        .find_map(|kv| kv.strip_prefix("id="))
        .map(percent_decode)
        .unwrap_or_default();
    // a webview's own pages only (`label`, `label-bottom`, ...): framed
    // in another webview, a page would run with that one's bridge and
    // API, so a field can't frame the reviewer's card JS in the editor
    if id != label && !id.starts_with(&format!("{label}-")) {
        return text(StatusCode::NOT_FOUND, "page not found");
    }
    let Some(page) = shell.pages.get(&id) else {
        return text(StatusCode::NOT_FOUND, "page not found");
    };
    let Some(html) = page.html else {
        return text(StatusCode::NOT_FOUND, "page not found");
    };
    let html = crate::theme::apply_to_html(&html, shell.night.get());
    if page.kind == WebviewKind::AddonView {
        let mut resp = respond(StatusCode::OK, "text/html", html);
        resp.headers_mut().insert(
            CONTENT_SECURITY_POLICY,
            "sandbox allow-scripts".parse().unwrap(),
        );
        return resp;
    }
    if page.kind != WebviewKind::Editor {
        return respond(StatusCode::OK, "text/html", html);
    }
    // Keep JS in field content from running in the editor, which has API
    // access; the nonce lets editor code authorise its own scripts.
    let nonce = data_encoding::BASE64URL_NOPAD.encode(&rand::random::<[u8; 16]>());
    let html = html.replacen(
        "<head>",
        &format!("<head><meta name=\"anki-csp-nonce\" content=\"{nonce}\">"),
        1,
    );
    let mut resp = respond(StatusCode::OK, "text/html", html);
    resp.headers_mut().insert(
        CONTENT_SECURITY_POLICY,
        // frames: the shell's own pages (an add-on's side panel) and the
        // web (embeds), not media documents, which would share the
        // editor's origin and bridge
        format!(
            "script-src 'nonce-{nonce}' {ORIGIN}_anki/ {ORIGIN}_addons/; \
             frame-src {ORIGIN}_anki/legacyPageData https: http:; object-src 'none'"
        )
        .parse()
        .unwrap(),
    );
    resp
}

/// SvelteKit's page shell, with the page font every other page gets from
/// `std_html`, in the current theme.
fn sveltekit_page(html: &[u8], night: bool) -> Vec<u8> {
    let html = crate::theme::apply_to_html(&String::from_utf8_lossy(html), night);
    html.replacen(
        "</head>",
        &format!("<style>{}</style></head>", crate::pages::SYSTEM_FONT_CSS),
        1,
    )
    .into_bytes()
}

fn bundled_file(web: &Path, path: &str, night: bool) -> HttpResponse {
    let immutable = path.contains("immutable");
    let path = if path.starts_with("sveltekit/") && !immutable {
        "sveltekit/index.html"
    } else {
        path
    };
    let Some(full) = safe_join(web, path) else {
        return text(StatusCode::FORBIDDEN, format!("invalid path: {path}"));
    };
    match std::fs::read(&full) {
        Ok(mut data) => {
            if path == "sveltekit/index.html" {
                data = sveltekit_page(&data, night);
            }
            let mut resp = respond(StatusCode::OK, mime(path), data);
            if immutable {
                resp.headers_mut()
                    .insert("Cache-Control", "max-age=31536000".parse().unwrap());
            }
            resp
        }
        Err(_) => {
            tracing::debug!(path, "bundled file not found");
            text(StatusCode::NOT_FOUND, format!("invalid path: {path}"))
        }
    }
}

fn local_file(root: &Path, path: &str) -> HttpResponse {
    let mut resp = file_response(root, path);
    // a media file opened as a document (framed by a card or field) gets
    // an origin of its own, without the bridge or API
    let document = resp
        .headers()
        .get(CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .is_some_and(|ctype| {
            matches!(
                ctype,
                "text/html" | "application/xhtml+xml" | "image/svg+xml"
            )
        });
    if resp.status() == StatusCode::OK && document {
        resp.headers_mut()
            .insert(CONTENT_SECURITY_POLICY, "sandbox".parse().unwrap());
    }
    resp
}

/// `root/path` (guarded), as is.
/// An HTML response in the current theme (an add-on's panel page).
fn themed(mut resp: HttpResponse, night: bool) -> HttpResponse {
    if night && resp.status() == StatusCode::OK && resp.headers()[CONTENT_TYPE] == "text/html" {
        let html = crate::theme::apply_to_html(&String::from_utf8_lossy(resp.body()), true);
        *resp.body_mut() = html.into_bytes();
    }
    resp
}

fn file_response(root: &Path, path: &str) -> HttpResponse {
    let Some(full) = safe_join(root, path) else {
        return text(StatusCode::FORBIDDEN, format!("invalid path: {path}"));
    };
    if full.is_dir() {
        return text(StatusCode::FORBIDDEN, "directories are not supported");
    }
    match std::fs::read(&full) {
        Ok(data) => respond(StatusCode::OK, mime(path), data),
        Err(_) => text(StatusCode::NOT_FOUND, format!("invalid path: {path}")),
    }
}

/// `root/path`, or None if `path` would escape `root`.
fn safe_join(root: &Path, path: &str) -> Option<PathBuf> {
    let rel = Path::new(path);
    if rel
        .components()
        .any(|c| !matches!(c, Component::Normal(_) | Component::CurDir))
    {
        return None;
    }
    Some(root.join(rel))
}

fn mime(path: &str) -> &'static str {
    let ext = Path::new(path)
        .extension()
        .and_then(|e| e.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match ext.as_str() {
        "css" => "text/css",
        "js" | "mjs" => "application/javascript",
        "html" | "htm" => "text/html",
        "svg" => "image/svg+xml",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "webp" => "image/webp",
        "avif" => "image/avif",
        "ico" => "image/x-icon",
        "json" | "map" => "application/json",
        "woff" => "font/woff",
        "woff2" => "font/woff2",
        "ttf" => "font/ttf",
        "otf" => "font/otf",
        "mp3" => "audio/mpeg",
        "m4a" => "audio/mp4",
        "wav" => "audio/wav",
        "ogg" | "oga" => "audio/ogg",
        "mp4" => "video/mp4",
        "webm" => "video/webm",
        "pdf" => "application/pdf",
        "txt" => "text/plain",
        _ => "application/octet-stream",
    }
}

pub fn percent_decode(s: &str) -> String {
    fn hex(b: u8) -> Option<u8> {
        (b as char).to_digit(16).map(|d| d as u8)
    }
    let bytes = s.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let (Some(hi), Some(lo)) = (hex(bytes[i + 1]), hex(bytes[i + 2])) {
                out.push(hi << 4 | lo);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

#[cfg(test)]
mod test {
    use anki_proto::i18n::I18nResourcesRequest;
    use prost::Message;

    use super::*;
    use crate::api::Api;
    use crate::profile::ProfilePaths;

    struct Fixture {
        shell: Shell,
        _dir: tempfile::TempDir,
    }

    fn fixture() -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let web = dir.path().join("web");
        std::fs::create_dir_all(web.join("css")).unwrap();
        std::fs::create_dir_all(web.join("sveltekit/_app/immutable")).unwrap();
        std::fs::write(web.join("css/reviewer.css"), "body {}").unwrap();
        std::fs::write(
            web.join("sveltekit/index.html"),
            "<html><head></head><svelte>",
        )
        .unwrap();
        std::fs::write(web.join("sveltekit/_app/immutable/a.js"), "a").unwrap();
        let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), web);
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        shell.open_profile(paths.clone()).unwrap();
        std::fs::write(paths.media().join("pic.png"), "png").unwrap();
        std::fs::write(paths.media().join("page.html"), "<script>x</script>").unwrap();
        std::fs::write(paths.media().join("pic.svg"), "<svg/>").unwrap();
        std::fs::write(dir.path().join("secret.txt"), "no").unwrap();
        shell.pages.set(
            "editor",
            WebviewKind::Editor,
            Some("<html><head></head></html>".into()),
        );
        shell.pages.set(
            "reviewer",
            WebviewKind::Reviewer,
            Some("<html><head></head></html>".into()),
        );
        Fixture { shell, _dir: dir }
    }

    #[test]
    fn add_on_files_are_served_only_where_exported() {
        let f = fixture();
        let addons = f._dir.path().join("addons21");
        std::fs::create_dir_all(addons.join("frontier/web")).unwrap();
        std::fs::write(addons.join("frontier/web/panel.html"), "<p>panel").unwrap();
        std::fs::write(addons.join("frontier/meta.json"), "{\"secret\": 1}").unwrap();
        assert_eq!(
            get(&f, "addon-panel-1", "_addons/frontier/web/panel.html").status(),
            StatusCode::NOT_FOUND
        );
        f.shell.addon_files.set_folder(Some(addons));
        f.shell.addon_files.set_exports("frontier", r"web/.*");
        let resp = get(&f, "addon-panel-1", "_addons/frontier/web/panel.html");
        assert_eq!(resp.status(), StatusCode::OK);
        assert_eq!(resp.body(), b"<p>panel");
        // a panel is the add-on's page, with its scripts and the bridge,
        // unlike media opened as a document
        assert!(!resp.headers().contains_key(CONTENT_SECURITY_POLICY));
        for path in [
            "_addons/frontier/meta.json",
            "_addons/frontier/web/../meta.json",
            "_addons/other/web/panel.html",
            // the pattern must match the whole path, as re.fullmatch
            "_addons/frontier/xweb/panel.html",
        ] {
            assert_ne!(
                get(&f, "addon-panel-1", path).status(),
                StatusCode::OK,
                "{path}"
            );
        }
    }

    fn get(f: &Fixture, label: &str, path: &str) -> HttpResponse {
        let req = Request::get(format!("anki://localhost/{path}"))
            .body(vec![])
            .unwrap();
        handle(&f.shell, label, &req)
    }

    fn post(f: &Fixture, label: &str, endpoint: &str, ctype: &str, body: Vec<u8>) -> HttpResponse {
        let req = Request::post(format!("anki://localhost/_anki/{endpoint}"))
            .header(CONTENT_TYPE, ctype)
            .body(body)
            .unwrap();
        handle(&f.shell, label, &req)
    }

    fn i18n_request() -> Vec<u8> {
        I18nResourcesRequest {
            modules: vec!["editing".into()],
        }
        .encode_to_vec()
    }

    #[test]
    fn reviewer_gets_only_the_whitelisted_endpoints() {
        let f = fixture();
        let ok = post(
            &f,
            "reviewer",
            "i18nResources",
            "application/binary",
            i18n_request(),
        );
        assert_eq!(ok.status(), StatusCode::OK);
        let denied = post(&f, "reviewer", "getDeckNames", "application/binary", vec![]);
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
        // unknown webviews are treated as untrusted
        let denied = post(&f, "nobody", "getDeckNames", "application/binary", vec![]);
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
    }

    fn post_from(f: &Fixture, cap: Option<&str>, endpoint: &str, body: Vec<u8>) -> HttpResponse {
        let mut req = Request::post(format!("anki://localhost/_anki/{endpoint}"))
            .header(CONTENT_TYPE, "application/binary");
        if let Some(cap) = cap {
            req = req.header(crate::pages::CAP_HEADER, cap);
        }
        handle(&f.shell, "main", &req.body(body).unwrap())
    }

    /// The main window showing a deck-list page whose document got `cap`.
    fn main_page(f: &Fixture, kind: WebviewKind, cap: Option<&str>) {
        f.shell
            .pages
            .set_with_cap("main", kind, None, cap.map(str::to_string));
    }

    #[test]
    fn main_window_pages_get_only_the_deck_list_api() {
        let f = fixture();
        main_page(&f, WebviewKind::Main, Some("c1"));
        let tree = anki_proto::decks::DeckTreeRequest { now: 0 }.encode_to_vec();
        let ok = post_from(&f, Some("c1"), "deckTree", tree.clone());
        assert_eq!(ok.status(), StatusCode::OK);
        // not the editor's API, nor the rest of the backend
        for endpoint in ["getDeckNames", "removeNotes", "setConfigJson"] {
            let denied = post_from(&f, Some("c1"), endpoint, vec![]);
            assert_eq!(denied.status(), StatusCode::FORBIDDEN, "{endpoint}");
        }
        // the same webview showing the reviewer gets the whitelist only
        main_page(&f, WebviewKind::Reviewer, Some("c1"));
        let denied = post_from(&f, Some("c1"), "deckTree", tree);
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
    }

    #[test]
    fn the_deck_list_api_needs_the_documents_capability() {
        // The kind changes before the webview navigates, so the reviewer's
        // document (card JS) still runs for a moment as `Main`. It never saw
        // the new page's URL, so it can't send its capability.
        let f = fixture();
        main_page(&f, WebviewKind::Main, Some("new"));
        let tree = anki_proto::decks::DeckTreeRequest { now: 0 }.encode_to_vec();
        for cap in [None, Some("old"), Some("")] {
            let denied = post_from(&f, cap, "deckTree", tree.clone());
            assert_eq!(denied.status(), StatusCode::FORBIDDEN, "{cap:?}");
        }
        // a Main page without a capability (the home page) gets nothing
        main_page(&f, WebviewKind::Main, None);
        let denied = post_from(&f, Some(""), "deckTree", tree);
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
    }

    #[test]
    fn deck_list_pages_cant_be_framed() {
        let f = fixture();
        for path in ["deck-browser", "deck-overview"] {
            let page = get(&f, "main", path);
            let csp = page.headers()[CONTENT_SECURITY_POLICY].to_str().unwrap();
            assert!(csp.contains("frame-ancestors 'none'"), "{path}: {csp}");
        }
        // the toolbar is framed by the reviewer
        let toolbar = get(&f, "main", "toolbar");
        assert!(toolbar.headers().get(CONTENT_SECURITY_POLICY).is_none());
    }

    #[test]
    fn editor_gets_the_page_api_but_not_the_whole_backend() {
        let f = fixture();
        let ok = post(&f, "editor", "getDeckNames", "application/binary", vec![]);
        assert_eq!(ok.status(), StatusCode::OK);
        // a real backend method, but not one mediasrv exposes
        let missing = post(&f, "editor", "removeNotes", "application/binary", vec![]);
        assert_eq!(missing.status(), StatusCode::NOT_FOUND);
    }

    #[test]
    fn screens_get_their_own_backend_calls_only() {
        let f = fixture();
        f.shell
            .pages
            .set("preferences", WebviewKind::Preferences, None);
        let call = |label: &str, endpoint: &str| {
            post(&f, label, endpoint, "application/binary", vec![]).status()
        };
        assert_eq!(call("preferences", "getPreferences"), StatusCode::OK);
        assert_eq!(call("preferences", "getDeckNames"), StatusCode::OK);
        assert_eq!(call("editor", "getPreferences"), StatusCode::NOT_FOUND);
        assert_eq!(call("preferences", "removeNotes"), StatusCode::NOT_FOUND);
    }

    #[test]
    fn add_on_web_views_are_sandboxed_and_get_no_api() {
        let f = fixture();
        f.shell.pages.set(
            "editor-side-w3",
            WebviewKind::AddonView,
            Some("<p>model output".into()),
        );
        let page = get(&f, "editor", "_anki/legacyPageData?id=editor-side-w3");
        assert_eq!(page.status(), StatusCode::OK);
        assert_eq!(
            page.headers()[CONTENT_SECURITY_POLICY],
            "sandbox allow-scripts"
        );
        // the sandboxed frame is in the editor's webview, but its origin is
        // opaque: the editor's API isn't its
        let req = Request::post("anki://localhost/_anki/getDeckNames")
            .header(CONTENT_TYPE, "application/binary")
            .header("Origin", "null")
            .body(vec![])
            .unwrap();
        assert_eq!(
            handle(&f.shell, "editor", &req).status(),
            StatusCode::FORBIDDEN
        );
        let req = Request::post("anki://localhost/_anki/getDeckNames")
            .header(CONTENT_TYPE, "application/binary")
            .header("Origin", "anki://localhost")
            .body(vec![])
            .unwrap();
        assert_eq!(handle(&f.shell, "editor", &req).status(), StatusCode::OK);
    }

    #[test]
    fn api_calls_need_the_binary_content_type() {
        let f = fixture();
        let denied = post(&f, "editor", "getDeckNames", "text/plain", vec![]);
        assert_eq!(denied.status(), StatusCode::FORBIDDEN);
    }

    #[test]
    fn ui_state_endpoints_go_to_the_shell() {
        let f = fixture();
        let missing = post(
            &f,
            "editor",
            "deckOptionsReady",
            "application/binary",
            vec![],
        );
        assert_eq!(missing.status(), StatusCode::INTERNAL_SERVER_ERROR);
        f.shell
            .register_frontend("deck_options_ready", |_, label, _| {
                assert_eq!(label, "editor");
                Ok(vec![])
            });
        let ok = post(
            &f,
            "editor",
            "deckOptionsReady",
            "application/binary",
            vec![],
        );
        assert_eq!(ok.status(), StatusCode::NO_CONTENT);
    }

    #[test]
    fn serves_bundled_files_media_and_sveltekit_pages() {
        let f = fixture();
        let css = get(&f, "reviewer", "_anki/css/reviewer.css");
        assert_eq!(css.status(), StatusCode::OK);
        assert_eq!(css.headers()[CONTENT_TYPE], "text/css");
        // legacy top-level reference
        assert_eq!(get(&f, "reviewer", "_anki/reviewer.css").body(), b"body {}");
        assert!(
            String::from_utf8_lossy(get(&f, "editor", "deck-options/1").body())
                .ends_with("</head><svelte>")
        );
        let asset = get(&f, "editor", "_app/immutable/a.js");
        assert_eq!(asset.body(), b"a");
        assert!(asset.headers().contains_key("Cache-Control"));
        let media = get(&f, "reviewer", "pic.png");
        assert_eq!(media.body(), b"png");
        assert_eq!(media.headers()[CONTENT_TYPE], "image/png");
    }

    #[test]
    fn sveltekit_pages_default_to_the_system_sans_serif_font() {
        // WebKit's default font is Times, where QtWebEngine's is sans-serif, and
        // the SvelteKit pages set none of their own
        let f = fixture();
        for path in ["deck-options/1", "deck-browser"] {
            let body = String::from_utf8_lossy(get(&f, "editor", path).body()).into_owned();
            assert!(
                body.contains(&format!(
                    "<style>{}</style></head>",
                    crate::pages::SYSTEM_FONT_CSS
                )),
                "{path}: {body}"
            );
        }
    }

    #[test]
    fn pages_are_served_in_the_current_theme() {
        let f = fixture();
        let page = |label: &str, path: &str| {
            String::from_utf8_lossy(get(&f, label, path).body()).into_owned()
        };
        assert!(!page("editor", "deck-options/1").contains("night-mode"));
        assert!(!page("reviewer", "_anki/legacyPageData?id=reviewer").contains("night-mode"));
        f.shell.night.set(true);
        for (label, path) in [
            ("editor", "deck-options/1"),
            ("editor", "deck-browser"),
            ("reviewer", "_anki/legacyPageData?id=reviewer"),
            ("editor", "_anki/legacyPageData?id=editor"),
        ] {
            let body = page(label, path);
            assert!(
                body.contains("<html class=\"night-mode\" data-bs-theme=\"dark\">"),
                "{path}: {body}"
            );
        }
        // other files are left alone
        assert_eq!(
            get(&f, "reviewer", "_anki/css/reviewer.css").body(),
            b"body {}"
        );
        // add-on panels too
        let addons = f._dir.path().join("addons21");
        std::fs::create_dir_all(addons.join("frontier/web")).unwrap();
        std::fs::write(addons.join("frontier/web/form.html"), "<html><body>").unwrap();
        std::fs::write(addons.join("frontier/web/form.js"), "<html>").unwrap();
        f.shell.addon_files.set_folder(Some(addons));
        f.shell.addon_files.set_exports("frontier", r"web/.*");
        assert_eq!(
            get(&f, "addon-panel-1", "_addons/frontier/web/form.html").body(),
            b"<html class=\"night-mode\" data-bs-theme=\"dark\"><body class=\"nightMode night_mode\">"
        );
        assert_eq!(
            get(&f, "addon-panel-1", "_addons/frontier/web/form.js").body(),
            b"<html>"
        );
    }

    #[test]
    fn paths_cannot_escape_their_folder() {
        let f = fixture();
        // media is <tmp>/User 1/collection.media and web is <tmp>/web; secret.txt is in
        // <tmp>
        for path in [
            "../../secret.txt",
            "%2e%2e/%2e%2e/secret.txt",
            "_anki/../secret.txt",
            "_anki/%2e%2e/secret.txt",
        ] {
            let resp = get(&f, "reviewer", path);
            assert_ne!(resp.status(), StatusCode::OK, "{path}");
        }
    }

    #[test]
    fn editor_page_gets_a_csp_nonce() {
        let f = fixture();
        let editor = get(&f, "editor", "_anki/legacyPageData?id=editor");
        let csp = editor.headers()[CONTENT_SECURITY_POLICY].to_str().unwrap();
        let nonce = csp.split('\'').nth(1).unwrap().trim_start_matches("nonce-");
        let html = String::from_utf8(editor.body().clone()).unwrap();
        assert!(html.contains(&format!("content=\"{nonce}\"")), "{html}");
        let reviewer = get(&f, "reviewer", "_anki/legacyPageData?id=reviewer");
        assert!(!reviewer.headers().contains_key(CONTENT_SECURITY_POLICY));
    }

    #[test]
    fn editor_fields_cant_frame_local_documents() {
        // a shared deck's field with <iframe src="x.html"> would get the
        // editor's bridge; only the shell's own pages and the web may be
        // framed, and plugins/objects not at all
        let f = fixture();
        let editor = get(&f, "editor", "_anki/legacyPageData?id=editor");
        let csp = editor.headers()[CONTENT_SECURITY_POLICY].to_str().unwrap();
        assert!(
            csp.contains(&format!(
                "frame-src {ORIGIN}_anki/legacyPageData https: http:"
            )),
            "{csp}"
        );
        assert!(csp.contains("object-src 'none'"), "{csp}");
    }

    #[test]
    fn webviews_get_only_their_own_legacy_pages() {
        let f = fixture();
        let own = get(&f, "reviewer", "_anki/legacyPageData?id=reviewer");
        assert_eq!(own.status(), StatusCode::OK);
        assert_eq!(
            get(&f, "editor", "_anki/legacyPageData?id=reviewer").status(),
            StatusCode::NOT_FOUND
        );
        // `editor-side` (an add-on's panel) is the editor's
        f.shell
            .pages
            .set("editor-side", WebviewKind::Dialog, Some("side".into()));
        assert_eq!(
            get(&f, "editor", "_anki/legacyPageData?id=editor-side").body(),
            b"side"
        );
        assert_eq!(
            get(&f, "editorx", "_anki/legacyPageData?id=editor-side").status(),
            StatusCode::NOT_FOUND
        );
    }

    #[test]
    fn media_documents_are_sandboxed() {
        // their own origin, so a framed or opened media page can't reach
        // the bridge or the API
        let f = fixture();
        for path in ["page.html", "pic.svg"] {
            let resp = get(&f, "reviewer", path);
            assert_eq!(resp.status(), StatusCode::OK);
            assert_eq!(resp.headers()[CONTENT_SECURITY_POLICY], "sandbox", "{path}");
        }
        assert!(!get(&f, "reviewer", "pic.png")
            .headers()
            .contains_key(CONTENT_SECURITY_POLICY));
    }

    fn bridge() -> tauri::http::request::Builder {
        Request::post(format!("anki://localhost/{BRIDGE_PATH}")).header(BRIDGE_HEADER, "1")
    }

    #[test]
    fn bridge_commands_reach_their_handler() {
        let f = fixture();
        f.shell
            .bridge
            .register("key", |_, label, args| serde_json::json!([label, args]));
        let req = bridge().body(b"key:0:12:a:b".to_vec()).unwrap();
        let resp = handle(&f.shell, "editor", &req);
        assert_eq!(resp.body(), br#"["editor","0:12:a:b"]"#);
        let req = bridge().body(b"mystery".to_vec()).unwrap();
        assert_eq!(handle(&f.shell, "editor", &req).body(), b"null");
        assert_eq!(f.shell.bridge.unknown_commands()["mystery"], 1);
    }

    #[test]
    fn bridge_rejects_opaque_and_cross_origin_requests() {
        let f = fixture();
        f.shell
            .bridge
            .register("key", |_, _, _| serde_json::json!(true));
        // a no-cors fetch can send text/plain, but not a custom header
        let req = Request::post(format!("anki://localhost/{BRIDGE_PATH}"))
            .header(CONTENT_TYPE, "text/plain")
            .body(b"key:0:1:x".to_vec())
            .unwrap();
        assert_eq!(
            handle(&f.shell, "editor", &req).status(),
            StatusCode::FORBIDDEN
        );
        let req = bridge()
            .header("Origin", "https://example.com")
            .body(b"key:0:1:x".to_vec())
            .unwrap();
        assert_eq!(
            handle(&f.shell, "editor", &req).status(),
            StatusCode::FORBIDDEN
        );
        let req = bridge()
            .header("Origin", "anki://localhost")
            .body(b"key:0:1:x".to_vec())
            .unwrap();
        assert_eq!(handle(&f.shell, "editor", &req).body(), b"true");
    }

    #[test]
    fn navigation_is_limited_to_anki_pages_and_web_frames() {
        assert!(allowed_navigation(
            &"anki://localhost/_anki/legacyPageData?id=main"
                .parse()
                .unwrap()
        ));
        // frames in cards and fields
        assert!(allowed_navigation(
            &"https://www.youtube.com/embed/x".parse().unwrap()
        ));
        assert!(allowed_navigation(&"about:srcdoc".parse().unwrap()));
        assert!(!allowed_navigation(&"anki://elsewhere/".parse().unwrap()));
        assert!(!allowed_navigation(&"file:///etc/hosts".parse().unwrap()));
    }
}
