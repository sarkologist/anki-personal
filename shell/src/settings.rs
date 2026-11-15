// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The shell's own settings (a JSON file), plus importing the profile list
//! and sync credentials from the old Anki's `prefs21.db`.
//!
//! The old Anki stays the fallback and can change its copy (a re-login, a
//! sync redirect), so the import can be re-run at any time; it replaces the
//! imported fields and keeps the shell's own ones.

use std::collections::BTreeMap;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;

use anyhow::bail;
use anyhow::Context;
use anyhow::Result;
use serde::Deserialize;
use serde::Serialize;

use crate::latex::LatexCommands;
use crate::theme::ThemePref;

const IMPORT_SCRIPT: &str = include_str!("../scripts/import_prefs.py");

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// The old Anki's base folder (holding `prefs21.db` and the profile
    /// folders). None means the default location.
    pub anki_base: Option<PathBuf>,
    /// `--anki-base` (a profile copy), for this run only: never saved, so
    /// a run on a copy doesn't point later runs at it.
    #[serde(skip)]
    anki_base_for_run: Option<PathBuf>,
    /// Python with `aqt` installed, for the import and the plugin host.
    /// None means search the usual install locations.
    pub import_python: Option<PathBuf>,
    pub last_profile: Option<String>,
    pub profiles: Vec<Profile>,
    /// Unix seconds of the last import.
    pub imported_at: Option<i64>,
    /// Answer keys 1-4 from the old Anki and its hjkl add-on (imported).
    pub answer_keys: Vec<Vec<String>>,
    /// The shell's own shortcut overrides: action -> combos (see
    /// `crate::shortcuts::DEFAULTS`).
    pub shortcuts: BTreeMap<String, Vec<String>>,
    /// Space/Enter rate the card on the answer side (imported; default on).
    pub spacebar_rates_card: Option<bool>,
    /// Add-ons to run in the plugin host: folder names in the old Anki's
    /// `addons21` (they share its config and files). None by default.
    pub plugins: Vec<String>,
    /// What the editor remembers between notes (the profile and meta keys
    /// `lastTextColor`, `lastHighlightColor`, `*TagsCollapsed` in Qt).
    pub editor: EditorPrefs,
    /// The theme chosen in the shell's preferences; None uses `anki_theme`.
    pub theme: Option<ThemePref>,
    /// The old Anki's theme (imported).
    pub anki_theme: Option<ThemePref>,
    /// The LaTeX commands set in the shell's preferences; None uses
    /// `anki_latex_commands`, else latex.py's own.
    pub latex_commands: Option<LatexCommands>,
    /// The `pngCommands`/`svgCommands` an enabled add-on sets in the old
    /// Anki (the "Edit LaTeX toolchain" add-on does), imported.
    pub anki_latex_commands: Option<LatexCommands>,
    /// Whether an import has read the LaTeX commands (None is a value).
    pub latex_imported: bool,
    /// The card browser's layout and search history.
    pub browser: BrowserPrefs,
    /// The main window's zoom (View menu); None is 1.
    pub main_window_zoom: Option<f64>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct BrowserPrefs {
    /// The table's width beside the editor, in pixels (0: the default).
    pub split: f64,
    /// The sidebar's width (0: the default).
    pub sidebar: f64,
    /// Column widths by key, in each mode.
    pub card_widths: std::collections::HashMap<String, f64>,
    pub note_widths: std::collections::HashMap<String, f64>,
    /// Searches typed, newest first (Anki keeps them in the profile).
    pub history: Vec<String>,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct EditorPrefs {
    pub last_text_color: Option<String>,
    pub last_highlight_color: Option<String>,
    pub add_tags_collapsed: bool,
    pub edit_tags_collapsed: bool,
    /// The side panel's width, in pixels (0: the default).
    pub side_width: f64,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Profile {
    pub name: String,
    pub sync: SyncSettings,
}

/// The `prefs21.db` profile keys sync needs, under their pickle names.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct SyncSettings {
    pub sync_key: Option<String>,
    pub sync_user: Option<String>,
    pub host_num: Option<i64>,
    pub current_sync_url: Option<String>,
    pub custom_sync_url: Option<String>,
    pub sync_media: Option<bool>,
    pub auto_sync: Option<bool>,
}

/// What `import_prefs.py` prints.
#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct Imported {
    profiles: Vec<ImportedProfile>,
    last_loaded: Option<String>,
    #[serde(default)]
    answer_keys: BTreeMap<String, String>,
    spacebar_rates_card: Option<bool>,
    /// `aqt.theme.Theme` as an int
    theme: Option<i64>,
    latex_commands: Option<LatexCommands>,
}

