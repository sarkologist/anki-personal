// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Headless collection and media sync, the way `aqt.sync`, `aqt.mediasync`
//! and `aqt.profiles` drive the backend. The UI layer owns the dialogs and
//! progress display; this module owns the backend calls, the profile's sync
//! settings, and the AnkiWeb guard.
//!
//! Every call blocks until the backend returns, so run them off the main
//! thread. Every function that can reach the network takes the Anki base
//! folder and calls [`check_allowed`] first: a profile copy may only sync
//! with a server on the loopback interface, never AnkiWeb, since its sync
//! key is the real one.

use std::net::IpAddr;
use std::path::Path;

use anki_proto::backend::backend_error::Kind;
use anki_proto::collection::progress;
use anki_proto::collection::CreateBackupRequest;
use anki_proto::collection::Progress;
use anki_proto::generic::Empty;
use anki_proto::sync::sync_collection_response::ChangesRequired;
use anki_proto::sync::sync_status_response;
use anki_proto::sync::FullUploadOrDownloadRequest;
use anki_proto::sync::MediaSyncProgress;
use anki_proto::sync::MediaSyncStatusResponse;
pub use anki_proto::sync::SyncAuth;
use anki_proto::sync::SyncCollectionRequest;
use anki_proto::sync::SyncCollectionResponse;
use anki_proto::sync::SyncLoginRequest;
use anki_proto::sync::SyncStatusResponse;
use anyhow::anyhow;
use anyhow::bail;
use anyhow::Context;
use anyhow::Result;

use crate::api::Api;
use crate::api::ApiError;
use crate::profile::ProfilePaths;
use crate::settings::default_anki_base;
use crate::settings::SyncSettings;

/// `profiles.network_timeout()`'s default (the old Anki has no UI for it).
pub const NETWORK_TIMEOUT_SECS: u32 = 60;

/// The server to sync with: the last redirect, else the user's custom
/// server, else None (AnkiWeb). `profiles.sync_endpoint()`.
pub fn endpoint(sync: &SyncSettings) -> Option<String> {
    [&sync.current_sync_url, &sync.custom_sync_url]
        .into_iter()
        .flatten()
        .find(|url| !url.is_empty())
        .cloned()
}

/// The credentials for a sync, or None if the profile isn't logged in.
/// `profiles.sync_auth()`.
pub fn auth(sync: &SyncSettings) -> Option<SyncAuth> {
    let hkey = sync.sync_key.as_deref().filter(|k| !k.is_empty())?;
    Some(SyncAuth {
        hkey: hkey.to_string(),
        endpoint: endpoint(sync),
        io_timeout_secs: Some(NETWORK_TIMEOUT_SECS),
    })
}

/// `profiles.media_syncing_enabled()`: on unless turned off.
pub fn media_enabled(sync: &SyncSettings) -> bool {
    sync.sync_media.unwrap_or(true)
}

/// `profiles.auto_syncing_enabled()`: sync on open/close unless turned off.
pub fn auto_sync_enabled(sync: &SyncSettings) -> bool {
    sync.auto_sync.unwrap_or(true)
}

/// The AnkiWeb guard. The live base (the old Anki's default folder) may
/// sync anywhere; any other base is a profile copy, which may only sync
/// with an explicit server on a loopback address.
pub fn check_allowed(anki_base: &Path, endpoint: Option<&str>) -> Result<()> {
    if same_folder(anki_base, &default_anki_base()) {
        return Ok(());
    }
    let copy = anki_base.display();
    let Some(endpoint) = endpoint else {
        bail!(
            "Refusing to sync the profile copy in {copy} with AnkiWeb. \
             Set a custom sync server on 127.0.0.1 for it."
        );
    };
    // parsed the way rslib will parse it, so the host checked is the host
    // contacted
    let parsed = anki::sync::login::SyncAuth::try_from(SyncAuth {
        hkey: String::new(),
        endpoint: Some(endpoint.into()),
        io_timeout_secs: None,
    })
    .map_err(|e| anyhow!("{e}"))
    .with_context(|| format!("sync server {endpoint:?}"))?;
    let host = parsed
        .endpoint
        .as_ref()
        .and_then(|url| url.host_str())
        .unwrap_or_default();
    if !is_loopback_host(host) {
        bail!(
            "Refusing to sync the profile copy in {copy} with {endpoint:?}: \
             a copy may only sync with a server on 127.0.0.1, ::1 or localhost."
        );
    }
    Ok(())
}

