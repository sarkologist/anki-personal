// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Audio the reviewer page can't play itself. The page plays sounds with
//! `<audio>` and TTS with `speechSynthesis` (the system's
//! AVSpeechSynthesizer); a file WebKit refuses is handed to mpv, as the Qt
//! app plays everything (`aqt/sound.py`). mpv comes from Anki's
//! `anki_audio` package or Homebrew.

use std::io::Write;
use std::path::PathBuf;
use std::process::Child;
use std::process::Command;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::Mutex;

use crate::app::Ui;
use crate::app::MAIN;

struct Mpv {
    child: Child,
    socket: PathBuf,
}

static MPV: Mutex<Option<Mpv>> = Mutex::new(None);

/// mpv from an Anki install's `anki_audio`, else Homebrew's.
pub fn find_mpv() -> Option<PathBuf> {
    let home = dirs::home_dir()?;
    let data = dirs::data_dir()?;
    let venvs = [
        data.join("AnkiProgramFiles/.venv/lib"),
        home.join(".anki-stable/lib"),
    ];
    for lib in venvs {
        let Ok(entries) = std::fs::read_dir(&lib) else {
            continue;
        };
        for entry in entries.flatten() {
            let mpv = entry.path().join("site-packages/anki_audio/mpv");
            if mpv.exists() {
                return Some(mpv);
            }
        }
    }
    ["/opt/homebrew/bin/mpv", "/usr/local/bin/mpv"]
        .into_iter()
        .map(PathBuf::from)
        .find(|p| p.exists())
}

/// Play a media file with mpv; the page hears `_shellAv.mpvEnded()` when it
/// stops. Only plain file names inside the media folder are played.
pub fn play_with_mpv(ui: &Arc<Ui>, file: &str) {
    stop_mpv();
    let Some(media) = ui.shell.media_folder() else {
        return;
    };
    let path = media.join(file);
    let inside = std::path::Path::new(file)
        .components()
        .all(|c| matches!(c, std::path::Component::Normal(_)));
    let (Some(mpv), true, true) = (find_mpv(), inside, path.is_file()) else {
        tracing::warn!(file, "can't play with mpv");
        ui.eval(MAIN, "_shellAv.mpvEnded();".into());
        return;
    };
    let socket = std::env::temp_dir().join(format!("anki-shell-mpv-{}.sock", std::process::id()));
    let child = Command::new(mpv)
        .arg("--no-terminal")
        .arg("--force-window=no")
        .arg("--keep-open=no")
        .arg(format!("--input-ipc-server={}", socket.display()))
        .arg("--")
        .arg(&path)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn();
    let child = match child {
        Ok(child) => child,
        Err(err) => {
            tracing::warn!(%err, "starting mpv failed");
            ui.eval(MAIN, "_shellAv.mpvEnded();".into());
            return;
        }
    };
    let pid = child.id();
    *MPV.lock().unwrap() = Some(Mpv { child, socket });
    let ui = ui.clone();
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_millis(100));
        let mut guard = MPV.lock().unwrap();
        let ended = match guard.as_mut() {
            Some(m) if m.child.id() == pid => m.child.try_wait().map_or(true, |s| s.is_some()),
            // stopped, or replaced by a newer file
            _ => return,
        };
        if ended {
            *guard = None;
            drop(guard);
            ui.eval(MAIN, "_shellAv.mpvEnded();".into());
            return;
        }
    });
}

pub fn stop_mpv() {
    if let Some(mut m) = MPV.lock().unwrap().take() {
        let _ = m.child.kill();
        let _ = m.child.wait();
        let _ = std::fs::remove_file(&m.socket);
    }
}

/// An mpv command over its IPC socket (pause, seek).
pub fn mpv_command(args: &[&str]) {
    let socket = match MPV.lock().unwrap().as_ref() {
        Some(m) => m.socket.clone(),
        None => return,
    };
    let cmd = serde_json::json!({ "command": args });
    if let Ok(mut stream) = std::os::unix::net::UnixStream::connect(socket) {
        let _ = writeln!(stream, "{cmd}");
    }
}
