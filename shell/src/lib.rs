// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! A native macOS shell for Anki: the existing `ts/` pages on the system
//! WebView, talking to an in-process Rust backend. See
//! `native-shell-port-plan.md`. macOS only.

#![cfg(target_os = "macos")]

pub mod ankiconnect;
pub mod api;
pub mod app;
pub mod audio;
pub mod bench;
pub mod bridge;
pub mod card_text;
pub mod editor;
pub mod latex;
pub mod main_window;
pub mod menus;
pub mod pages;
pub mod profile;
pub mod protocol;
pub mod reviewer;
pub mod settings;
pub mod shortcuts;
pub mod sync;
pub mod testchan;

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;
use std::sync::RwLock;

use crate::api::Api;
use crate::bridge::Bridge;
use crate::pages::Pages;
use crate::profile::OpenError;
use crate::profile::ProfilePaths;

/// Handles one of the `mediasrv` endpoints that depend on UI state:
/// `(shell, webview label, request bytes) -> response bytes`.
pub type FrontendHandler =
    Box<dyn Fn(&Shell, &str, &[u8]) -> Result<Vec<u8>, String> + Send + Sync>;

/// State shared by the protocol handler, the bridge, AnkiConnect and the
/// windows.
pub struct Shell {
    pub api: Api,
    /// Bundled web assets (`out/qt/_aqt/data/web` in a dev build).
    pub web: PathBuf,
    pub pages: Pages,
    pub bridge: Bridge,
    open: Mutex<Option<ProfilePaths>>,
    frontend: RwLock<HashMap<String, FrontendHandler>>,
}

impl Shell {
    pub fn new(api: Api, web: PathBuf) -> Shell {
        Shell {
            api,
            web,
            pages: Pages::default(),
            bridge: Bridge::default(),
            open: Mutex::new(None),
            frontend: RwLock::new(HashMap::new()),
        }
    }

    pub fn open_profile(&self, paths: ProfilePaths) -> Result<(), OpenError> {
        let mut open = self.open.lock().unwrap();
        if open.as_ref() == Some(&paths) {
            return Ok(());
        }
        if open.is_some() {
            return Err(OpenError::Other("another profile is open".into()));
        }
        profile::open_collection(&self.api, &paths)?;
        tracing::info!(profile = paths.name, "collection open");
        *open = Some(paths);
        Ok(())
    }

    /// Close the open profile's collection, with a quick check and backup.
    pub fn close_profile(&self) -> Option<profile::CloseReport> {
        let paths = self.open.lock().unwrap().take()?;
        match profile::close_collection(&self.api, &paths) {
            Ok(report) => {
                tracing::info!(?report, "collection closed");
                Some(report)
            }
            Err(err) => {
                tracing::error!(%err, "closing collection failed");
                None
            }
        }
    }

    pub fn open_profile_paths(&self) -> Option<ProfilePaths> {
        self.open.lock().unwrap().clone()
    }

    pub fn media_folder(&self) -> Option<PathBuf> {
        self.open.lock().unwrap().as_ref().map(ProfilePaths::media)
    }

    /// The periodic backup check; does nothing without an open profile.
    pub fn periodic_backup(&self) {
        let Some(paths) = self.open_profile_paths() else {
            return;
        };
        match profile::periodic_backup(&self.api, &paths) {
            Ok(true) => tracing::info!("periodic backup started"),
            Ok(false) => {}
            Err(err) => tracing::warn!(%err, "periodic backup failed"),
        }
    }

    pub fn register_frontend(
        &self,
        method: &str,
        handler: impl Fn(&Shell, &str, &[u8]) -> Result<Vec<u8>, String> + Send + Sync + 'static,
    ) {
        self.frontend
            .write()
            .unwrap()
            .insert(method.to_string(), Box::new(handler));
    }

    /// A UI-state endpoint (see `protocol::SHELL_POST_HANDLERS`).
    pub fn frontend(&self, label: &str, method: &str, input: &[u8]) -> Result<Vec<u8>, String> {
        match self.frontend.read().unwrap().get(method) {
            Some(handler) => handler(self, label, input),
            None => {
                tracing::warn!(method, label, "frontend endpoint not implemented yet");
                Err(format!("{method} is not available in the native shell yet"))
            }
        }
    }
}
