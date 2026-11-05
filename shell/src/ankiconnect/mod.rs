// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! A native subset of the AnkiConnect add-on's HTTP API (protocol version
//! 6), so automations keep working with the shell. Requests and replies
//! follow the add-on's `handler` and `web.py`; actions the shell doesn't
//! have yet say so instead of claiming to be unknown.

mod actions;
mod cards;
mod media;
mod notes;
mod params;
#[cfg(test)]
mod tests;
mod web;

use std::cell::RefCell;
use std::collections::HashMap;
use std::net::SocketAddr;
use std::rc::Rc;
use std::sync::Arc;
use std::sync::Mutex;
use std::sync::PoisonError;

use anki_proto::backend::backend_error::Kind;
use axum::body::Bytes;
use axum::extract::DefaultBodyLimit;
use axum::extract::State;
use axum::http::HeaderMap;
use axum::http::Method;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use prost::Message;
use serde_json::json;
use serde_json::Value;

use crate::api::Api;
use crate::api::ApiError;
use crate::Shell;

pub const API_VERSION: i64 = 6;

/// The add-on's settings that affect the protocol.
#[derive(Debug, Clone)]
pub struct Config {
    pub api_key: Option<String>,
    pub cors_origins: Vec<String>,
    pub ignore_origins: Vec<String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            api_key: None,
            cors_origins: vec!["http://localhost".into()],
            ignore_origins: vec![],
        }
    }
}

/// AnkiConnect's AnkiWeb id, its folder in `addons21`.
const ADDON_FOLDER: &str = "2055492159";