pub fn same_folder(a: &Path, b: &Path) -> bool {
    let canonical = |p: &Path| std::fs::canonicalize(p).unwrap_or_else(|_| p.to_path_buf());
    canonical(a) == canonical(b)
}

/// `host` as the URL parser normalised it (IPv6 in brackets).
fn is_loopback_host(host: &str) -> bool {
    let ip = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    host == "localhost" || ip.parse::<IpAddr>().is_ok_and(|ip| ip.is_loopback())
}

/// Log in and return the new credentials (`sync.sync_login`). Errors are
/// [`ApiError`]s (check with [`is_auth_error`]) or a guard refusal.
pub fn login(
    api: &Api,
    anki_base: &Path,
    username: &str,
    password: &str,
    endpoint: Option<&str>,
) -> Result<SyncAuth> {
    check_allowed(anki_base, endpoint)?;
    Ok(api.call(
        "sync_login",
        &SyncLoginRequest {
            username: username.into(),
            password: password.into(),
            endpoint: endpoint.map(Into::into),
        },
    )?)
}

/// Save a login's key and username; the endpoint fields are kept.
pub fn store_login(sync: &mut SyncSettings, username: &str, auth: &SyncAuth) {
    sync.sync_key = Some(auth.hkey.clone());
    sync.sync_user = Some(username.to_string());
}

/// `profiles.clear_sync_auth()`, for an auth error or a logout.
pub fn clear_auth(sync: &mut SyncSettings) {
    sync.sync_key = None;
    sync.sync_user = None;
    sync.host_num = None;
    sync.current_sync_url = None;
}

/// What a collection sync did or still needs.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Required {
    /// Up to date: there were no changes, or a normal sync completed (rslib
    /// doesn't distinguish the two). Media sync, if asked for, is running.
    Done,
    /// A one-way sync is needed; call [`full_sync`].
    FullSync(FullSyncChoice),
}

/// Which directions a full sync may take. The old Anki confirms even the
/// one-way cases, since a wrong guess loses data.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum FullSyncChoice {
    /// Both sides have changes: the user picks upload or download.
    UploadOrDownload,
    /// The local collection is empty.
    DownloadOnly,
    /// The server's collection is empty.
    UploadOnly,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    pub required: Required,
    pub host_number: u32,
    /// A message from the server to show the user.
    pub server_message: Option<String>,
    /// The server redirected us; persist with [`apply_outcome`].
    pub new_endpoint: Option<String>,
    pub server_media_usn: i32,
}

impl Outcome {
    /// The `server_media_usn` argument for [`full_sync`]: media sync
    /// follows the full sync only if enabled.
    pub fn full_sync_media_usn(&self, sync_media: bool) -> Option<i32> {
        sync_media.then_some(self.server_media_usn)
    }
}

impl TryFrom<SyncCollectionResponse> for Outcome {
    type Error = anyhow::Error;

    fn try_from(out: SyncCollectionResponse) -> Result<Self> {
        let required = match ChangesRequired::try_from(out.required) {
            Ok(ChangesRequired::NoChanges) => Required::Done,
            Ok(ChangesRequired::FullSync) => Required::FullSync(FullSyncChoice::UploadOrDownload),
            Ok(ChangesRequired::FullDownload) => Required::FullSync(FullSyncChoice::DownloadOnly),
            Ok(ChangesRequired::FullUpload) => Required::FullSync(FullSyncChoice::UploadOnly),
            // rslib completes a normal sync before returning
            Ok(ChangesRequired::NormalSync) | Err(_) => {
                bail!("unexpected sync result {}", out.required)
            }
        };
        Ok(Outcome {
            required,
            host_number: out.host_number,
            server_message: Some(out.server_message).filter(|m| !m.is_empty()),
            new_endpoint: out.new_endpoint.filter(|e| !e.is_empty()),
            server_media_usn: out.server_media_usn,
        })
    }
}

/// A normal sync (`sync.sync_collection`). Holds the collection for the
/// whole sync. With `sync_media`, a media sync starts in the background
/// when no full sync is needed; follow it with [`media_status`].
pub fn sync_collection(
    api: &Api,
    anki_base: &Path,
    auth: &SyncAuth,
    sync_media: bool,
) -> Result<Outcome> {
    check_allowed(anki_base, auth.endpoint.as_deref())?;
    let out: SyncCollectionResponse = api.call(
        "sync_collection",
        &SyncCollectionRequest {
            auth: Some(auth.clone()),
            sync_media,
        },
    )?;
    out.try_into()
}

