// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The main window's zoom (View > Zoom In/Out/Reset Zoom), as Qt's
//! `mainWindowZoomFactor`: steps of 0.1 between 0.25 and 5.

use std::sync::Arc;

use tauri::Manager;

use crate::app::Ui;
use crate::app::MAIN;

pub const DEFAULT: f64 = 1.0;
const MIN: f64 = 0.25;
const MAX: f64 = 5.0;
const STEP: f64 = 0.1;

/// The menu items' ids.
pub const ZOOM_IN: &str = "zoomIn";
pub const ZOOM_OUT: &str = "zoomOut";
pub const RESET_ZOOM: &str = "resetZoom";

/// A saved factor as one to use: the default if it's unusable.
pub fn clamped(factor: f64) -> f64 {
    if factor.is_finite() {
        factor.clamp(MIN, MAX)
    } else {
        DEFAULT
    }
}

/// The factor after the menu item `id`, if it's a zoom item.
pub fn after(id: &str, current: f64) -> Option<f64> {
    let next = match id {
        ZOOM_IN => current + STEP,
        ZOOM_OUT => current - STEP,
        RESET_ZOOM => DEFAULT,
        _ => return None,
    };
    // in hundredths, so steps don't drift (1.1 + 0.1 isn't 1.2)
    Some(clamped((next * 100.0).round() / 100.0))
}

/// Apply the saved zoom to the main window (when it opens).
pub fn apply(ui: &Ui) {
    if let Some(window) = ui.app.get_webview_window(MAIN) {
        let factor = ui.settings.lock().unwrap().main_window_zoom();
        if let Err(err) = window.set_zoom(factor) {
            tracing::warn!(%err, "setting the zoom failed");
        }
    }
}

/// A zoom menu item: zooms the main window unless another window has the
/// focus (the editors have no zoom yet). Returns whether `id` was one.
pub fn on_menu(ui: &Arc<Ui>, id: &str) -> bool {
    if ![ZOOM_IN, ZOOM_OUT, RESET_ZOOM].contains(&id) {
        return false;
    }
    let other_focused = ui
        .app
        .webview_windows()
        .iter()
        .any(|(label, w)| label != MAIN && w.is_focused().unwrap_or(false));
    let Some(window) = ui.app.get_webview_window(MAIN).filter(|_| !other_focused) else {
        return true;
    };
    // one lock for the step, the window and the file: a held Cmd+= sends
    // events on several threads at once
    let mut settings = ui.settings.lock().unwrap();
    let Some(factor) = after(id, settings.main_window_zoom()) else {
        return true;
    };
    settings.main_window_zoom = Some(factor);
    if let Err(err) = window.set_zoom(factor) {
        tracing::warn!(%err, "setting the zoom failed");
    }
    if let Err(err) = settings.save(&ui.settings_path) {
        tracing::error!(%err, "saving settings failed");
    }
    true
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn steps_by_a_tenth_without_drift() {
        let mut f = DEFAULT;
        for _ in 0..3 {
            f = after(ZOOM_IN, f).unwrap();
        }
        assert_eq!(f, 1.3);
        for _ in 0..5 {
            f = after(ZOOM_OUT, f).unwrap();
        }
        assert_eq!(f, 0.8);
        assert_eq!(after(RESET_ZOOM, f), Some(DEFAULT));
    }

    #[test]
    fn stops_at_the_limits() {
        assert_eq!(after(ZOOM_OUT, 0.3), Some(MIN));
        assert_eq!(after(ZOOM_OUT, MIN), Some(MIN));
        assert_eq!(after(ZOOM_IN, 4.95), Some(MAX));
        assert_eq!(after(ZOOM_IN, MAX), Some(MAX));
    }

    #[test]
    fn other_items_are_not_zoom() {
        assert_eq!(after("undo", 1.0), None);
    }

    #[test]
    fn unusable_saved_factors_are_the_default() {
        assert_eq!(clamped(1.5), 1.5);
        assert_eq!(clamped(9.0), MAX);
        assert_eq!(clamped(0.0), MIN);
        assert_eq!(clamped(f64::NAN), DEFAULT);
        assert_eq!(clamped(f64::INFINITY), DEFAULT);
    }
}
