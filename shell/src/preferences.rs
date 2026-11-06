// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The Preferences window (`aqt/preferences.py`): the `preferences` route.
//! The collection's preferences go through the backend
//! (`get_preferences`/`set_preferences`); the shell's own (theme, LaTeX
//! commands) through the bridge commands here.

use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::time::Duration;

use anyhow::anyhow;
use anyhow::Result;
use serde::Deserialize;
use serde_json::json;
use serde_json::Value;
use tauri::Manager;

use crate::app::Content;
use crate::app::Ui;
use crate::latex::LatexCommands;
use crate::pages::WebviewKind;
use crate::theme::ThemePref;

pub const LABEL: &str = "preferences";

/// Set while the window flushes its page before closing.
static CLOSING: AtomicBool = AtomicBool::new(false);
/// Counts the windows opened, so a late close timeout leaves a new one.
static OPENED: AtomicU64 = AtomicU64::new(0);
/// How long closing waits for the page.
const FLUSH_TIMEOUT: Duration = Duration::from_secs(2);

pub fn open(ui: &Ui) {
    // already open: keep its page (and what's being typed)
    if let Some(window) = ui.app.get_webview_window(LABEL) {
        if !ui.background {
            let _ = window.set_focus();
        }
        return;
    }
    CLOSING.store(false, Ordering::SeqCst);
    OPENED.fetch_add(1, Ordering::SeqCst);
    if let Err(err) = ui.open_window(
        LABEL,
        &ui.main.tr.preferences_preferences(),
        WebviewKind::Preferences,
        Content::Route("preferences".into()),
    ) {
        tracing::error!(%err, "opening preferences failed");
    }
}

/// Whether a close request should first save what's being typed (not when
/// the page already did, or doesn't answer).
pub fn wants_flush(_ui: &Ui) -> bool {
    !CLOSING.load(Ordering::SeqCst)
}

/// Save the page's pending changes, then close: changes are saved shortly
/// after typing stops, and a closing webview runs no more timers.
pub fn close(ui: &Arc<Ui>) {
    CLOSING.store(true, Ordering::SeqCst);
    ui.eval(
        LABEL,
        "(async () => { try { await window.shellFlush?.(); } finally { \
         pycmd('preferencesClosed'); } })();"
            .into(),
    );
    let u = ui.clone();
    let opened = OPENED.load(Ordering::SeqCst);
    std::thread::spawn(move || {
        std::thread::sleep(FLUSH_TIMEOUT);
        if OPENED.load(Ordering::SeqCst) == opened {
            destroy(&u);
        }
    });
}

fn destroy(ui: &Ui) {
    if let Some(window) = ui.app.get_webview_window(LABEL) {
        let _ = window.destroy();
    }
}

pub fn register(ui: &Arc<Ui>) {
    let bridge = &ui.shell.bridge;
    let u = ui.clone();
    bridge.register("preferencesClosed", move |_, label, _| {
        if label == LABEL {
            destroy(&u);
        }
        Value::Null
    });
    // the collection's preferences changed: the queues and undo with them
    let u = ui.clone();
    bridge.register("preferencesChanged", move |_, label, _| {
        if label == LABEL {
            crate::menus::refresh(&u);
            crate::main_window::refresh(&u);
        }
        Value::Null
    });
    let u = ui.clone();
    bridge.register("shellPreferences", move |_, label, _| {
        if label != LABEL {
            return Value::Null;
        }
        shell_preferences(&u)
    });
    let u = ui.clone();
    bridge.register("setShellPreferences", move |_, label, args| {
        if label != LABEL {
            return Value::Null;
        }
        match set_shell_preferences(&u, args) {
            Ok(()) => json!({ "ok": true }),
            Err(err) => json!({ "error": format!("{err:#}") }),
        }
    });
}