#[derive(Debug, Deserialize)]
struct ImportedProfile {
    name: String,
    #[serde(flatten)]
    sync: SyncSettings,
}

pub fn default_anki_base() -> PathBuf {
    dirs::data_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("Anki2")
}

impl Settings {
    pub fn load(path: &Path) -> Result<Settings> {
        match std::fs::read(path) {
            Ok(data) => {
                serde_json::from_slice(&data).with_context(|| format!("reading {}", path.display()))
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(Settings::default()),
            Err(e) => Err(e).with_context(|| format!("reading {}", path.display())),
        }
    }

    /// Written to a temp file and renamed, so a crash can't truncate it.
    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let tmp = path.with_extension("json.tmp");
        std::fs::write(&tmp, serde_json::to_vec_pretty(self)?)?;
        std::fs::rename(&tmp, path)?;
        Ok(())
    }

    pub fn theme_pref(&self) -> ThemePref {
        self.theme.or(self.anki_theme).unwrap_or_default()
    }

    pub fn main_window_zoom(&self) -> f64 {
        self.main_window_zoom
            .map_or(crate::zoom::DEFAULT, crate::zoom::clamped)
    }

    /// The LaTeX commands to run, where they aren't latex.py's.
    pub fn latex_toolchain(&self) -> LatexCommands {
        self.latex_commands
            .clone()
            .or_else(|| self.anki_latex_commands.clone())
            .unwrap_or_default()
    }

    pub fn use_anki_base_for_this_run(&mut self, base: PathBuf) {
        self.anki_base_for_run = Some(base);
    }

    pub fn anki_base(&self) -> PathBuf {
        self.anki_base_for_run
            .clone()
            .or_else(|| self.anki_base.clone())
            .unwrap_or_else(default_anki_base)
    }

    pub fn profile(&self, name: &str) -> Option<&Profile> {
        self.profiles
            .iter()
            .find(|p| p.name.eq_ignore_ascii_case(name))
    }

    /// The profile to open: the last one used here, else the one the old
    /// Anki last loaded (as imported), else the first.
    pub fn startup_profile(&self) -> Option<&str> {
        self.last_profile
            .as_deref()
            .and_then(|n| self.profile(n))
            .or_else(|| self.profiles.first())
            .map(|p| p.name.as_str())
    }

    /// Settings imported before the theme was: take just the old Anki's
    /// theme (a full re-import would replace sync logins made here).
    /// Also the LaTeX commands, if they were imported before the import
    /// read them.
    pub fn import_theme_if_missing(&mut self) -> Result<bool> {
        if !self.backfill_needed() {
            return Ok(false);
        }
        let out = self.run_import()?;
        self.merge_theme(&out)?;
        Ok(true)
    }

    /// Imported, but before the import read the theme or LaTeX commands.
    fn backfill_needed(&self) -> bool {
        self.imported_at.is_some() && (self.anki_theme.is_none() || !self.latex_imported)
    }

    fn merge_theme(&mut self, json: &[u8]) -> Result<()> {
        let imported: Imported =
            serde_json::from_slice(json).context("unexpected profile import output")?;
        if self.anki_theme.is_none() {
            // "follow the system" if it has none, so this runs once
            self.anki_theme = Some(
                imported
                    .theme
                    .and_then(ThemePref::from_anki)
                    .unwrap_or_default(),
            );
        }
        if !self.latex_imported {
            self.anki_latex_commands = imported.latex_commands.filter(|c| !c.is_empty());
            self.latex_imported = true;
        }
        Ok(())
    }

    /// Run the import and merge it in.
    pub fn import_from_anki(&mut self) -> Result<()> {
        let out = self.run_import()?;
        self.merge_import(&out)
    }

