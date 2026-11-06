// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! What `AnkiWebView` does for each webview: its kind (which decides API
//! access, as `webview.py`'s `_profileForPage`) and the HTML the shell
//! assembled for it (`stdHtml` + `mediasrv`'s legacy page store).

use std::collections::HashMap;
use std::path::Path;
use std::sync::Mutex;

pub const ORIGIN: &str = "anki://localhost/";
/// The URL parameter carrying a page's capability (`Page::cap`).
pub const CAP_PARAM: &str = "cap";
/// The header its API calls send it in (added by the bridge script).
pub const CAP_HEADER: &str = "X-Anki-Page-Cap";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum WebviewKind {
    Main,
    Reviewer,
    Previewer,
    Editor,
    DeckOptions,
    DeckStats,
    ChangeNotetype,
    CardInfo,
    ImportCsv,
    ImportAnkiPackage,
    ImportLog,
    CardLayout,
    /// A [crate::main_window::ask] window.
    Dialog,
    /// An add-on's web view (`AnkiWebView`) in an editor's side panel,
    /// which may show a model's output: served sandboxed, whatever frames
    /// it.
    AddonView,
}

impl WebviewKind {
    /// Pages with the whole page API, as in `webview.py`. Other pages may
    /// show user content (card templates), so they get only the reviewer
    /// endpoints.
    pub fn has_api_access(self) -> bool {
        matches!(
            self,
            WebviewKind::DeckOptions
                | WebviewKind::Editor
                | WebviewKind::DeckStats
                | WebviewKind::ChangeNotetype
                | WebviewKind::CardInfo
                | WebviewKind::ImportCsv
                | WebviewKind::ImportAnkiPackage
                | WebviewKind::ImportLog
        )
    }
}

#[derive(Debug, Clone)]
pub struct Page {
    pub kind: WebviewKind,
    /// HTML served at `/_anki/legacyPageData`, if the shell assembled one.
    pub html: Option<String>,
    /// For the main window's own pages: the capability their document got
    /// in its URL, which its API calls must carry (see `protocol.rs`).
    pub cap: Option<String>,
}

/// Webview label -> page.
#[derive(Default)]
pub struct Pages(Mutex<HashMap<String, Page>>);

impl Pages {
    pub fn set(&self, label: &str, kind: WebviewKind, html: Option<String>) {
        self.set_with_cap(label, kind, html, None);
    }

    pub fn set_with_cap(
        &self,
        label: &str,
        kind: WebviewKind,
        html: Option<String>,
        cap: Option<String>,
    ) {
        self.0
            .lock()
            .unwrap()
            .insert(label.to_string(), Page { kind, html, cap });
    }

    pub fn get(&self, label: &str) -> Option<Page> {
        self.0.lock().unwrap().get(label).cloned()
    }

    pub fn remove(&self, label: &str) {
        self.0.lock().unwrap().remove(label);
    }
}

/// The URL a webview loads to show the HTML stored for it.
pub fn legacy_page_url(label: &str) -> String {
    format!("{ORIGIN}_anki/legacyPageData?id={label}")
}

/// The page font. QtWebEngine defaults to sans-serif, WebKit to Times, and
/// most pages (all the SvelteKit ones) don't set their own.
pub const SYSTEM_FONT_CSS: &str = "html { font-family: -apple-system, \"Helvetica\", sans-serif; }";

/// `AnkiWebView.stdHtml`, minus Qt and add-on hooks. As there, `js`
/// replaces the default jQuery, and scripts go at the start of the body.
pub fn std_html(web: &Path, body: &str, css: &[&str], js: &[&str], default_css: bool) -> String {
    let mut head = String::new();
    if default_css {
        let webview_css = std::fs::read_to_string(web.join("css/webview.css")).unwrap_or_default();
        head += &format!("<style>{webview_css}</style>");
        head += "<style>body { zoom: 1; background-color: var(--canvas); }</style>";
        head += &format!("<style>{SYSTEM_FONT_CSS}</style>");
    }
    for c in css {
        head += &format!("<link rel=\"stylesheet\" type=\"text/css\" href=\"/_anki/{c}\">");
    }
    let mut scripts = String::new();
    for j in std::iter::once(&"js/webview.js").chain(js) {
        scripts += &format!("<script src=\"/_anki/{j}\"></script>");
    }
    format!(
        "<!doctype html><html class=\"mac\" dir=\"ltr\" data-bs-theme=\"light\">\
         <head><base href=\"{ORIGIN}\"><meta charset=\"utf-8\"><title></title>{head}</head>\
         <body class=\"isMac\">{scripts}{body}</body></html>"
    )
}

pub fn reviewer_html(web: &Path) -> String {
    std_html(
        web,
        "<div id=\"_mark\" hidden>&#x2605;</div><div id=\"_flag\" hidden>&#x2691;</div>\
         <div id=\"qa\" dir=\"auto\"></div>",
        &["css/reviewer.css"],
        &["js/reviewer.js"],
        true,
    )
}

pub fn editor_html(web: &Path) -> String {
    std_html(web, "", &["css/editor.css"], &["js/editor.js"], false)
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn pages_resolve_media_against_the_server_root() {
        // card images are relative (`<img src="pic.png">`), and the page
        // itself is served from /_anki/legacyPageData
        let html = std_html(Path::new("/nonexistent"), "", &[], &[], false);
        assert!(
            html.contains(&format!("<head><base href=\"{ORIGIN}\">")),
            "{html}"
        );
    }

    #[test]
    fn only_trusted_pages_get_the_full_api() {
        assert!(WebviewKind::Editor.has_api_access());
        assert!(WebviewKind::DeckOptions.has_api_access());
        assert!(!WebviewKind::Reviewer.has_api_access());
        assert!(!WebviewKind::Previewer.has_api_access());
        assert!(!WebviewKind::CardLayout.has_api_access());
        assert!(!WebviewKind::Main.has_api_access());
    }
}
