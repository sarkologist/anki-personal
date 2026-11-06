// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Sync against rslib's own sync server, in process on 127.0.0.1. Nothing
//! here may reach AnkiWeb: every base folder is a temp dir, so the guard
//! refuses anything but a loopback server.

use std::path::Path;
use std::sync::mpsc;
use std::sync::OnceLock;
use std::time::Duration;
use std::time::Instant;

use anki::sync::http_server::default_ip_header;
use anki::sync::http_server::SimpleServer;
use anki::sync::http_server::SyncServerConfig;
use anki_proto::generic;
use anki_proto::notes::AddNoteRequest;
use anki_proto::notes::AddNoteResponse;
use anki_proto::notes::Note;
use anki_proto::notetypes::NotetypeId;
use anki_shell::api::Api;
use anki_shell::profile;
use anki_shell::profile::ProfilePaths;
use anki_shell::settings::SyncSettings;
use anki_shell::sync;
use anki_shell::sync::FullSyncChoice;
use anki_shell::sync::Required;
use anki_shell::sync::StatusRequired;
use anki_shell::sync::SyncAuth;

/// The endpoint of a sync server shared by this file's tests, with one
/// account, user:pass.
fn server() -> &'static str {
    static ENDPOINT: OnceLock<String> = OnceLock::new();
    ENDPOINT.get_or_init(|| {
        std::env::set_var("SYNC_USER1", "user:pass");
        let base = tempfile::tempdir().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let rt = tokio::runtime::Builder::new_multi_thread()
                .enable_all()
                .build()
                .unwrap();
            rt.block_on(async move {
                let (addr, server) = SimpleServer::make_server(SyncServerConfig {
                    host: "127.0.0.1".parse().unwrap(),
                    port: 0,
                    base_folder: base.path().into(),
                    ip_header: default_ip_header(),
                })
                .await
                .unwrap();
                tx.send(addr).unwrap();
                server.await.unwrap();
                drop(base);
            })
        });
        let addr = rx.recv().unwrap();
        assert!(addr.ip().is_loopback());
        format!("http://{addr}/")
    })
}

/// A profile with its collection open, in its own temp base folder.
struct Client {
    _dir: tempfile::TempDir,
    api: Api,
    paths: ProfilePaths,
}

impl Client {
    fn new() -> Client {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        let api = Api::new(vec!["en".into()]).unwrap();
        profile::open_collection(&api, &paths).unwrap();
        Client {
            _dir: dir,
            api,
            paths,
        }
    }

    fn base(&self) -> &Path {
        self._dir.path()
    }

    fn add_note(&self, front: &str) {
        let ntid: NotetypeId = self
            .api
            .call(
                "get_notetype_id_by_name",
                &generic::String {
                    val: "Basic".into(),
                },
            )
            .unwrap();
        let mut note: Note = self.api.call("new_note", &ntid).unwrap();
        note.fields[0] = front.into();
        let _: AddNoteResponse = self
            .api
            .call(
                "add_note",
                &AddNoteRequest {
                    note: Some(note),
                    deck_id: 1,
                },
            )
            .unwrap();
    }

    fn fronts(&self) -> Vec<String> {
        let mut fronts: Vec<String> = self
            .api
            .query("select flds from notes", &[])
            .unwrap()
            .into_iter()
            .map(|row| {
                let flds = row[0].as_str().unwrap();
                flds.split('\x1f').next().unwrap().to_string()
            })
            .collect();
        fronts.sort();
        fronts
    }

    fn sync(&self, auth: &SyncAuth) -> sync::Outcome {
        sync::sync_collection(&self.api, self.base(), auth, true).unwrap()
    }