    /// `import_prefs.py`'s output.
    fn run_import(&self) -> Result<Vec<u8>> {
        let python = match &self.import_python {
            Some(python) => python.clone(),
            None => find_python().context(
                "no Anki Python found for the profile import; set import_python in settings",
            )?,
        };
        let out = Command::new(&python)
            .arg("-I")
            .arg("-c")
            .arg(IMPORT_SCRIPT)
            .arg(self.anki_base())
            .output()
            .with_context(|| format!("running {}", python.display()))?;
        if !out.status.success() {
            bail!(
                "profile import failed: {}",
                String::from_utf8_lossy(&out.stderr).trim()
            );
        }
        Ok(out.stdout)
    }

    fn merge_import(&mut self, json: &[u8]) -> Result<()> {
        let imported: Imported =
            serde_json::from_slice(json).context("unexpected profile import output")?;
        self.profiles = imported
            .profiles
            .into_iter()
            .map(|p| Profile {
                name: p.name,
                sync: p.sync,
            })
            .collect();
        if self
            .last_profile
            .as_deref()
            .and_then(|n| self.profile(n))
            .is_none()
        {
            self.last_profile = imported.last_loaded;
        }
        self.answer_keys =
            crate::shortcuts::import_answer_keys(&self.anki_base(), &imported.answer_keys);
        self.spacebar_rates_card = imported.spacebar_rates_card;
        // "follow the system" if it has none, so no backfill runs later
        self.anki_theme = Some(
            imported
                .theme
                .and_then(ThemePref::from_anki)
                .unwrap_or_default(),
        );
        self.anki_latex_commands = imported.latex_commands.filter(|c| !c.is_empty());
        self.latex_imported = true;
        self.imported_at = Some(chrono::Utc::now().timestamp());
        Ok(())
    }
}

/// A Python that can import `aqt`: `ANKI_SHELL_PYTHON`, the launcher's
/// venv, or a venv in `~/.anki-stable`.
pub fn find_python() -> Option<PathBuf> {
    if let Some(p) = std::env::var_os("ANKI_SHELL_PYTHON") {
        return Some(p.into());
    }
    let home = dirs::home_dir()?;
    let data = dirs::data_dir()?;
    [
        data.join("AnkiProgramFiles/.venv/bin/python"),
        home.join(".anki-stable/bin/python"),
    ]
    .into_iter()
    .find(|p| p.exists())
}

#[cfg(test)]
mod test {
    use super::*;

