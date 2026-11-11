// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The plugin host (see the plan's "Plugin host"): add-ons from the old
//! Anki's `addons21` run in a Python sidecar (`plugin_host/host.py`) with a
//! small `aqt` shim. Its pylib calls the shell's backend over a Unix socket
//! ([wire]), so plugin calls are serialized through it like page calls,
//! and it gets the shell's events (hooks, menu clicks, page messages).
//!
//! The host is a child in its own process group (with whatever it starts,
//! such as Frontier's model CLIs), restarted with a backoff if it dies. A
//! plugin can't block the shell: every event the shell waits for has a
//! timeout, and its output goes to a log file.

pub mod editors;
pub mod ui;
pub mod windows;
pub mod wire;

use std::collections::HashMap;
use std::fs::OpenOptions;
use std::os::unix::fs::PermissionsExt;
use std::os::unix::net::UnixListener;
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::path::PathBuf;
use std::process::Child;
use std::process::Command;
use std::process::Stdio;
use std::sync::atomic::AtomicU32;
use std::sync::atomic::Ordering;
use std::sync::mpsc;
use std::sync::Arc;
use std::sync::Condvar;
use std::sync::Mutex;
use std::sync::RwLock;
use std::time::Duration;
use std::time::Instant;

use anyhow::Context;
use anyhow::Result;
use prost::Message;
use serde_json::json;
use serde_json::Value;

use crate::api::Api;
use crate::profile::ProfilePaths;
use crate::Shell;

/// Gets the host's requests to the shell: (op, the whole message), and
/// returns the reply to the ones that wait for one (null if none).
pub type HostHandler = Arc<dyn Fn(&str, &Value) -> Option<Value> + Send + Sync>;

/// Backend methods add-ons can't call: the shell owns the collection's
/// lifecycle and syncing.
const DENIED_METHODS: &[&str] = &[
    "open_collection",
    "close_collection",
    "full_upload_or_download",
    "sync_collection",
    "sync_media",
    "sync_login",
    // these take the collection out of the backend
    "export_collection_package",
    "import_collection_package",
];

/// A write the host doesn't take within this means it's stuck: it's killed
/// (and restarted) rather than holding up the shell.
const WRITE_TIMEOUT: Duration = Duration::from_secs(2);

const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);
/// Restarts after a crash wait 1, 4, 9 s; more crashes than this within
/// [CRASH_WINDOW] stop the host until the shell restarts it.
const MAX_CRASHES: usize = 3;
const CRASH_WINDOW: Duration = Duration::from_secs(600);

#[derive(Debug, Clone)]
pub struct HostConfig {
    pub python: PathBuf,
    /// `host.py`.
    pub script: PathBuf,
    /// Where `anki` and `_aqt` are, besides the script's folder.
    pub paths: Vec<PathBuf>,
    pub anki_base: PathBuf,
    pub profile: ProfilePaths,
    /// Folder names in `addons21`.
    pub addons: Vec<String>,
    pub log: PathBuf,
}

impl HostConfig {
    /// The host script and its paths: `Resources/plugin_host` in the app
    /// bundle (with pylib and the generated hooks inside), else this
    /// checkout's `shell/plugin_host` with `pylib` and the build's `out`.
    pub fn locate() -> Option<(PathBuf, Vec<PathBuf>)> {
        let exe = std::env::current_exe().ok()?;
        if let Some(bundled) = exe
            .parent()
            .and_then(|p| p.parent())
            .map(|p| p.join("Resources/plugin_host/host.py"))
            .filter(|p| p.exists())
        {
            return Some((bundled, vec![]));
        }
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).parent()?;
        let script = repo.join("shell/plugin_host/host.py");
        script.exists().then(|| {
            (
                script,
                vec![
                    repo.join("pylib"),
                    repo.join("out/pylib"),
                    repo.join("out/qt"),
                ],
            )
        })
    }
}

/// Where a request's reply goes: (OK or ERR, payload).
type ReplySender = mpsc::Sender<(u8, Vec<u8>)>;

/// The socket to a running host.
struct Conn {
    writer: Mutex<UnixStream>,
    pending: Mutex<HashMap<u32, ReplySender>>,
    next: AtomicU32,
    /// The host's process group.
    pgid: i32,
}

