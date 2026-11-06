// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Single-key shortcuts for the main window (the reviewer's `_shortcutKeys`
//! and the main window's state keys), configurable in the settings. They
//! replace the "hjkl answer keys" add-on, so h/j/k/l answer by default, and
//! the add-on's and Anki's own answer keys are imported from the old Anki.
//!
//! Shortcuts with Cmd are native menu items instead (`crate::menus`), so
//! they work wherever the focus is.
//!
//! Pages report key presses as combos like `Shift+a`, `Space`, `*`: the
//! modifiers Cmd, Ctrl, Alt, Shift in that order, then the key. Letters are
//! lower case and only take an explicit Shift; other characters already
//! include it (`*`, `!`). See [key_script].

use std::collections::BTreeMap;
use std::collections::HashMap;
use std::path::Path;

use serde_json::Value;

/// The "right hand reviews / hjkl answer shortcuts" add-on.
const HJKL_ADDON: &str = "850294128";

/// action -> default combos. Answer keys come first, so imported ones can
/// replace them by position.
pub const DEFAULTS: &[(&str, &[&str])] = &[
    ("answer1", &["1", "h"]),
    ("answer2", &["2", "j"]),
    ("answer3", &["3", "k"]),
    ("answer4", &["4", "l"]),
    // reviewer
    ("showAnswer", &["Space", "Enter"]),
    ("edit", &["e"]),
    ("more", &["m"]),
    ("replay", &["r", "F5"]),
    ("mark", &["*"]),
    ("buryNote", &["="]),
    ("buryCard", &["-"]),
    ("suspendNote", &["!"]),
    ("suspendCard", &["@"]),
    ("options", &["o"]),
    ("cardInfo", &["i"]),
    ("undo", &["u"]),
    ("pauseAudio", &["5"]),
    ("seekBackward", &["6"]),
    ("seekForward", &["7"]),
    ("autoAdvance", &["Shift+a"]),
    // main window
    ("decks", &["d"]),
    ("add", &["a"]),
    ("browse", &["b"]),
    ("study", &["s"]),
    ("chooseDeck", &["/"]),
    ("stats", &["t"]),
    ("sync", &["y"]),
];

/// Canonical form of a combo as written in the settings: `ctrl+shift+A` is
/// `Ctrl+Shift+a`, `E` is `e` (as in Qt), `space` is `Space`.
pub fn normalize(combo: &str) -> String {
    // "+" alone, or a trailing "++" (Shift++), is the plus key
    let (mods, key) = match combo.strip_suffix("++") {
        Some(rest) => (rest, "+"),
        None if combo == "+" => ("", "+"),
        None => combo.rsplit_once('+').unwrap_or(("", combo)),
    };
    let mut flags = [false; 4];
    for m in mods.split('+').filter(|m| !m.is_empty()) {
        match m.to_ascii_lowercase().as_str() {
            "cmd" | "meta" | "command" => flags[0] = true,
            "ctrl" | "control" => flags[1] = true,
            "alt" | "option" | "opt" => flags[2] = true,
            "shift" => flags[3] = true,
            _ => {}
        }
    }
    let key = match key {
        k if k.chars().count() == 1 => {
            let c = k.chars().next().unwrap();
            if c.is_ascii_alphabetic() {
                c.to_ascii_lowercase().to_string()
            } else {
                // the character already says whether Shift was down
                flags[3] = false;
                if c == ' ' {
                    "Space".into()
                } else {
                    k.into()
                }
            }
        }
        k => match k.to_ascii_lowercase().as_str() {
            "space" => "Space".into(),
            "enter" | "return" => "Enter".into(),
            "esc" | "escape" => "Escape".into(),
            "backspace" => "Backspace".into(),
            "delete" | "del" => "Delete".into(),
            "tab" => "Tab".into(),
            f if f.starts_with('f') && f[1..].parse::<u8>().is_ok() => f.to_ascii_uppercase(),
            _ => k.into(),
        },
    };
    let names = ["Cmd", "Ctrl", "Alt", "Shift"];
    let mut out = String::new();
    for (on, name) in flags.iter().zip(names) {
        if *on {
            out += name;
            out.push('+');
        }
    }
    out + &key
}

/// The key bindings in effect: defaults, then imported answer keys, then
/// the settings' own overrides.
#[derive(Debug, Clone, Default)]
pub struct Shortcuts {
    by_combo: HashMap<String, String>,
    by_action: BTreeMap<String, Vec<String>>,
}

