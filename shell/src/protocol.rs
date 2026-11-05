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

/// SvelteKit routes, served through the SvelteKit fallback page.
const SVELTEKIT_PAGES: &[&str] = &[
    "graphs",
    "congrats",
    "card-info",
    "change-notetype",
    "deck-options",
    "import-anki-package",
    "import-csv",
    "import-page",
    "image-occlusion",
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
                return legacy_page(shell, req);
            }
        }
        return bundled_file(&shell.web, &remap_legacy_path(rest));
    }
    if path.starts_with("_addons/") {
        return text(StatusCode::NOT_FOUND, "add-on files are not supported yet");
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
    if ctype != "application/binary" {
        tracing::warn!(
            endpoint,
            label,
            "rejected API call without binary content type"
        );
        return text(StatusCode::FORBIDDEN, "unexpected content type");
    }
    let kind = shell.pages.get(label).map(|p| p.kind);
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

    let method = camel_to_snake(endpoint);
    let result = if SHELL_POST_HANDLERS.contains(&method.as_str()) {
        shell.frontend(label, &method, req.body())
    } else if EXPOSED_BACKEND.contains(&method.as_str())
        || FORWARDED_POST_HANDLERS.contains(&method.as_str())
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

fn legacy_page(shell: &Shell, req: &Request<Vec<u8>>) -> HttpResponse {
    let id = req
        .uri()
        .query()
        .unwrap_or_default()
        .split('&')
        .find_map(|kv| kv.strip_prefix("id="))
        .map(percent_decode)
        .unwrap_or_default();
    let Some(page) = shell.pages.get(&id) else {
        return text(StatusCode::NOT_FOUND, "page not found");
    };
    let Some(html) = page.html else {
        return text(StatusCode::NOT_FOUND, "page not found");
    };
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
        format!("script-src 'nonce-{nonce}' {ORIGIN}_anki/ {ORIGIN}_addons/")
            .parse()
            .unwrap(),
    );
    resp
}

fn bundled_file(web: &Path, path: &str) -> HttpResponse {
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
        Ok(data) => {
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
        std::fs::write(web.join("sveltekit/index.html"), "<svelte>").unwrap();
        std::fs::write(web.join("sveltekit/_app/immutable/a.js"), "a").unwrap();
        let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), web);
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        shell.open_profile(paths.clone()).unwrap();
        std::fs::write(paths.media().join("pic.png"), "png").unwrap();
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
        assert_eq!(get(&f, "editor", "deck-options/1").body(), b"<svelte>");
        let asset = get(&f, "editor", "_app/immutable/a.js");
        assert_eq!(asset.body(), b"a");
        assert!(asset.headers().contains_key("Cache-Control"));
        let media = get(&f, "reviewer", "pic.png");
        assert_eq!(media.body(), b"png");
        assert_eq!(media.headers()[CONTENT_TYPE], "image/png");
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
        let editor = get(&f, "x", "_anki/legacyPageData?id=editor");
        let csp = editor.headers()[CONTENT_SECURITY_POLICY].to_str().unwrap();
        let nonce = csp.split('\'').nth(1).unwrap().trim_start_matches("nonce-");
        let html = String::from_utf8(editor.body().clone()).unwrap();
        assert!(html.contains(&format!("content=\"{nonce}\"")), "{html}");
        let reviewer = get(&f, "x", "_anki/legacyPageData?id=reviewer");
        assert!(!reviewer.headers().contains_key(CONTENT_SECURITY_POLICY));
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