impl Conn {
    fn send(&self, kind: u8, id: u32, payload: &[u8]) -> std::io::Result<()> {
        let mut writer = self.writer.lock().unwrap();
        let result = wire::write_frame(&mut *writer, kind, id, payload);
        if let Err(err) = &result {
            // a host that doesn't read (stopped, wedged) or is gone; the
            // supervisor sees it exit, and restarts it
            tracing::error!(%err, "plugin host isn't reading; ending it");
            let _ = writer.shutdown(std::net::Shutdown::Both);
            // SAFETY: plain syscall; the group is the host's own
            unsafe { libc::killpg(self.pgid, libc::SIGKILL) };
        }
        result
    }

    fn request(&self, kind: u8, payload: &[u8], timeout: Duration) -> Option<(u8, Vec<u8>)> {
        let id = loop {
            let id = self.next.fetch_add(1, Ordering::SeqCst);
            if id != 0 {
                break id;
            }
        };
        let (tx, rx) = mpsc::channel();
        self.pending.lock().unwrap().insert(id, tx);
        if self.send(kind, id, payload).is_err() {
            self.pending.lock().unwrap().remove(&id);
            return None;
        }
        let out = rx.recv_timeout(timeout).ok();
        self.pending.lock().unwrap().remove(&id);
        out
    }
}

#[derive(Default)]
struct Supervision {
    /// Bumped by each start and stop; a supervisor of an older one quits.
    generation: u64,
    /// The running host's process group.
    pgid: Option<i32>,
    crashes: Vec<Instant>,
}

pub struct PluginHost {
    shell: Arc<Shell>,
    handler: RwLock<Option<HostHandler>>,
    conn: Mutex<Option<Arc<Conn>>>,
    state: Mutex<Supervision>,
    exited: Condvar,
}

impl PluginHost {
    pub fn new(shell: Arc<Shell>) -> Arc<Self> {
        Arc::new(PluginHost {
            shell,
            handler: RwLock::new(None),
            conn: Mutex::new(None),
            state: Mutex::new(Supervision::default()),
            exited: Condvar::new(),
        })
    }

    pub fn set_handler(&self, handler: HostHandler) {
        *self.handler.write().unwrap() = Some(handler);
    }

    pub fn is_running(&self) -> bool {
        self.conn.lock().unwrap().is_some()
    }

    /// Start the host (stopping a running one first); it's restarted if it
    /// dies, until [Self::stop].
    pub fn start(self: &Arc<Self>, config: HostConfig) {
        self.stop();
        let generation = {
            let mut state = self.state.lock().unwrap();
            state.generation += 1;
            state.crashes.clear();
            state.generation
        };
        let host = self.clone();
        std::thread::spawn(move || host.supervise(config, generation));
    }

    /// Let add-ons finish (`profile_will_close`), then end the host and
    /// everything it started.
    pub fn stop(&self) {
        if self.is_running() {
            self.event("profile_will_close", json!({}), Duration::from_secs(3));
            self.notify("shutdown", json!({}));
        }
        let mut state = self.state.lock().unwrap();
        state.generation += 1;
        if state.pgid.is_some() {
            state = self
                .exited
                .wait_timeout_while(state, Duration::from_secs(2), |s| s.pgid.is_some())
                .unwrap()
                .0;
        }
        if let Some(pgid) = state.pgid {
            // SAFETY: plain syscall; the group is the host's own
            unsafe { libc::killpg(pgid, libc::SIGKILL) };
        }
    }

    /// Send an event and wait for the host's answer; None if no host is
    /// running, it failed, or it took longer than `timeout`.
    pub fn event(&self, event: &str, fields: Value, timeout: Duration) -> Option<Value> {
        let conn = self.conn.lock().unwrap().clone()?;
        let (kind, payload) = conn.request(wire::EVENT, &event_json(event, fields), timeout)?;
        match kind {
            wire::OK => serde_json::from_slice(&payload).ok(),
            _ => {
                tracing::warn!(
                    event,
                    error = %String::from_utf8_lossy(&payload),
                    "plugin event failed"
                );
                None
            }
        }
    }

    /// Send an event without waiting.
    pub fn notify(&self, event: &str, fields: Value) {
        if let Some(conn) = self.conn.lock().unwrap().clone() {
            let _ = conn.send(wire::EVENT, 0, &event_json(event, fields));
        }
    }