impl Shortcuts {
    /// `imported`: answer keys 1-4 from the old Anki (None = keep the
    /// defaults); `overrides`: action -> combos from the settings.
    pub fn new(imported: &[Vec<String>], overrides: &BTreeMap<String, Vec<String>>) -> Self {
        let mut by_action: BTreeMap<String, Vec<String>> = DEFAULTS
            .iter()
            .map(|(action, combos)| {
                (
                    action.to_string(),
                    combos.iter().map(|c| normalize(c)).collect(),
                )
            })
            .collect();
        for (i, keys) in imported.iter().enumerate().take(4) {
            if !keys.is_empty() {
                by_action.insert(
                    format!("answer{}", i + 1),
                    keys.iter().map(|c| normalize(c)).collect(),
                );
            }
        }
        for (action, combos) in overrides {
            by_action.insert(
                action.clone(),
                combos.iter().map(|c| normalize(c)).collect(),
            );
        }
        let mut by_combo = HashMap::new();
        // a combo bound twice goes to the override, then to an imported
        // answer key, then to the action listed first in DEFAULTS
        let order: Vec<&str> = DEFAULTS.iter().map(|(a, _)| *a).collect();
        let rank = |a: &String| {
            let tier = if overrides.contains_key(a) {
                0
            } else if a.starts_with("answer") && !imported.is_empty() {
                1
            } else {
                2
            };
            (
                tier,
                order.iter().position(|o| o == a).unwrap_or(usize::MAX),
            )
        };
        let mut actions: Vec<&String> = by_action.keys().collect();
        actions.sort_by_key(|a| rank(a));
        for action in actions {
            for combo in &by_action[action] {
                by_combo
                    .entry(combo.clone())
                    .or_insert_with(|| action.clone());
            }
        }
        Shortcuts {
            by_combo,
            by_action,
        }
    }

    pub fn action(&self, combo: &str) -> Option<&str> {
        self.by_combo.get(combo).map(String::as_str)
    }

    pub fn combos(&self, action: &str) -> &[String] {
        self.by_action.get(action).map_or(&[], Vec::as_slice)
    }

    /// The combos bound to any of `actions`, for the page script.
    pub fn bound(&self, actions: &[&str]) -> Vec<String> {
        let mut out: Vec<String> = self
            .by_combo
            .iter()
            .filter(|(_, a)| actions.contains(&a.as_str()))
            .map(|(c, _)| c.clone())
            .collect();
        out.sort();
        out
    }
}

/// The answer keys the old Anki uses, if they differ from 1-4: the hjkl
/// add-on's config (when enabled), with Anki's own answer keys
/// (`answer_keys` in the profile manager's meta, from the import) first.
pub fn import_answer_keys(
    anki_base: &Path,
    anki_keys: &BTreeMap<String, String>,
) -> Vec<Vec<String>> {
    let addon = hjkl_addon_keys(anki_base);
    let mut out = vec![];
    for ease in 1..=4 {
        let mut keys = vec![];
        let own = anki_keys
            .get(&ease.to_string())
            .cloned()
            .unwrap_or_else(|| ease.to_string());
        if !own.is_empty() {
            keys.push(own);
        }
        if let Some(key) = addon
            .as_ref()
            .and_then(|a| a.get(ease - 1))
            .filter(|k| !k.is_empty())
        {
            keys.push(key.clone());
        }
        out.push(keys);
    }
    // nothing imported beyond the stock 1-4: keep the h/j/k/l defaults
    let stock = (1..=4).all(|e| out[e - 1] == vec![e.to_string()]);
    if stock {
        vec![]
    } else {
        out
    }
}

/// `reviewer_1`..`reviewer_4` from the add-on's settings (`meta.json`'s
/// `config` holds the user's edits, `config.json` the defaults).
fn hjkl_addon_keys(anki_base: &Path) -> Option<Vec<String>> {
    let dir = anki_base.join("addons21").join(HJKL_ADDON);
    let read = |name: &str| -> Option<Value> {
        serde_json::from_slice(&std::fs::read(dir.join(name)).ok()?).ok()
    };
    let meta = read("meta.json").unwrap_or(Value::Null);
    if meta["disabled"] == Value::Bool(true) {
        return None;
    }
    let config = match &meta["config"] {
        Value::Object(_) => meta["config"].clone(),
        _ => read("config.json")?,
    };
    let keys: Vec<String> = (1..=4)
        .map(|i| {
            config[format!("reviewer_{i}")]
                .as_str()
                .unwrap_or_default()
                .to_string()
        })
        .collect();
    keys.iter().any(|k| !k.is_empty()).then_some(keys)
}