impl Config {
    /// The add-on's settings in the old Anki, if it's installed (its
    /// `config.json`, with the user's edits from `meta.json` on top), plus
    /// the port to listen on. Then the `ANKICONNECT_CORS_ORIGIN` override.
    pub fn for_anki_base(anki_base: &std::path::Path) -> (Self, u16) {
        let folder = anki_base.join("addons21").join(ADDON_FOLDER);
        let read = |name: &str| -> Value {
            std::fs::read(folder.join(name))
                .ok()
                .and_then(|data| serde_json::from_slice(&data).ok())
                .unwrap_or(Value::Null)
        };
        let mut settings = read("config.json");
        if let (Value::Object(settings), Value::Object(edits)) =
            (&mut settings, &read("meta.json")["config"])
        {
            settings.extend(edits.clone());
        }
        let strings = |key: &str| -> Option<Vec<String>> {
            settings[key].as_array().map(|items| {
                items
                    .iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
        };
        let mut config = Config {
            api_key: settings["apiKey"]
                .as_str()
                .filter(|key| !key.is_empty())
                .map(str::to_string),
            ..Config::default()
        };
        if let Some(origins) = strings("webCorsOriginList") {
            config.cors_origins = origins;
        }
        if let Some(origins) = strings("ignoreOriginList") {
            config.ignore_origins = origins;
        }
        let port = settings["webBindPort"]
            .as_u64()
            .and_then(|p| u16::try_from(p).ok())
            .unwrap_or(8765);
        (config.with_env(), port)
    }

    /// The defaults, plus the add-on's `ANKICONNECT_CORS_ORIGIN` override.
    pub fn from_env() -> Self {
        Config::default().with_env()
    }

    fn with_env(mut self) -> Self {
        let config = &mut self;
        if let Ok(origin) = std::env::var("ANKICONNECT_CORS_ORIGIN") {
            if !origin.is_empty() {
                config.cors_origins.push(origin);
            }
        }
        self
    }
}

/// Serve on 127.0.0.1:`port` in a background thread. A port that's taken
/// (usually by the old Anki's AnkiConnect) is logged, not fatal.
pub fn spawn(shell: Arc<Shell>, port: u16, config: Config) {
    let addr = SocketAddr::from(([127, 0, 0, 1], port));
    match serve_with(shell, addr, config) {
        Ok(addr) => tracing::info!(%addr, "AnkiConnect listening"),
        Err(err) => tracing::warn!(%addr, %err, "AnkiConnect not started"),
    }
}

/// Bind `addr` and serve in a background thread; returns the bound address,
/// so port 0 can be used.
pub fn serve(shell: Arc<Shell>, addr: SocketAddr) -> std::io::Result<SocketAddr> {
    serve_with(shell, addr, Config::default())
}

pub fn serve_with(
    shell: Arc<Shell>,
    addr: SocketAddr,
    config: Config,
) -> std::io::Result<SocketAddr> {
    let listener = std::net::TcpListener::bind(addr)?;
    listener.set_nonblocking(true)?;
    let bound = listener.local_addr()?;
    let state = Arc::new(Server {
        shell,
        config,
        busy: Mutex::new(()),
    });
    std::thread::Builder::new()
        .name("ankiconnect".into())
        .spawn(move || {
            let runtime = match tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
            {
                Ok(runtime) => runtime,
                Err(err) => {
                    tracing::warn!(%err, "AnkiConnect runtime failed");
                    return;
                }
            };
            runtime.block_on(async move {
                let listener = match tokio::net::TcpListener::from_std(listener) {
                    Ok(listener) => listener,
                    Err(err) => {
                        tracing::warn!(%err, "AnkiConnect listener failed");
                        return;
                    }
                };
                let router = axum::Router::new()
                    .fallback(endpoint)
                    .with_state(state)
                    // media is sent inline as base64, and the add-on has no limit
                    .layer(DefaultBodyLimit::disable());
                if let Err(err) = axum::serve(listener, router).await {
                    tracing::warn!(%err, "AnkiConnect server stopped");
                }
            });
        })?;
    Ok(bound)
}

struct Server {
    shell: Arc<Shell>,
    config: Config,
    /// The add-on handles one request at a time on the main thread; doing
    /// the same keeps multi-call actions like `addNotes` from interleaving.
    busy: Mutex<()>,
}

async fn endpoint(
    State(server): State<Arc<Server>>,
    method: Method,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let result = tokio::task::spawn_blocking(move || {
        let _busy = server.busy.lock().unwrap_or_else(PoisonError::into_inner);
        web::respond(&server.shell, &server.config, &method, &headers, &body)
    })
    .await;
    match result {
        Ok(resp) => resp.into_response(),
        Err(err) => {
            tracing::error!(%err, "AnkiConnect request failed");
            StatusCode::INTERNAL_SERVER_ERROR.into_response()
        }
    }
}

/// One request body -> response body, as the add-on's `handler`.
pub fn handle(shell: &Shell, request: &Value) -> Value {
    handle_with(shell, &Config::default(), request)
}

fn handle_with(shell: &Shell, config: &Config, request: &Value) -> Value {
    let get = |key| request.as_object().and_then(|r| r.get(key));
    let legacy = get("version").map_or(true, |v| v.as_f64().map_or(true, |v| v <= 4.0));
    let result = (|| {
        let Some(request) = request.as_object() else {
            return Err(Error::from(format!(
                "'{}' object has no attribute 'get'",
                params::py_type(request)
            )));
        };
        let name = request.get("action").and_then(Value::as_str).unwrap_or("");
        let key = request.get("key").unwrap_or(&Value::Null);
        let key_ok = match &config.api_key {
            None => key.is_null(),
            Some(expected) => key.as_str() == Some(expected),
        };
        if !key_ok && name != "requestPermission" {
            return Err("valid api key must be provided".into());
        }
        let empty = json!({});
        let params = request.get("params").unwrap_or(&empty);
        let ctx = Ctx::new(shell, config);
        actions::run(&ctx, name, params)
    })();
    match result {
        Ok(value) if legacy => value,
        Ok(value) => json!({"result": value, "error": null}),
        Err(err) => json!({"result": null, "error": err.message}),
    }
}

#[derive(Debug)]
pub(crate) struct Error {
    pub message: String,
    /// The backend's NotFoundError, which some actions turn into `{}` or
    /// `null` entries.
    pub not_found: bool,
}

impl Error {
    fn not_found(message: String) -> Self {
        Error {
            message,
            not_found: true,
        }
    }
}

impl From<String> for Error {
    fn from(message: String) -> Self {
        Error {
            message,
            not_found: false,
        }
    }
}

impl From<&str> for Error {
    fn from(message: &str) -> Self {
        message.to_string().into()
    }
}

impl From<ApiError> for Error {
    fn from(err: ApiError) -> Self {
        Error {
            not_found: err.kind == Kind::NotFoundError as i32,
            message: err.message,
        }
    }
}

impl From<std::io::Error> for Error {
    fn from(err: std::io::Error) -> Self {
        err.to_string().into()
    }
}

pub(crate) type Result<T, E = Error> = std::result::Result<T, E>;

/// One action's view of the shell, caching what several items of a batch
/// look up.
pub(crate) struct Ctx<'a> {
    pub shell: &'a Shell,
    pub config: &'a Config,
    notetypes: RefCell<HashMap<i64, Rc<Value>>>,
    deck_names: RefCell<Option<Rc<HashMap<i64, String>>>>,
}

impl<'a> Ctx<'a> {
    fn new(shell: &'a Shell, config: &'a Config) -> Self {
        Ctx {
            shell,
            config,
            notetypes: Default::default(),
            deck_names: Default::default(),
        }
    }