    fn supervise(self: Arc<Self>, config: HostConfig, generation: u64) {
        loop {
            let started = Instant::now();
            match self.launch(&config, generation) {
                Ok(child) => self.run(child),
                Err(err) => tracing::error!("plugin host failed to start: {err:#}"),
            }
            let wait = {
                let mut state = self.state.lock().unwrap();
                if state.generation != generation {
                    return;
                }
                state.crashes.retain(|t| t.elapsed() < CRASH_WINDOW);
                state.crashes.push(started);
                let crashes = state.crashes.len();
                if crashes > MAX_CRASHES {
                    tracing::error!("plugin host keeps failing; not restarting it");
                    drop(state);
                    self.host_request("stopped", &json!({"op": "stopped", "restart": false}));
                    return;
                }
                Duration::from_secs((crashes * crashes) as u64)
            };
            tracing::warn!(?wait, "plugin host exited; restarting");
            self.host_request("stopped", &json!({"op": "stopped", "restart": true}));
            std::thread::sleep(wait);
            if self.state.lock().unwrap().generation != generation {
                return;
            }
        }
    }

    fn launch(&self, config: &HostConfig, generation: u64) -> Result<Child> {
        let dir = std::env::temp_dir().join(format!("anki-shell-{}", std::process::id()));
        std::fs::create_dir_all(&dir)?;
        // the socket gives the whole collection to whoever connects
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700))?;
        static SOCKETS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
        let socket = dir.join(format!(
            "plugins-{}.sock",
            SOCKETS.fetch_add(1, Ordering::SeqCst)
        ));
        let _ = std::fs::remove_file(&socket);
        let listener =
            UnixListener::bind(&socket).with_context(|| format!("binding {}", socket.display()))?;
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&config.log)
            .with_context(|| format!("opening {}", config.log.display()))?;
        let mut cmd = Command::new(&config.python);
        cmd.arg("-I")
            .arg("-u")
            .arg(&config.script)
            .arg("--socket")
            .arg(&socket)
            .arg("--base")
            .arg(&config.anki_base)
            .arg("--profile")
            .arg(&config.profile.name)
            .arg("--collection")
            .arg(config.profile.collection());
        for addon in &config.addons {
            cmd.arg("--addon").arg(addon);
        }
        for path in &config.paths {
            cmd.arg("--path").arg(path);
        }
        let mut child = cmd
            .process_group(0)
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log)
            .spawn()
            .with_context(|| format!("running {}", config.python.display()))?;
        {
            let mut state = self.state.lock().unwrap();
            if state.generation != generation {
                // stopped meanwhile
                let _ = child.kill();
                let _ = child.wait();
                anyhow::bail!("stopped");
            }
            state.pgid = Some(child.id() as i32);
        }
        let stream = accept(&listener, &mut child);
        let _ = std::fs::remove_file(&socket);
        let stream = match stream {
            Ok(stream) => stream,
            Err(err) => {
                let _ = child.kill();
                let _ = child.wait();
                self.state.lock().unwrap().pgid = None;
                return Err(err);
            }
        };
        let writer = stream.try_clone()?;
        writer.set_write_timeout(Some(WRITE_TIMEOUT))?;
        let conn = Arc::new(Conn {
            writer: Mutex::new(writer),
            pending: Mutex::new(HashMap::new()),
            next: AtomicU32::new(1),
            pgid: child.id() as i32,
        });
        *self.conn.lock().unwrap() = Some(conn.clone());
        let shell = self.shell.clone();
        let handler = self.handler.read().unwrap().clone();
        std::thread::spawn(move || serve(stream, conn, shell, handler));
        tracing::info!(pid = child.id(), addons = ?config.addons, "plugin host started");
        Ok(child)
    }

    /// Wait for the host to exit, then forget it.
    fn run(&self, mut child: Child) {
        let status = child.wait();
        tracing::info!(?status, "plugin host exited");
        *self.conn.lock().unwrap() = None;
        if let Some(pgid) = self.state.lock().unwrap().pgid.take() {
            // what it started goes with it
            // SAFETY: plain syscall; the group was the host's
            unsafe { libc::killpg(pgid, libc::SIGKILL) };
        }
        self.exited.notify_all();
    }

    fn host_request(&self, op: &str, msg: &Value) {
        if let Some(handler) = self.handler.read().unwrap().clone() {
            let _ = handler(op, msg);
        }
    }
}

