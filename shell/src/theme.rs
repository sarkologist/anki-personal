// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Dark mode, as `aqt.theme.ThemeManager`: the theme preference (follow
//! the system, light or dark) decides night mode. Pages get Qt's markers
//! for it: `night-mode` and `data-bs-theme` on `<html>`, `nightMode` and
//! `night_mode` on `<body>` (card templates use those). The protocol adds
//! them to every HTML page it serves, so a page never loads in the wrong
//! theme; pages already open get [toggle_js].

use std::sync::atomic::AtomicBool;
use std::sync::atomic::Ordering;

use serde::Deserialize;
use serde::Serialize;

/// `aqt.theme.Theme`; the old Anki's `_global.theme` is 0, 1 or 2.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ThemePref {
    #[default]
    System,
    Light,
    Dark,
}

impl ThemePref {
    pub fn from_anki(value: i64) -> Option<ThemePref> {
        match value {
            0 => Some(ThemePref::System),
            1 => Some(ThemePref::Light),
            2 => Some(ThemePref::Dark),
            _ => None,
        }
    }

    pub fn night_mode(self, system_dark: bool) -> bool {
        match self {
            ThemePref::System => system_dark,
            ThemePref::Light => false,
            ThemePref::Dark => true,
        }
    }

    /// The appearance windows are given: None follows the system.
    pub fn window_theme(self) -> Option<tauri::Theme> {
        match self {
            ThemePref::System => None,
            ThemePref::Light => Some(tauri::Theme::Light),
            ThemePref::Dark => Some(tauri::Theme::Dark),
        }
    }
}

/// Whether pages are drawn dark (read by the protocol on every page).
#[derive(Default)]
pub struct Night(AtomicBool);

impl Night {
    pub fn get(&self) -> bool {
        self.0.load(Ordering::SeqCst)
    }

    /// Returns whether it changed.
    pub fn set(&self, night: bool) -> bool {
        self.0.swap(night, Ordering::SeqCst) != night
    }
}

/// Whether macOS is in dark mode (the global `AppleInterfaceStyle`, which
/// the windows' own appearance would hide once a theme is forced).
pub fn system_dark() -> bool {
    use objc2_foundation::ns_string;
    use objc2_foundation::NSUserDefaults;
    let defaults = NSUserDefaults::standardUserDefaults();
    defaults
        .stringForKey(ns_string!("AppleInterfaceStyle"))
        .is_some_and(|style| style.to_string().eq_ignore_ascii_case("dark"))
}

/// Apply the theme preference: the windows' appearance, night mode for
/// pages served from now on, and the pages already open. `system_dark` is
/// the system's appearance if known (from a theme change event).
pub fn apply(ui: &std::sync::Arc<crate::app::Ui>, system_dark: Option<bool>) {
    // one at a time: a system change and a preference change at once could
    // otherwise leave the older outcome
    static APPLYING: std::sync::Mutex<()> = std::sync::Mutex::new(());
    let _one = APPLYING.lock().unwrap();
    let pref = ui.settings.lock().unwrap().theme_pref();
    ui.app.set_theme(pref.window_theme());
    let night = pref.night_mode(system_dark.unwrap_or_else(self::system_dark));
    ui.main.reviewer.lock().unwrap().night_mode = night;
    if !ui.shell.night.set(night) {
        return;
    }
    tracing::info!(night, "theme changed");
    let labels: Vec<String> = {
        use tauri::Manager;
        ui.app.webview_windows().keys().cloned().collect()
    };
    for label in labels {
        ui.eval(&label, toggle_js(night));
    }
}

/// Add the night-mode markers to a page's `<html>` and `<body>` tags.
pub fn apply_to_html(html: &str, night: bool) -> String {
    if !night {
        return html.to_string();
    }
    let html = add_to_tag(html, "html", &["night-mode"], Some("dark"));
    add_to_tag(&html, "body", &["nightMode", "night_mode"], None)
}

