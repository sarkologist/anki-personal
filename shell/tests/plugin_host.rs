// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The real plugin host (`plugin_host/host.py`, this checkout's pylib) on a
//! scratch collection, with a probe add-on. Needs the build's Python
//! (`out/pyenv`); skipped without it.

use std::path::Path;
use std::path::PathBuf;
use std::sync::mpsc;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Duration;

use anki_shell::api::Api;
use anki_shell::plugins::HostConfig;
use anki_shell::plugins::PluginHost;
use anki_shell::profile::ProfilePaths;
use anki_shell::Shell;
use serde_json::json;
use serde_json::Value;

const PROBE: &str = r#"
import os
from anki._backend import RustBackend
from aqt import gui_hooks, mw

def message(handled, text, context):
    if text == "probe:count":
        return (True, mw.col.note_count())
    if text == "probe:close":
        try:
            # past RemoteBackend's own no-op, to the shell
            RustBackend.close_collection(mw.col._backend, downgrade_to_schema11=False)
        except Exception as exc:
            return (True, str(exc))
        return (True, "closed")
    if text == "probe:crash":
        os._exit(3)
    if text == "probe:ask":
        return (True, mw.native.request("ask", question="six times seven"))
    if text == "probe:freeze":
        import signal
        os.kill(os.getpid(), signal.SIGSTOP)
    return handled

gui_hooks.webview_did_receive_js_message.append(message)
gui_hooks.profile_will_close.append(
    lambda: open(os.path.join(os.path.dirname(__file__), "closed"), "w").close()
)
"#;

struct Fixture {
    shell: Arc<Shell>,
    host: Arc<PluginHost>,
    requests: Mutex<mpsc::Receiver<(String, Value)>>,
    probe: PathBuf,
    _dir: tempfile::TempDir,
}

fn python() -> Option<PathBuf> {
    let python = Path::new(env!("CARGO_MANIFEST_DIR")).join("../out/pyenv/bin/python");
    python.exists().then_some(python)
}

fn fixture() -> Option<Fixture> {
    let python = python()?;
    // Unix socket paths are short; the host's lives under temp_dir()
    let dir = tempfile::tempdir().unwrap();
    let paths = ProfilePaths::new(dir.path(), "User 1");
    std::fs::create_dir_all(&paths.folder).unwrap();
    let shell = Arc::new(Shell::new(
        Api::new(vec!["en".into()]).unwrap(),
        dir.path().into(),
    ));
    shell.open_profile(paths.clone()).unwrap();
    let probe = dir.path().join("addons21/probe");
    std::fs::create_dir_all(&probe).unwrap();
    std::fs::write(probe.join("__init__.py"), PROBE).unwrap();
    let host = PluginHost::new(shell.clone());
    let (tx, rx) = mpsc::channel();
    let tx = Mutex::new(tx);
    host.set_handler(Arc::new(move |op, msg| {
        let _ = tx.lock().unwrap().send((op.to_string(), msg.clone()));
        (op == "ask").then(|| json!({"answer": 42, "to": msg["question"]}))
    }));
    let (script, search) = HostConfig::locate().unwrap();
    host.start(HostConfig {
        python,
        script,
        paths: search,
        anki_base: dir.path().into(),
        profile: paths,
        addons: vec!["probe".into()],
        log: dir.path().join("plugins.log"),
    });
    let f = Fixture {
        shell,
        host,
        requests: Mutex::new(rx),
        probe,
        _dir: dir,
    };
    f.wait_for("ready");
    Some(f)
}

impl Fixture {
    fn wait_for(&self, op: &str) -> Value {
        let rx = self.requests.lock().unwrap();
        loop {
            match rx.recv_timeout(Duration::from_secs(20)) {
                Ok((got, msg)) if got == op => return msg,
                Ok(_) => {}
                Err(_) => panic!(
                    "no {op} from the plugin host; log:\n{}",
                    std::fs::read_to_string(self._dir.path().join("plugins.log"))
                        .unwrap_or_default()
                ),
            }
        }
    }

    fn message(&self, text: &str, timeout: Duration) -> Option<Value> {
        self.host
            .event(
                "webview_did_receive_js_message",
                json!({"message": text, "context": ""}),
                timeout,
            )
            .map(|v| v["value"].clone())
    }
}

#[test]
fn add_ons_use_the_shells_collection_and_wait_out_a_full_sync() {
    let Some(f) = fixture() else {
        return;
    };
    let long = Duration::from_secs(10);
    assert_eq!(f.message("probe:count", long), Some(json!(0)));
    // a full sync holds the gate: add-on calls wait for it
    let sync = f.shell.plugin_gate.write().unwrap();
    assert_eq!(f.message("probe:count", Duration::from_millis(500)), None);
    drop(sync);
    assert_eq!(f.message("probe:count", long), Some(json!(0)));
}

#[test]
fn the_hosts_requests_get_the_shells_replies() {
    let Some(f) = fixture() else {
        return;
    };
    assert_eq!(
        f.message("probe:ask", Duration::from_secs(10)),
        Some(json!({"answer": 42, "to": "six times seven"}))
    );
}

#[test]
fn add_ons_cant_close_the_shells_collection() {
    let Some(f) = fixture() else {
        return;
    };
    let answer = f.message("probe:close", Duration::from_secs(10)).unwrap();
    assert!(
        answer
            .as_str()
            .unwrap()
            .contains("isn't available to add-ons"),
        "{answer}"
    );
    assert!(f.shell.api.query("select count() from notes", &[]).is_ok());
}

#[test]
fn a_crashed_host_is_restarted_and_a_stopped_one_closes_cleanly() {
    let Some(f) = fixture() else {
        return;
    };
    assert_eq!(f.message("probe:crash", Duration::from_secs(2)), None);
    assert_eq!(f.wait_for("stopped")["restart"], json!(true));
    f.wait_for("ready");
    assert_eq!(
        f.message("probe:count", Duration::from_secs(10)),
        Some(json!(0))
    );
    f.host.stop();
    assert!(!f.host.is_running());
    assert!(f.probe.join("closed").exists());
}

#[test]
fn a_host_that_stops_reading_cant_block_the_shell() {
    let Some(f) = fixture() else {
        return;
    };
    // it never answers: stopped before replying
    assert_eq!(f.message("probe:freeze", Duration::from_millis(500)), None);
    let started = std::time::Instant::now();
    let big = "x".repeat(4096);
    for _ in 0..500 {
        f.host
            .notify("operation_did_execute", json!({"changes": big}));
    }
    assert!(
        started.elapsed() < Duration::from_secs(10),
        "{:?}",
        started.elapsed()
    );
    // the stuck host is replaced
    assert_eq!(f.wait_for("stopped")["restart"], json!(true));
    f.wait_for("ready");
    assert_eq!(
        f.message("probe:count", Duration::from_secs(10)),
        Some(json!(0))
    );
}