fn shell_preferences(ui: &Ui) -> Value {
    let settings = ui.settings.lock().unwrap();
    let toolchain = settings.latex_toolchain();
    let defaults = LatexCommands::default();
    json!({
        "theme": settings.theme_pref(),
        "latex": {
            "png": format_commands(&toolchain.for_kind(false)),
            "svg": format_commands(&toolchain.for_kind(true)),
        },
        "latexDefaults": {
            "png": format_commands(&defaults.for_kind(false)),
            "svg": format_commands(&defaults.for_kind(true)),
        },
    })
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct Update {
    theme: Option<ThemePref>,
    latex: Option<LatexText>,
    /// Back to the old Anki's commands, or latex.py's.
    #[serde(default)]
    reset_latex: bool,
}

#[derive(Deserialize)]
struct LatexText {
    png: String,
    svg: String,
}

fn set_shell_preferences(ui: &Arc<Ui>, args: &str) -> Result<()> {
    let update: Update = serde_json::from_str(args)?;
    let latex = update
        .latex
        .map(|text| -> Result<LatexCommands> {
            Ok(LatexCommands {
                png: custom_commands(&text.png, false)?,
                svg: custom_commands(&text.svg, true)?,
            })
        })
        .transpose()?;
    {
        let mut settings = ui.settings.lock().unwrap();
        if let Some(theme) = update.theme {
            settings.theme = Some(theme);
        }
        if let Some(latex) = latex {
            settings.latex_commands = Some(latex);
        }
        if update.reset_latex {
            settings.latex_commands = None;
        }
        crate::latex::set_toolchain(settings.latex_toolchain());
    }
    ui.save_settings_now();
    crate::theme::apply(ui, None);
    Ok(())
}

/// The commands typed for one kind of image; None if they're latex.py's.
fn custom_commands(text: &str, svg: bool) -> Result<Option<Vec<Vec<String>>>> {
    let commands = parse_commands(text)?;
    if commands.is_empty() {
        return Err(anyhow!("there must be at least one command"));
    }
    Ok((commands != LatexCommands::default().for_kind(svg)).then_some(commands))
}

/// One command per line, split as a shell would (quotes, backslashes).
pub fn parse_commands(text: &str) -> Result<Vec<Vec<String>>> {
    text.lines()
        .enumerate()
        .filter(|(_, line)| !line.trim().is_empty())
        .map(|(i, line)| {
            shlex::split(line).ok_or_else(|| anyhow!("line {}: unmatched quote", i + 1))
        })
        .collect()
}

pub fn format_commands(commands: &[Vec<String>]) -> String {
    commands
        .iter()
        .map(|args| args.iter().map(|a| quote(a)).collect::<Vec<_>>().join(" "))
        .collect::<Vec<_>>()
        .join("\n")
}

/// An argument as typed: quoted only if it needs to be.
fn quote(arg: &str) -> String {
    let plain = |c: char| c.is_ascii_alphanumeric() || "_@%+=:,./-~^".contains(c);
    if !arg.is_empty() && arg.chars().all(plain) {
        arg.to_string()
    } else {
        format!("'{}'", arg.replace('\'', "'\"'\"'"))
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn commands_round_trip_through_text() {
        let commands = vec![
            vec![
                "pdflatex".to_string(),
                "--interaction=nonstopmode".into(),
                "tmp.tex".into(),
            ],
            vec![
                "pdfcrop".into(),
                "--margins".into(),
                "2 .5 2 .5".into(),
                "tmp.pdf".into(),
            ],
        ];
        let text = format_commands(&commands);
        assert_eq!(
            text,
            "pdflatex --interaction=nonstopmode tmp.tex\npdfcrop --margins '2 .5 2 .5' tmp.pdf"
        );
        assert_eq!(parse_commands(&text).unwrap(), commands);
        let odd = vec![vec!["it's".to_string(), String::new(), "a\"b".into()]];
        assert_eq!(parse_commands(&format_commands(&odd)).unwrap(), odd);
    }

    #[test]
    fn blank_lines_are_skipped_and_bad_quotes_named() {
        assert_eq!(
            parse_commands("\n  latex tmp.tex \n\n").unwrap(),
            vec![vec!["latex", "tmp.tex"]]
        );
        let err = parse_commands("latex tmp.tex\ndvipng \"tmp.dvi").unwrap_err();
        assert_eq!(err.to_string(), "line 2: unmatched quote");
    }

    #[test]
    fn the_default_commands_are_not_stored_as_custom_ones() {
        let defaults = format_commands(&LatexCommands::default().for_kind(false));
        assert_eq!(custom_commands(&defaults, false).unwrap(), None);
        // the PNG defaults aren't the SVG ones
        assert!(custom_commands(&defaults, true).unwrap().is_some());
        assert!(custom_commands("  \n", false).is_err());
    }
}