/// Persist a sync's host number and redirect, as `sync.py` does after
/// every sync.
pub fn apply_outcome(sync: &mut SyncSettings, outcome: &Outcome) {
    sync.host_num = Some(outcome.host_number.into());
    if let Some(url) = &outcome.new_endpoint {
        sync.current_sync_url = Some(url.clone());
    }
}

/// A full upload or download (`sync.full_upload` / `full_download`). A
/// download is preceded by a forced backup. rslib closes the collection,
/// transfers it, and reopens it (also on failure), so anything cached from
/// it must be reloaded afterwards. With `server_media_usn` (see
/// [`Outcome::full_sync_media_usn`]), a media sync starts in the
/// background after a successful transfer.
pub fn full_sync(
    api: &Api,
    anki_base: &Path,
    paths: &ProfilePaths,
    auth: &SyncAuth,
    upload: bool,
    server_media_usn: Option<i32>,
) -> Result<()> {
    check_allowed(anki_base, auth.endpoint.as_deref())?;
    if !upload {
        backup_now(api, &paths.backups())?;
    }
    let _: Empty = api.call(
        "full_upload_or_download",
        &FullUploadOrDownloadRequest {
            auth: Some(auth.clone()),
            upload,
            server_usn: server_media_usn,
        },
    )?;
    Ok(())
}

/// `mw.create_backup_now()`: forced, and waits until written.
fn backup_now(api: &Api, folder: &Path) -> Result<()> {
    std::fs::create_dir_all(folder)
        .with_context(|| format!("creating backup folder {}", folder.display()))?;
    let _: anki_proto::generic::Bool = api.call(
        "create_backup",
        &CreateBackupRequest {
            backup_folder: folder.to_string_lossy().into(),
            force: true,
            wait_for_completion: true,
        },
    )?;
    Ok(())
}

/// Start a media sync in the background, unless one is running
/// (`mediasync.MediaSyncer.start`). The collection must be open.
pub fn sync_media(api: &Api, anki_base: &Path, auth: &SyncAuth) -> Result<()> {
    check_allowed(anki_base, auth.endpoint.as_deref())?;
    let _: Empty = api.call("sync_media", auth)?;
    Ok(())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StatusRequired {
    NoChanges,
    NormalSync,
    FullSync,
}

/// For the toolbar's sync indicator.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Status {
    pub required: StatusRequired,
    pub new_endpoint: Option<String>,
}

/// Whether a sync is needed (`sync.get_sync_status`). Local changes answer
/// without the network, and the backend caches the server's answer for 5
/// minutes. The old Anki ignores errors here.
pub fn status(api: &Api, anki_base: &Path, auth: &SyncAuth) -> Result<Status> {
    check_allowed(anki_base, auth.endpoint.as_deref())?;
    let out: SyncStatusResponse = api.call("sync_status", auth)?;
    use sync_status_response::Required as R;
    let required = match R::try_from(out.required) {
        Ok(R::NoChanges) => StatusRequired::NoChanges,
        Ok(R::NormalSync) => StatusRequired::NormalSync,
        Ok(R::FullSync) => StatusRequired::FullSync,
        Err(_) => bail!("unexpected sync status {}", out.required),
    };
    Ok(Status {
        required,
        new_endpoint: out.new_endpoint.filter(|e| !e.is_empty()),
    })
}

/// Persist a status check's redirect.
pub fn apply_status(sync: &mut SyncSettings, status: &Status) {
    if let Some(url) = &status.new_endpoint {
        sync.current_sync_url = Some(url.clone());
    }
}

/// Whether a media sync is running, with its latest progress. Media sync
/// is done when `active` turns false; if it failed, the first call after
/// it stopped returns the error (once).
pub fn media_status(api: &Api) -> Result<MediaSyncStatusResponse, ApiError> {
    api.call("media_sync_status", &Empty {})
}

/// The progress of a running sync, for a timer to show (`sync.py` polls
/// every 150ms).
#[derive(Debug, Clone, PartialEq)]
pub enum SyncProgress {
    Normal {
        stage: String,
        added: String,
        removed: String,
    },
    Full {
        transferred: u32,
        total: u32,
    },
    Media(MediaSyncProgress),
}

impl SyncProgress {
    /// One line, roughly as the old Anki shows it.
    pub fn text(&self) -> String {
        match self {
            SyncProgress::Normal {
                stage,
                added,
                removed,
            } => format!("{stage}: {added}, {removed}"),
            SyncProgress::Full { transferred, total } => {
                format!("{} / {} KiB", transferred / 1024, total / 1024)
            }
            SyncProgress::Media(p) => format!("{}, {}, {}", p.added, p.removed, p.checked),
        }
    }
}