    /// The backend, if a collection is open.
    pub fn col(&self) -> Result<&'a Api> {
        if self.shell.open_profile_paths().is_none() {
            return Err("collection is not available".into());
        }
        Ok(&self.shell.api)
    }

    pub fn call<T: Message + Default>(&self, method: &str, input: &impl Message) -> Result<T> {
        Ok(self.col()?.call(method, input)?)
    }

    /// A call whose output isn't needed.
    pub fn run(&self, method: &str, input: &impl Message) -> Result<()> {
        self.col()?.raw(method, &input.encode_to_vec())?;
        Ok(())
    }

    pub fn query(&self, sql: &str, args: &[Value]) -> Result<Vec<Vec<Value>>> {
        Ok(self.col()?.query(sql, args)?)
    }

    pub fn notetype(&self, id: i64) -> Result<Rc<Value>> {
        if let Some(nt) = self.notetypes.borrow().get(&id) {
            return Ok(nt.clone());
        }
        let json: anki_proto::generic::Json = self.call(
            "get_notetype_legacy",
            &anki_proto::notetypes::NotetypeId { ntid: id },
        )?;
        let nt = Rc::new(serde_json::from_slice::<Value>(&json.json).map_err(|e| e.to_string())?);
        self.notetypes.borrow_mut().insert(id, nt.clone());
        Ok(nt)
    }

    /// `models.by_name()`.
    pub fn notetype_by_name(&self, name: &str) -> Result<Option<Rc<Value>>> {
        match self.call::<anki_proto::notetypes::NotetypeId>(
            "get_notetype_id_by_name",
            &anki_proto::generic::String { val: name.into() },
        ) {
            Ok(id) => self.notetype(id.ntid).map(Some),
            Err(err) if err.not_found => Ok(None),
            Err(err) => Err(err),
        }
    }

    /// The `getModel()` helper's error for unknown names.
    pub fn notetype_named(&self, name: &str) -> Result<Rc<Value>> {
        self.notetype_by_name(name)?
            .ok_or_else(|| format!("model was not found: {name}").into())
    }

    pub fn deck_names(&self) -> Result<Rc<HashMap<i64, String>>> {
        if let Some(names) = &*self.deck_names.borrow() {
            return Ok(names.clone());
        }
        let names = Rc::new(
            actions::all_decks(self)?
                .into_iter()
                .map(|d| (d.id, d.name))
                .collect::<HashMap<_, _>>(),
        );
        *self.deck_names.borrow_mut() = Some(names.clone());
        Ok(names)
    }
}
