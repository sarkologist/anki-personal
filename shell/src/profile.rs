// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Opening and closing a profile's collection, and backups, the way
//! `aqt.main` and `aqt.profiles` do it. The shell uses the same profile
//! folders as the old Anki, so either can be used on a given day.

use std::path::Path;
use std::path::PathBuf;

use anki_proto::collection::CloseCollectionRequest;
use anki_proto::collection::CreateBackupRequest;
use anki_proto::collection::OpenCollectionRequest;
use anki_proto::generic::Empty;
use serde_json::json;
use serde_json::Value;

use crate::api::Api;
use crate::api::ApiError;

/// How often the periodic backup is checked (`aqt.main`: 5 minutes). The
/// backend decides whether enough time has passed since the last one.
pub const BACKUP_CHECK_INTERVAL: std::time::Duration = std::time::Duration::from_secs(5 * 60);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProfilePaths {
    pub name: String,
    pub folder: PathBuf,
}

impl ProfilePaths {
    pub fn new(anki_base: &Path, name: &str) -> Self {
        ProfilePaths {
            name: name.to_string(),
            folder: anki_base.join(name),
        }
    }

    pub fn collection(&self) -> PathBuf {
        self.folder.join("collection.anki2")
    }

    pub fn media(&self) -> PathBuf {
        self.folder.join("collection.media")
    }

    pub fn media_db(&self) -> PathBuf {
        self.folder.join("collection.media.db2")
    }

    pub fn backups(&self) -> PathBuf {
        self.folder.join("backups")
    }
}

#[derive(Debug)]
pub enum OpenError {
    /// The old Anki (or a media sync) has the collection open: rslib opens
    /// SQLite in exclusive locking mode.
    Locked(String),
    Other(String),
}

impl std::fmt::Display for OpenError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            OpenError::Locked(msg) => write!(
                f,
                "{msg} Close the profile in the other Anki first, then try again."
            ),
            OpenError::Other(msg) => f.write_str(msg),
        }
    }
}

impl std::error::Error for OpenError {}

impl From<ApiError> for OpenError {
    fn from(err: ApiError) -> Self {
        if err.is_db_locked() {
            OpenError::Locked(err.message)
        } else {
            OpenError::Other(err.message)
        }
    }
}

pub fn open_collection(api: &Api, paths: &ProfilePaths) -> Result<(), OpenError> {
    std::fs::create_dir_all(paths.media())
        .map_err(|e| OpenError::Other(format!("creating media folder: {e}")))?;
    let _: Empty = api.call(
        "open_collection",
        &OpenCollectionRequest {
            collection_path: paths.collection().to_string_lossy().into(),
            media_folder_path: paths.media().to_string_lossy().into(),
            media_db_path: paths.media_db().to_string_lossy().into(),
        },
    )?;
    Ok(())
}

/// A backup if the configured interval has passed and the collection
/// changed (`aqt.main.on_periodic_backup_timer`). Returns whether one was
/// made.
pub fn periodic_backup(api: &Api, paths: &ProfilePaths) -> Result<bool, ApiError> {
    create_backup(api, paths, false)
}

fn create_backup(api: &Api, paths: &ProfilePaths, force: bool) -> Result<bool, ApiError> {
    std::fs::create_dir_all(paths.backups()).map_err(|e| ApiError {
        kind: -1,
        message: format!("creating backup folder: {e}"),
    })?;
    let created: anki_proto::generic::Bool = api.call(
        "create_backup",
        &CreateBackupRequest {
            backup_folder: paths.backups().to_string_lossy().into(),
            force,
            wait_for_completion: false,
        },
    )?;
    Ok(created.val)
}

#[derive(Debug, Default, PartialEq, Eq)]
pub struct CloseReport {
    pub corrupt: bool,
    pub backed_up: bool,
}

/// `aqt.main._unloadCollection`: a quick check, a (throttled) backup unless
/// the check failed, then close and wait for the backup to finish.
pub fn close_collection(api: &Api, paths: &ProfilePaths) -> Result<CloseReport, ApiError> {
    let mut report = CloseReport::default();
    let check = api.db_command(&json!({
        "kind": "query",
        "sql": "pragma quick_check",
        "args": [],
        "first_row_only": true,
    }));
    report.corrupt = !matches!(&check, Ok(Value::Array(rows))
        if rows.first().and_then(|r| r.get(0)).and_then(Value::as_str) == Some("ok"));
    if !report.corrupt {
        match create_backup(api, paths, false) {
            Ok(created) => report.backed_up = created,
            Err(err) => tracing::warn!(%err, "backup on close failed"),
        }
    } else {
        tracing::error!(?check, "quick check failed; skipping backup");
    }
    let _: Empty = api.call(
        "close_collection",
        &CloseCollectionRequest {
            downgrade_to_schema11: false,
        },
    )?;
    let _: Empty = api.call("await_backup_completion", &Empty {})?;
    Ok(report)
}

#[cfg(test)]
mod test {
    use super::*;

    fn temp_profile() -> (tempfile::TempDir, ProfilePaths) {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        (dir, paths)
    }

    #[test]
    fn second_open_reports_the_lock() {
        let (_dir, paths) = temp_profile();
        let first = Api::new(vec!["en".into()]).unwrap();
        open_collection(&first, &paths).unwrap();

        let second = Api::new(vec!["en".into()]).unwrap();
        let err = open_collection(&second, &paths).unwrap_err();
        assert!(matches!(err, OpenError::Locked(_)), "{err:?}");
        assert!(err.to_string().contains("other Anki"), "{err}");

        close_collection(&first, &paths).unwrap();
        open_collection(&second, &paths).unwrap();
    }

    #[test]
    fn close_runs_a_quick_check_and_backs_up() {
        let (_dir, paths) = temp_profile();
        let api = Api::new(vec!["en".into()]).unwrap();
        open_collection(&api, &paths).unwrap();
        // a change, so there's something to back up
        api.db_command(&json!({"kind": "begin"})).unwrap();
        api.db_command(&json!({
            "kind": "query",
            "sql": "update col set mod = mod + 1",
            "args": [],
            "first_row_only": false,
        }))
        .unwrap();
        api.db_command(&json!({"kind": "commit"})).unwrap();

        let report = close_collection(&api, &paths).unwrap();
        assert_eq!(
            report,
            CloseReport {
                corrupt: false,
                backed_up: true
            }
        );
        let backups: Vec<_> = std::fs::read_dir(paths.backups()).unwrap().collect();
        assert_eq!(backups.len(), 1);
    }

    #[test]
    fn periodic_backup_is_throttled_by_the_backend() {
        let (_dir, paths) = temp_profile();
        let api = Api::new(vec!["en".into()]).unwrap();
        open_collection(&api, &paths).unwrap();
        assert!(periodic_backup(&api, &paths).unwrap());
        let _: Empty = api.call("await_backup_completion", &Empty {}).unwrap();
        // too soon for another
        assert!(!periodic_backup(&api, &paths).unwrap());
        close_collection(&api, &paths).unwrap();
    }
}