/// Installed in every `anki:` frame of the main window: key presses bound
/// for the current page (`window.top.__shellKeys`, set by the shell) go to
/// the shell as `shortcut:<token>:<combo>`, unless the focus is in a text
/// field.
///
/// Card templates run JS in the reviewer, and Qt never let it undo, bury or
/// suspend, so only real key presses count: synthetic events are ignored
/// (unless `allow_synthetic`, for test scripts), and the message carries
/// `token`, which lives in this closure and goes out through the `fetch`
/// captured before any page script runs.
pub fn key_script(token: &str, allow_synthetic: bool) -> String {
    KEY_SCRIPT
        .replace("__TOKEN__", &serde_json::to_string(token).unwrap())
        .replace(
            "__SYNTHETIC__",
            if allow_synthetic { "true" } else { "false" },
        )
        .replace("__BRIDGE__", crate::protocol::BRIDGE_PATH)
        .replace("__HEADER__", crate::protocol::BRIDGE_HEADER)
}

const KEY_SCRIPT: &str = r#"(() => {
    if (location.protocol !== "anki:") {
        return;
    }
    const token = __TOKEN__;
    const allowSynthetic = __SYNTHETIC__;
    const post = window.fetch.bind(window);
    const send = (combo) => post("/__BRIDGE__", {
        method: "POST",
        headers: { "Content-Type": "text/plain", "__HEADER__": "1" },
        body: "shortcut:" + token + ":" + combo,
    }).catch((e) => console.error("shortcut", e));
    const combo = (e) => {
        const mods = [];
        if (e.metaKey) mods.push("Cmd");
        if (e.ctrlKey) mods.push("Ctrl");
        if (e.altKey) mods.push("Alt");
        let key = e.key;
        // Alt changes the character on macOS, so use the key's position
        if ((e.metaKey || e.ctrlKey || e.altKey) && /^(Key.|Digit.)$/.test(e.code)) {
            key = e.code.slice(-1);
        }
        if (["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead"].includes(key)) {
            return null;
        }
        if (key === " ") {
            key = "Space";
        }
        if (key.length === 1) {
            if (/[a-z]/i.test(key)) {
                key = key.toLowerCase();
                if (e.shiftKey) mods.push("Shift");
            }
        } else if (e.shiftKey) {
            mods.push("Shift");
        }
        return [...mods, key].join("+");
    };
    const editable = (el) => el && (el.isContentEditable
        || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
    window.addEventListener("keydown", (e) => {
        let keys;
        try {
            keys = window.top.__shellKeys;
        } catch {
            return;
        }
        if (!keys || (!e.isTrusted && !allowSynthetic) || e.isComposing || e.repeat
            || editable(document.activeElement)) {
            return;
        }
        const c = combo(e);
        if (c && keys.includes(c)) {
            e.preventDefault();
            e.stopImmediatePropagation();
            send(c);
        }
    }, true);
})();"#;

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn combos_normalize_like_the_page_script_reports_them() {
        assert_eq!(normalize("ctrl+shift+A"), "Ctrl+Shift+a");
        assert_eq!(normalize("E"), "e");
        assert_eq!(normalize("Shift+v"), "Shift+v");
        assert_eq!(normalize("space"), "Space");
        assert_eq!(normalize(" "), "Space");
        assert_eq!(normalize("Return"), "Enter");
        assert_eq!(normalize("f5"), "F5");
        assert_eq!(normalize("Shift+*"), "*");
        assert_eq!(normalize("Alt+Cmd+i"), "Cmd+Alt+i");
        assert_eq!(normalize("+"), "+");
        assert_eq!(normalize("Cmd++"), "Cmd++");
        assert_eq!(normalize(";"), ";");
    }

    #[test]
    fn defaults_answer_with_digits_and_hjkl() {
        let s = Shortcuts::new(&[], &BTreeMap::new());
        for (combo, action) in [
            ("1", "answer1"),
            ("h", "answer1"),
            ("2", "answer2"),
            ("j", "answer2"),
            ("3", "answer3"),
            ("k", "answer3"),
            ("4", "answer4"),
            ("l", "answer4"),
            ("Space", "showAnswer"),
            ("Shift+a", "autoAdvance"),
        ] {
            assert_eq!(s.action(combo), Some(action), "{combo}");
        }
    }

    #[test]
    fn slash_opens_the_deck_picker() {
        // stock Anki's Tools > Study Deck... shortcut
        let s = Shortcuts::new(&[], &BTreeMap::new());
        assert_eq!(s.action("/"), Some("chooseDeck"));
    }

    #[test]
    fn default_combos_are_unique() {
        let mut seen = std::collections::HashSet::new();
        for (action, combos) in DEFAULTS {
            for c in *combos {
                assert!(seen.insert(normalize(c)), "{action}: {c} bound twice");
            }
        }
    }

    #[test]
    fn imported_keys_replace_the_defaults_and_overrides_win() {
        // the user's add-on config: ; l k j
        let imported = vec![
            vec!["1".to_string(), ";".into()],
            vec!["2".into(), "l".into()],
            vec!["3".into(), "k".into()],
            vec!["4".into(), "j".into()],
        ];
        let s = Shortcuts::new(&imported, &BTreeMap::new());
        assert_eq!(s.action(";"), Some("answer1"));
        assert_eq!(s.action("l"), Some("answer2"));
        assert_eq!(s.action("j"), Some("answer4"));
        assert_eq!(s.action("h"), None);
        let overrides = BTreeMap::from([("decks".to_string(), vec!["Shift+d".to_string()])]);
        let s = Shortcuts::new(&imported, &overrides);
        assert_eq!(s.action("Shift+d"), Some("decks"));
        assert_eq!(s.action("d"), None);
        // an override that takes another action's combo wins it
        let overrides = BTreeMap::from([("decks".to_string(), vec!["1".to_string()])]);
        let s = Shortcuts::new(&[], &overrides);
        assert_eq!(s.action("1"), Some("decks"));
        // and an imported answer key wins over a default
        let imported = vec![vec!["d".to_string()]];
        let s = Shortcuts::new(&imported, &BTreeMap::new());
        assert_eq!(s.action("d"), Some("answer1"));
    }

    #[test]
    fn the_key_script_takes_only_real_key_presses_with_the_token() {
        let js = key_script("se\"cret", false);
        assert!(js.contains(r#"const token = "se\"cret";"#));
        assert!(js.contains("const allowSynthetic = false;"));
        assert!(js.contains("!e.isTrusted && !allowSynthetic"));
        assert!(js.contains(r#"body: "shortcut:" + token + ":" + combo"#));
        for placeholder in ["__TOKEN__", "__SYNTHETIC__", "__BRIDGE__", "__HEADER__"] {
            assert!(!js.contains(placeholder), "{placeholder} left");
        }
        assert!(key_script("t", true).contains("const allowSynthetic = true;"));
    }

    #[test]
    fn bound_lists_combos_for_the_page() {
        let s = Shortcuts::new(&[], &BTreeMap::new());
        assert_eq!(s.bound(&["decks", "stats"]), vec!["d", "t"]);
    }

    fn base_with_addon(meta: &str, config: Option<&str>) -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        let addon = dir.path().join("addons21").join(HJKL_ADDON);
        std::fs::create_dir_all(&addon).unwrap();
        std::fs::write(addon.join("meta.json"), meta).unwrap();
        if let Some(config) = config {
            std::fs::write(addon.join("config.json"), config).unwrap();
        }
        dir
    }

    #[test]
    fn import_reads_the_hjkl_addon() {
        let base = base_with_addon(
            r#"{"config": {"reviewer_1": ";", "reviewer_2": "l", "reviewer_3": "k", "reviewer_4": "j"}}"#,
            Some(r#"{"reviewer_1": "h"}"#),
        );
        let keys = import_answer_keys(base.path(), &BTreeMap::new());
        assert_eq!(
            keys,
            vec![
                vec!["1", ";"],
                vec!["2", "l"],
                vec!["3", "k"],
                vec!["4", "j"]
            ]
        );
        // its defaults when the user hasn't changed them
        let base = base_with_addon("{}", Some(r#"{"reviewer_1": "h", "reviewer_2": "j"}"#));
        let keys = import_answer_keys(base.path(), &BTreeMap::new());
        assert_eq!(keys[0], vec!["1", "h"]);
        assert_eq!(keys[2], vec!["3"]);
    }

    #[test]
    fn import_ignores_a_disabled_addon_and_keeps_stock_defaults() {
        let base = base_with_addon(r#"{"disabled": true, "config": {"reviewer_1": ";"}}"#, None);
        assert!(import_answer_keys(base.path(), &BTreeMap::new()).is_empty());
        let empty = tempfile::tempdir().unwrap();
        assert!(import_answer_keys(empty.path(), &BTreeMap::new()).is_empty());
        // Anki's own answer keys changed
        let own = BTreeMap::from([("1".to_string(), "a".to_string())]);
        assert_eq!(import_answer_keys(empty.path(), &own)[0], vec!["a"]);
    }
}