    /// Poll until the background media sync stops, as `MediaSyncer` does.
    fn wait_for_media(&self) {
        let deadline = Instant::now() + Duration::from_secs(30);
        while sync::media_status(&self.api).unwrap().active {
            assert!(Instant::now() < deadline, "media sync didn't finish");
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

#[test]
fn two_profiles_sync_notes_and_media() {
    let endpoint = server();
    let a = Client::new();
    let b = Client::new();

    let mut settings = SyncSettings {
        custom_sync_url: Some(endpoint.into()),
        ..Default::default()
    };
    let login = sync::login(&a.api, a.base(), "user", "pass", Some(endpoint)).unwrap();
    sync::store_login(&mut settings, "user", &login);
    let auth = sync::auth(&settings).unwrap();
    assert_eq!(auth.endpoint.as_deref(), Some(endpoint));

    // A's first sync: the server is empty
    a.add_note("from A");
    let out = a.sync(&auth);
    assert_eq!(out.required, Required::FullSync(FullSyncChoice::UploadOnly));
    sync::apply_outcome(&mut settings, &out);
    assert_eq!(settings.host_num, Some(out.host_number.into()));
    sync::full_sync(
        &a.api,
        a.base(),
        &a.paths,
        &auth,
        true,
        out.full_sync_media_usn(true),
    )
    .unwrap();
    a.wait_for_media();
    assert!(!a.paths.backups().exists(), "an upload needs no backup");

    // B is empty, so it can only download, after a backup
    let out = b.sync(&auth);
    assert_eq!(
        out.required,
        Required::FullSync(FullSyncChoice::DownloadOnly)
    );
    sync::full_sync(
        &b.api,
        b.base(),
        &b.paths,
        &auth,
        false,
        out.full_sync_media_usn(true),
    )
    .unwrap();
    b.wait_for_media();
    assert_eq!(b.fronts(), ["from A"]);
    assert_eq!(std::fs::read_dir(b.paths.backups()).unwrap().count(), 1);

    // normal syncs both ways
    b.add_note("from B");
    assert_eq!(b.sync(&auth).required, Required::Done);
    b.wait_for_media();
    assert_eq!(a.sync(&auth).required, Required::Done);
    a.wait_for_media();
    assert_eq!(a.fronts(), ["from A", "from B"]);

    // media follows a normal sync
    let name: generic::String = a
        .api
        .call(
            "add_media_file",
            &anki_proto::media::AddMediaFileRequest {
                desired_name: "synced.txt".into(),
                data: b"hello".to_vec(),
            },
        )
        .unwrap();
    assert_eq!(name.val, "synced.txt");
    assert_eq!(a.sync(&auth).required, Required::Done);
    a.wait_for_media();
    assert_eq!(b.sync(&auth).required, Required::Done);
    b.wait_for_media();
    assert_eq!(
        std::fs::read(b.paths.media().join("synced.txt")).unwrap(),
        b"hello"
    );

    let status = sync::status(&b.api, b.base(), &auth).unwrap();
    assert_eq!(status.required, StatusRequired::NoChanges);
    assert!(sync::progress(&b.api).is_ok());

    for client in [a, b] {
        profile::close_collection(&client.api, &client.paths).unwrap();
    }
}

#[test]
fn a_wrong_password_is_an_auth_error() {
    let endpoint = server();
    let dir = tempfile::tempdir().unwrap();
    let api = Api::new(vec!["en".into()]).unwrap();
    let err = sync::login(&api, dir.path(), "user", "wrong", Some(endpoint)).unwrap_err();
    let api_err = sync::api_error(&err).expect("a backend error");
    assert!(sync::is_auth_error(api_err), "{api_err:?}");
    assert!(!sync::is_interrupted(api_err));
}

#[test]
fn a_copy_without_a_server_is_refused_before_the_network() {
    // no server started, no collection open: the guard must answer first
    let dir = tempfile::tempdir().unwrap();
    let base = dir.path();
    let paths = ProfilePaths::new(base, "User 1");
    let api = Api::new(vec!["en".into()]).unwrap();
    let ankiweb = SyncAuth {
        hkey: "a real key".into(),
        endpoint: None,
        io_timeout_secs: None,
    };
    let errors = [
        sync::login(&api, base, "user", "pass", None).unwrap_err(),
        sync::sync_collection(&api, base, &ankiweb, true).unwrap_err(),
        sync::full_sync(&api, base, &paths, &ankiweb, false, None).unwrap_err(),
        sync::full_sync(&api, base, &paths, &ankiweb, true, None).unwrap_err(),
        sync::status(&api, base, &ankiweb).unwrap_err(),
        sync::sync_media(&api, base, &ankiweb).unwrap_err(),
    ];
    for err in errors {
        assert!(
            sync::api_error(&err).is_none(),
            "reached the backend: {err}"
        );
        assert!(err.to_string().contains("AnkiWeb"), "{err}");
    }
    assert!(!paths.backups().exists(), "backed up before the guard");

    let elsewhere = SyncAuth {
        endpoint: Some("https://sync.ankiweb.net/".into()),
        ..ankiweb
    };
    let err = sync::sync_collection(&api, base, &elsewhere, true).unwrap_err();
    assert!(
        sync::api_error(&err).is_none(),
        "reached the backend: {err}"
    );
}