fn event_json(event: &str, fields: Value) -> Vec<u8> {
    let mut msg = match fields {
        Value::Object(map) => map,
        _ => Default::default(),
    };
    msg.insert("event".into(), event.into());
    Value::Object(msg).to_string().into_bytes()
}

fn accept(listener: &UnixListener, child: &mut Child) -> Result<UnixStream> {
    listener.set_nonblocking(true)?;
    let deadline = Instant::now() + CONNECT_TIMEOUT;
    loop {
        match listener.accept() {
            Ok((stream, _)) => {
                stream.set_nonblocking(false)?;
                return Ok(stream);
            }
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {}
            Err(err) => return Err(err.into()),
        }
        if let Some(status) = child.try_wait()? {
            anyhow::bail!("the plugin host exited before connecting ({status})");
        }
        if Instant::now() > deadline {
            anyhow::bail!("the plugin host didn't connect");
        }
        std::thread::sleep(Duration::from_millis(20));
    }
}

/// Read the host's frames until it goes away: backend calls, requests to
/// the shell, and replies to events.
fn serve(mut stream: UnixStream, conn: Arc<Conn>, shell: Arc<Shell>, handler: Option<HostHandler>) {
    // the host's requests, in the order it sent them (a menu's items, say,
    // come whole each time, so the last one must win)
    let (requests, queue) = mpsc::channel::<wire::Frame>();
    let replies = conn.clone();
    std::thread::spawn(move || {
        for frame in queue {
            let mut reply = None;
            match serde_json::from_slice::<Value>(&frame.payload) {
                Ok(msg) => {
                    let op = msg["op"].as_str().unwrap_or_default().to_string();
                    if let Some(handler) = &handler {
                        reply = handler(&op, &msg);
                    }
                }
                Err(err) => tracing::warn!(%err, "bad plugin host request"),
            }
            if frame.id != 0 {
                let reply = reply.unwrap_or(Value::Null).to_string();
                let _ = replies.send(wire::OK, frame.id, reply.as_bytes());
            }
        }
    });
    loop {
        let frame = match wire::read_frame(&mut stream) {
            Ok(Some(frame)) => frame,
            Ok(None) => break,
            Err(err) => {
                tracing::warn!(%err, "plugin host socket");
                break;
            }
        };
        match frame.kind {
            wire::OK | wire::ERR => {
                if let Some(tx) = conn.pending.lock().unwrap().remove(&frame.id) {
                    let _ = tx.send((frame.kind, frame.payload));
                }
            }
            // a call can take a while; others mustn't wait for it
            wire::CALL | wire::DB => {
                let conn = conn.clone();
                let shell = shell.clone();
                std::thread::spawn(move || {
                    let (kind, out) = backend_call(&shell, frame.kind, &frame.payload);
                    let _ = conn.send(kind, frame.id, &out);
                });
            }
            wire::HOST => {
                let _ = requests.send(frame);
            }
            kind => tracing::warn!(kind, "unexpected plugin host frame"),
        }
    }
    // waiting events fail now rather than at their timeout
    conn.pending.lock().unwrap().clear();
    let _ = stream.shutdown(std::net::Shutdown::Both);
}

/// Run a CALL or DB frame on the shell's backend, while no full sync has
/// the collection.
fn backend_call(shell: &Shell, kind: u8, payload: &[u8]) -> (u8, Vec<u8>) {
    let _sync = shell.plugin_gate.read().unwrap();
    let result = if kind == wire::DB {
        shell.api.db_command_raw(payload)
    } else {
        match wire::split_call(payload) {
            None => Err(backend_error("bad call")),
            Some((service, method, input)) => match Api::method_name(service, method) {
                Some(name) if DENIED_METHODS.contains(&name) => {
                    tracing::warn!(name, "denied add-on call");
                    Err(backend_error(&format!("{name} isn't available to add-ons")))
                }
                _ => shell.api.raw_indexed(service, method, input),
            },
        }
    };
    match result {
        Ok(out) => (wire::OK, out),
        Err(err) => (wire::ERR, err),
    }
}

fn backend_error(message: &str) -> Vec<u8> {
    anki_proto::backend::BackendError {
        message: message.into(),
        kind: anki_proto::backend::backend_error::Kind::InvalidInput as i32,
        ..Default::default()
    }
    .encode_to_vec()
}