    const IMPORTED: &str = r#"{"profiles": [{"name": "User 1", "syncKey": "k1",
        "syncUser": "me@example.com", "hostNum": 10,
        "currentSyncUrl": "https://sync10.ankiweb.net/", "customSyncUrl": "",
        "syncMedia": true, "autoSync": false}, {"name": "Other"}],
        "lastLoaded": "User 1", "answerKeys": {"1": "1", "2": "2", "3": "3", "4": "x"},
        "spacebarRatesCard": false, "theme": 2,
        "latexCommands": {"png": [["pdflatex", "tmp.tex"], ["sips", "tmp.pdf"]]}}"#;

    #[test]
    fn import_replaces_profiles_and_keeps_own_fields() {
        let mut settings = Settings {
            anki_base: Some("/base".into()),
            profiles: vec![Profile {
                name: "Gone".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        settings.merge_import(IMPORTED.as_bytes()).unwrap();
        assert_eq!(settings.anki_base, Some("/base".into()));
        assert_eq!(settings.profiles.len(), 2);
        let sync = &settings.profile("user 1").unwrap().sync;
        assert_eq!(sync.sync_key.as_deref(), Some("k1"));
        assert_eq!(sync.host_num, Some(10));
        assert_eq!(
            sync.current_sync_url.as_deref(),
            Some("https://sync10.ankiweb.net/")
        );
        assert_eq!(sync.auto_sync, Some(false));
        assert_eq!(
            settings.profile("Other").unwrap().sync,
            SyncSettings::default()
        );
        assert_eq!(settings.startup_profile(), Some("User 1"));
        assert!(settings.imported_at.is_some());
        assert_eq!(settings.answer_keys[3], vec!["x"]);
        assert_eq!(settings.spacebar_rates_card, Some(false));
        assert_eq!(settings.theme_pref(), ThemePref::Dark);
        let latex = settings.latex_toolchain();
        assert_eq!(latex.png.unwrap()[1], vec!["sips", "tmp.pdf"]);
        assert_eq!(latex.svg, None);
    }

    #[test]
    fn an_earlier_import_gets_just_the_theme() {
        let mut settings = Settings {
            imported_at: Some(1),
            profiles: vec![Profile {
                name: "Kept".into(),
                ..Default::default()
            }],
            ..Default::default()
        };
        settings.merge_theme(IMPORTED.as_bytes()).unwrap();
        assert_eq!(settings.anki_theme, Some(ThemePref::Dark));
        assert_eq!(
            settings.latex_toolchain().png.unwrap()[1],
            vec!["sips", "tmp.pdf"]
        );
        assert_eq!(settings.profiles[0].name, "Kept");
        // none in the old Anki: the system's, so it's asked once only
        settings.anki_theme = None;
        settings.latex_imported = false;
        settings
            .merge_theme(br#"{"profiles": [], "lastLoaded": null}"#)
            .unwrap();
        assert_eq!(settings.anki_theme, Some(ThemePref::System));
        assert_eq!(settings.anki_latex_commands, None);
        assert!(!settings.import_theme_if_missing().unwrap());
    }

    #[test]
    fn the_shells_own_theme_and_latex_commands_win_over_imported_ones() {
        let mut settings = Settings {
            theme: Some(ThemePref::Light),
            latex_commands: Some(LatexCommands {
                png: None,
                svg: Some(vec![vec!["mine".into()]]),
            }),
            ..Default::default()
        };
        settings.merge_import(IMPORTED.as_bytes()).unwrap();
        assert_eq!(settings.anki_theme, Some(ThemePref::Dark));
        assert_eq!(settings.theme_pref(), ThemePref::Light);
        assert_eq!(
            settings.latex_toolchain(),
            settings.latex_commands.clone().unwrap()
        );
        // an import without them resets the imported ones only (no theme
        // there is "follow the system")
        settings
            .merge_import(br#"{"profiles": [], "lastLoaded": null}"#)
            .unwrap();
        assert_eq!(settings.anki_theme, Some(ThemePref::System));
        assert_eq!(settings.anki_latex_commands, None);
        assert_eq!(settings.theme, Some(ThemePref::Light));
        assert_eq!(Settings::default().theme_pref(), ThemePref::System);
    }

    #[test]
    fn a_full_import_needs_no_backfill() {
        // also when the old Anki has no theme and no LaTeX commands
        for json in [
            IMPORTED,
            r#"{"profiles": [{"name": "User 1"}], "lastLoaded": null}"#,
        ] {
            let mut settings = Settings::default();
            settings.merge_import(json.as_bytes()).unwrap();
            assert!(!settings.backfill_needed(), "{json}");
        }
    }

    #[test]
    fn the_main_window_zoom_is_1_unless_saved_and_usable() {
        let saved = |zoom| Settings {
            main_window_zoom: zoom,
            ..Default::default()
        };
        assert_eq!(saved(None).main_window_zoom(), 1.0);
        assert_eq!(saved(Some(1.2)).main_window_zoom(), 1.2);
        assert_eq!(saved(Some(9.0)).main_window_zoom(), 5.0);
    }

    #[test]
    fn reimport_keeps_the_shells_last_profile() {
        let mut settings = Settings {
            last_profile: Some("Other".into()),
            ..Default::default()
        };
        settings.merge_import(IMPORTED.as_bytes()).unwrap();
        assert_eq!(settings.startup_profile(), Some("Other"));
    }

    #[test]
    fn a_run_on_a_copy_keeps_the_saved_base() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("settings.json");
        Settings::default().save(&path).unwrap();
        let mut settings = Settings::load(&path).unwrap();
        settings.use_anki_base_for_this_run("/copy".into());
        assert_eq!(settings.anki_base(), PathBuf::from("/copy"));
        settings.save(&path).unwrap();
        assert_eq!(
            Settings::load(&path).unwrap().anki_base(),
            default_anki_base()
        );
    }

    #[test]
    fn settings_round_trip_through_json() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("sub/settings.json");
        assert_eq!(Settings::load(&path).unwrap(), Settings::default());
        let mut settings = Settings::default();
        settings.merge_import(IMPORTED.as_bytes()).unwrap();
        settings.save(&path).unwrap();
        assert_eq!(Settings::load(&path).unwrap(), settings);
    }
}