/// The latest sync progress, or None if the backend's latest progress is
/// something else.
pub fn progress(api: &Api) -> Result<Option<SyncProgress>, ApiError> {
    let out: Progress = api.call("latest_progress", &Empty {})?;
    Ok(match out.value {
        Some(progress::Value::NormalSync(p)) => Some(SyncProgress::Normal {
            stage: p.stage,
            added: p.added,
            removed: p.removed,
        }),
        Some(progress::Value::FullSync(p)) => Some(SyncProgress::Full {
            transferred: p.transferred,
            total: p.total,
        }),
        Some(progress::Value::MediaSync(p)) => Some(SyncProgress::Media(p)),
        _ => None,
    })
}

/// Cancel a running login, normal or full sync; it then fails with an
/// interrupted error ([`is_interrupted`]).
pub fn abort(api: &Api) -> Result<(), ApiError> {
    api.call::<Empty>("abort_sync", &Empty {}).map(drop)
}

/// Cancel a running media sync (`MediaSyncer.abort`); doesn't wait.
pub fn abort_media(api: &Api) -> Result<(), ApiError> {
    let _: Empty = api.call("set_wants_abort", &Empty {})?;
    api.call::<Empty>("abort_media_sync", &Empty {}).map(drop)
}

/// The backend error inside an error from this module, if any.
pub fn api_error(err: &anyhow::Error) -> Option<&ApiError> {
    err.downcast_ref()
}

/// Bad or expired credentials: `sync.py` clears them ([`clear_auth`]) so
/// the next sync asks for a login.
pub fn is_auth_error(err: &ApiError) -> bool {
    err.kind == Kind::SyncAuthError as i32
}

/// The user cancelled ([`abort`], [`abort_media`]); nothing to show.
pub fn is_interrupted(err: &ApiError) -> bool {
    err.kind == Kind::Interrupted as i32
}

#[cfg(test)]
mod test {
    use std::path::PathBuf;

    use super::*;

    fn copy_base() -> PathBuf {
        PathBuf::from("/tmp/anki-shell-test-copy/Anki2")
    }

    fn refused(endpoint: Option<&str>) -> String {
        check_allowed(&copy_base(), endpoint)
            .expect_err(&format!("{endpoint:?} should be refused"))
            .to_string()
    }

    #[test]
    fn copies_may_not_sync_with_ankiweb() {
        assert!(refused(None).contains("AnkiWeb"), "{}", refused(None));
        for endpoint in [
            "",
            "https://sync.ankiweb.net/",
            "https://sync2.ankiweb.net/",
            "https://example.com/",
            "http://192.168.1.10:8080/",
            "http://0.0.0.0:8080/",
            "http://127.0.0.1.evil.com/",
            "http://localhost.evil.com/",
            "http://127.0.0.1@evil.com/",
            "http://evil.com\\@127.0.0.1/",
            "http://evil.com#@127.0.0.1/",
            "127.0.0.1:8080",
            "not a url",
        ] {
            refused(Some(endpoint));
        }
    }

    #[test]
    fn copies_may_sync_with_loopback_servers() {
        for endpoint in [
            "http://127.0.0.1:27701/",
            "http://127.0.0.1:27701",
            "https://127.0.0.1/",
            "http://127.5.6.7:1/",
            "http://localhost:8080/",
            "http://LOCALHOST:8080/",
            "http://[::1]:8080/",
            "http://user@127.0.0.1:8080/",
        ] {
            check_allowed(&copy_base(), Some(endpoint))
                .unwrap_or_else(|e| panic!("{endpoint}: {e}"));
        }
    }

    #[test]
    fn the_live_base_may_sync_anywhere() {
        let live = default_anki_base();
        check_allowed(&live, None).unwrap();
        check_allowed(&live, Some("https://sync.ankiweb.net/")).unwrap();
        check_allowed(&live, Some("https://example.com/")).unwrap();
    }

    #[test]
    fn a_folder_inside_the_live_base_is_a_copy() {
        let inside = default_anki_base().join("copies").join("Anki2");
        assert!(check_allowed(&inside, None).is_err());
    }

    fn logged_in() -> SyncSettings {
        SyncSettings {
            sync_key: Some("key".into()),
            sync_user: Some("user@example.com".into()),
            host_num: Some(3),
            ..Default::default()
        }
    }