/// The first `<tag ...>`, with `classes` added to its class attribute and
/// `data-bs-theme` set if given. Unchanged if there's no such tag.
fn add_to_tag(html: &str, tag: &str, classes: &[&str], bs_theme: Option<&str>) -> String {
    let lower = html.to_ascii_lowercase();
    let open = format!("<{tag}");
    let Some(start) = lower.match_indices(&open).map(|(i, _)| i).find(|&i| {
        lower[i + open.len()..]
            .chars()
            .next()
            .is_some_and(|c| c == '>' || c.is_ascii_whitespace())
    }) else {
        return html.to_string();
    };
    let Some(len) = html[start..].find('>') else {
        return html.to_string();
    };
    let end = start + len;
    let mut attrs = html[start + open.len()..end].to_string();
    let joined = classes.join(" ");
    match find_attr(&attrs, "class") {
        Some((value_start, value_end)) => {
            let existing = &attrs[value_start..value_end];
            let value = if existing.trim().is_empty() {
                joined
            } else {
                format!("{existing} {joined}")
            };
            attrs.replace_range(value_start..value_end, &value);
        }
        None => attrs.push_str(&format!(" class=\"{joined}\"")),
    }
    if let Some(theme) = bs_theme {
        match find_attr(&attrs, "data-bs-theme") {
            Some((value_start, value_end)) => attrs.replace_range(value_start..value_end, theme),
            None => attrs.push_str(&format!(" data-bs-theme=\"{theme}\"")),
        }
    }
    format!("{}{open}{attrs}{}", &html[..start], &html[end..])
}

/// The byte range of a double-quoted attribute's value.
fn find_attr(attrs: &str, name: &str) -> Option<(usize, usize)> {
    let lower = attrs.to_ascii_lowercase();
    let needle = format!("{name}=\"");
    let mut from = 0;
    while let Some(i) = lower[from..].find(&needle) {
        let i = from + i;
        // a whole attribute name, not the end of another one
        if i == 0 || lower.as_bytes()[i - 1].is_ascii_whitespace() {
            let value_start = i + needle.len();
            let value_end = value_start + attrs[value_start..].find('"')?;
            return Some((value_start, value_end));
        }
        from = i + needle.len();
    }
    None
}

/// Qt's `on_theme_did_change` for a page that's already open, and for its
/// same-origin frames (the reviewer's bars).
pub fn toggle_js(night: bool) -> String {
    format!(
        r#"(() => {{
    const night = {night};
    const apply = (w) => {{
        try {{
            const doc = w.document.documentElement;
            doc.dataset.bsTheme = night ? "dark" : "light";
            doc.classList.toggle("night-mode", night);
            const body = w.document.body?.classList;
            if (body) {{
                body.toggle("nightMode", night);
                body.toggle("night_mode", night);
            }}
            for (let i = 0; i < w.frames.length; i++) apply(w.frames[i]);
        }} catch (e) {{}}
    }};
    apply(window);
}})();"#
    )
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn light_pages_are_unchanged() {
        let html = "<html class=\"mac\"><body class=\"isMac\"></body></html>";
        assert_eq!(apply_to_html(html, false), html);
    }

    #[test]
    fn night_mode_marks_the_html_and_body_tags() {
        let html = "<!doctype html><html class=\"mac\" dir=\"ltr\" data-bs-theme=\"light\">\
                    <head></head><body class=\"isMac\"><p>x</p></body></html>";
        assert_eq!(
            apply_to_html(html, true),
            "<!doctype html><html class=\"mac night-mode\" dir=\"ltr\" data-bs-theme=\"dark\">\
             <head></head><body class=\"isMac nightMode night_mode\"><p>x</p></body></html>"
        );
    }

    #[test]
    fn tags_without_classes_get_them() {
        // SvelteKit's page shell
        let html = "<!doctype html>\n<html lang=\"en\">\n<head></head>\n<body>\n<div></div></body>";
        assert_eq!(
            apply_to_html(html, true),
            "<!doctype html>\n<html lang=\"en\" class=\"night-mode\" data-bs-theme=\"dark\">\n\
             <head></head>\n<body class=\"nightMode night_mode\">\n<div></div></body>"
        );
    }

    #[test]
    fn only_the_tags_themselves_change() {
        // <header>/<bodyx> aren't <html>/<body>, and a data-class attribute
        // isn't the class attribute
        let html = "<header></header><html data-class=\"x\"><bodyx></bodyx><body></body>";
        assert_eq!(
            apply_to_html(html, true),
            "<header></header><html data-class=\"x\" class=\"night-mode\" data-bs-theme=\"dark\">\
             <bodyx></bodyx><body class=\"nightMode night_mode\"></body>"
        );
    }

    #[test]
    fn the_preference_decides_night_mode() {
        assert!(ThemePref::System.night_mode(true));
        assert!(!ThemePref::System.night_mode(false));
        assert!(!ThemePref::Light.night_mode(true));
        assert!(ThemePref::Dark.night_mode(false));
        assert_eq!(ThemePref::from_anki(0), Some(ThemePref::System));
        assert_eq!(ThemePref::from_anki(2), Some(ThemePref::Dark));
        assert_eq!(ThemePref::from_anki(7), None);
    }

    #[test]
    fn night_reports_changes() {
        let night = Night::default();
        assert!(!night.set(false));
        assert!(night.set(true));
        assert!(night.get());
    }
}
