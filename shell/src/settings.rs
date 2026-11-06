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

const IMPORT_SCRIPT: &str = include_str!("../scripts/import_prefs.py");

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    /// The old Anki's base folder (holding `prefs21.db` and the profile
    /// folders). None means the default location.
    pub anki_base: Option<PathBuf>,
    /// Python with `aqt` installed, for the import. None means search the
    /// usual install locations.
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
    /// What the editor remembers between notes (the profile and meta keys
    /// `lastTextColor`, `lastHighlightColor`, `*TagsCollapsed` in Qt).
    pub editor: EditorPrefs,
}

#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default, rename_all = "camelCase")]
pub struct EditorPrefs {
    pub last_text_color: Option<String>,
    pub last_highlight_color: Option<String>,
    pub add_tags_collapsed: bool,
    pub edit_tags_collapsed: bool,
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

    pub fn anki_base(&self) -> PathBuf {
        self.anki_base.clone().unwrap_or_else(default_anki_base)
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

    /// Run the import and merge it in.
    pub fn import_from_anki(&mut self) -> Result<()> {
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
        self.merge_import(&out.stdout)
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
        self.imported_at = Some(chrono::Utc::now().timestamp());
        Ok(())
    }
}

/// A Python that can import `aqt`: `ANKI_SHELL_PYTHON`, the launcher's
/// venv, or a venv in `~/.anki-stable`.
fn find_python() -> Option<PathBuf> {
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
        "spacebarRatesCard": false}"#;

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