    #[test]
    fn auth_needs_a_key() {
        assert_eq!(auth(&SyncSettings::default()), None);
        let empty_key = SyncSettings {
            sync_key: Some(String::new()),
            ..Default::default()
        };
        assert_eq!(auth(&empty_key), None);
        assert_eq!(
            auth(&logged_in()),
            Some(SyncAuth {
                hkey: "key".into(),
                endpoint: None,
                io_timeout_secs: Some(NETWORK_TIMEOUT_SECS),
            })
        );
    }

    #[test]
    fn the_endpoint_is_the_redirect_then_the_custom_server() {
        let mut sync = logged_in();
        sync.custom_sync_url = Some("http://custom/".into());
        assert_eq!(
            auth(&sync).unwrap().endpoint.as_deref(),
            Some("http://custom/")
        );
        sync.current_sync_url = Some("http://redirected/".into());
        assert_eq!(
            auth(&sync).unwrap().endpoint.as_deref(),
            Some("http://redirected/")
        );
        sync.current_sync_url = Some(String::new());
        assert_eq!(endpoint(&sync).as_deref(), Some("http://custom/"));
    }

    fn outcome(new_endpoint: Option<&str>) -> Outcome {
        Outcome {
            required: Required::Done,
            host_number: 7,
            server_message: None,
            new_endpoint: new_endpoint.map(Into::into),
            server_media_usn: 0,
        }
    }

    #[test]
    fn outcomes_persist_the_host_and_any_redirect() {
        let mut sync = logged_in();
        sync.current_sync_url = Some("http://old/".into());
        apply_outcome(&mut sync, &outcome(None));
        assert_eq!(sync.host_num, Some(7));
        assert_eq!(sync.current_sync_url.as_deref(), Some("http://old/"));
        apply_outcome(&mut sync, &outcome(Some("http://new/")));
        assert_eq!(sync.current_sync_url.as_deref(), Some("http://new/"));

        apply_status(
            &mut sync,
            &Status {
                required: StatusRequired::NoChanges,
                new_endpoint: Some("http://newer/".into()),
            },
        );
        assert_eq!(sync.current_sync_url.as_deref(), Some("http://newer/"));
    }

    #[test]
    fn sync_responses_map_to_outcomes() {
        let response = |required: ChangesRequired| SyncCollectionResponse {
            host_number: 2,
            server_message: String::new(),
            required: required as i32,
            new_endpoint: Some(String::new()),
            server_media_usn: 5,
        };
        let out = Outcome::try_from(response(ChangesRequired::FullDownload)).unwrap();
        assert_eq!(
            out.required,
            Required::FullSync(FullSyncChoice::DownloadOnly)
        );
        assert_eq!(out.server_message, None);
        assert_eq!(out.new_endpoint, None);
        assert_eq!(out.full_sync_media_usn(true), Some(5));
        assert_eq!(out.full_sync_media_usn(false), None);
        assert!(Outcome::try_from(response(ChangesRequired::NormalSync)).is_err());
    }

    #[test]
    fn login_keeps_the_endpoint_and_clearing_forgets_the_redirect() {
        let mut sync = SyncSettings {
            custom_sync_url: Some("http://custom/".into()),
            current_sync_url: Some("http://redirected/".into()),
            ..Default::default()
        };
        let auth = SyncAuth {
            hkey: "new key".into(),
            endpoint: None,
            io_timeout_secs: None,
        };
        store_login(&mut sync, "me", &auth);
        assert_eq!(sync.sync_key.as_deref(), Some("new key"));
        assert_eq!(sync.sync_user.as_deref(), Some("me"));
        assert_eq!(sync.current_sync_url.as_deref(), Some("http://redirected/"));

        clear_auth(&mut sync);
        assert_eq!(
            sync,
            SyncSettings {
                custom_sync_url: Some("http://custom/".into()),
                ..Default::default()
            }
        );
    }

    #[test]
    fn errors_are_classified_by_kind() {
        let err = |kind: Kind| ApiError {
            kind: kind as i32,
            message: String::new(),
        };
        assert!(is_auth_error(&err(Kind::SyncAuthError)));
        assert!(!is_auth_error(&err(Kind::SyncOtherError)));
        assert!(is_interrupted(&err(Kind::Interrupted)));
        assert!(!is_interrupted(&err(Kind::NetworkError)));
        let wrapped = anyhow::Error::from(err(Kind::SyncAuthError));
        assert!(api_error(&wrapped).is_some_and(is_auth_error));
    }
}
