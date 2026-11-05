// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

#![cfg(target_os = "macos")]

//! The AnkiConnect server over real HTTP, running the kind of workflow the
//! user's automations do.

use std::io::Read;
use std::io::Write;
use std::net::SocketAddr;
use std::net::TcpStream;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anki_shell::api::Api;
use anki_shell::profile::ProfilePaths;
use anki_shell::Shell;
use serde_json::json;
use serde_json::Value;

struct Response {
    status: u16,
    headers: String,
    body: Vec<u8>,
}

fn request(addr: SocketAddr, method: &str, headers: &[(&str, &str)], body: &[u8]) -> Response {
    let mut stream = TcpStream::connect(addr).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(60)))
        .unwrap();
    let mut head = format!(
        "{method} / HTTP/1.1\r\nHost: {addr}\r\nContent-Length: {}\r\nConnection: close\r\n",
        body.len()
    );
    for (name, value) in headers {
        head += &format!("{name}: {value}\r\n");
    }
    head += "\r\n";
    stream.write_all(head.as_bytes()).unwrap();
    stream.write_all(body).unwrap();
    let mut out = Vec::new();
    stream.read_to_end(&mut out).unwrap();
    let split = out
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .expect("no end of headers");
    let headers = String::from_utf8(out[..split].to_vec())
        .unwrap()
        .to_ascii_lowercase();
    let status = headers.split(' ').nth(1).unwrap().parse().unwrap();
    Response {
        status,
        headers,
        body: out[split + 4..].to_vec(),
    }
}

struct Client(SocketAddr);

impl Client {
    fn post(&self, body: &Value) -> Value {
        let resp = request(self.0, "POST", &[], body.to_string().as_bytes());
        assert_eq!(resp.status, 200, "{}", resp.headers);
        assert!(resp.headers.contains("content-type: application/json"));
        assert!(resp
            .headers
            .contains("access-control-allow-origin: http://localhost"));
        serde_json::from_slice(&resp.body).unwrap()
    }

    fn call(&self, action: &str, params: Value) -> Value {
        self.post(&json!({"action": action, "version": 6, "params": params}))
    }

    fn ok(&self, action: &str, params: Value) -> Value {
        let out = self.call(action, params);
        assert_eq!(out["error"], Value::Null, "{action}: {out}");
        out["result"].clone()
    }
}

#[test]
fn ankiconnect_workflow_over_http() {
    let dir = tempfile::tempdir().unwrap();
    let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), PathBuf::new());
    shell
        .open_profile(ProfilePaths::new(dir.path(), "User 1"))
        .unwrap();
    let addr =
        anki_shell::ankiconnect::serve(Arc::new(shell), "127.0.0.1:0".parse().unwrap()).unwrap();
    assert_ne!(addr.port(), 8765);
    let ac = Client(addr);

    assert_eq!(ac.ok("version", json!({})), 6);
    assert_eq!(ac.ok("deckNames", json!({})), json!(["Default"]));
    let deck_id = ac.ok("createDeck", json!({"deck": "Smoke::Test"}));
    assert!(deck_id.as_i64().unwrap() > 1);
    let models = ac.ok("modelNames", json!({}));
    assert!(models.as_array().unwrap().contains(&json!("Basic")));
    assert_eq!(
        ac.ok("modelFieldNames", json!({"modelName": "Basic"})),
        json!(["Front", "Back"])
    );

    let note = |front: &str| {
        json!({"deckName": "Smoke::Test", "modelName": "Basic",
               "fields": {"Front": front, "Back": "an answer"}, "tags": ["smoke"]})
    };
    let first = ac.ok("addNote", json!({"note": note("first question")}));
    let more = ac.ok(
        "addNotes",
        json!({"notes": [note("second question"), note("third question")]}),
    );
    assert_eq!(more.as_array().unwrap().len(), 2);
    let found = ac.ok("findNotes", json!({"query": "deck:Smoke::Test question"}));
    assert_eq!(found.as_array().unwrap().len(), 3);
    assert!(found.as_array().unwrap().contains(&first));

    let info = ac.ok("notesInfo", json!({"notes": [first]}));
    assert_eq!(info[0]["fields"]["Front"]["value"], "first question");
    assert_eq!(info[0]["modelName"], "Basic");
    assert_eq!(info[0]["tags"], json!(["smoke"]));
    ac.ok(
        "updateNoteFields",
        json!({"note": {"id": first, "fields": {"Back": "a better answer"}}}),
    );
    let info = ac.ok("notesInfo", json!({"notes": [first]}));
    assert_eq!(info[0]["fields"]["Back"]["value"], "a better answer");

    ac.ok("addTags", json!({"notes": [first], "tags": "extra"}));
    ac.ok("removeTags", json!({"notes": [first], "tags": "smoke"}));
    assert_eq!(
        ac.ok("getNoteTags", json!({"note": first})),
        json!(["extra"])
    );
    assert_eq!(ac.ok("getTags", json!({})), json!(["extra", "smoke"]));

    let cards = ac.ok("findCards", json!({"query": format!("nid:{first}")}));
    let cid = cards[0].clone();
    let card = &ac.ok("cardsInfo", json!({"cards": [cid]}))[0];
    assert!(card["question"]
        .as_str()
        .unwrap()
        .contains("first question"));
    assert!(card["answer"].as_str().unwrap().contains("a better answer"));
    assert_eq!(card["deckName"], "Smoke::Test");

    assert_eq!(ac.ok("suspend", json!({"cards": [cid]})), true);
    assert_eq!(
        ac.ok("areSuspended", json!({"cards": [cid]})),
        json!([true])
    );
    assert_eq!(ac.ok("suspended", json!({"card": cid})), true);
    ac.ok("unsuspend", json!({"cards": [cid]}));
    assert_eq!(ac.ok("suspended", json!({"card": cid})), false);

    let stats = ac.ok("getDeckStats", json!({"decks": ["Smoke::Test"]}));
    let stats = &stats[deck_id.to_string()];
    assert_eq!(stats["name"], "Test");
    assert_eq!(stats["new_count"], 3);
    assert_eq!(stats["total_in_deck"], 3);
    assert_eq!(
        ac.ok("getReviewsOfCards", json!({"cards": [cid]})),
        json!({cid.to_string(): []})
    );

    assert_eq!(
        ac.ok(
            "multi",
            json!({"actions": [
                {"action": "version", "version": 6},
                {"action": "getNoteTags", "version": 6, "params": {"note": first}},
            ]})
        ),
        json!([{"result": 6, "error": null}, {"result": ["extra"], "error": null}])
    );

    // "hello" in base64
    let name = ac.ok(
        "storeMediaFile",
        json!({"filename": "smoke.txt", "data": "aGVsbG8="}),
    );
    assert_eq!(name, "smoke.txt");
    assert_eq!(
        ac.ok("retrieveMediaFile", json!({"filename": "smoke.txt"})),
        "aGVsbG8="
    );

    ac.ok("deleteNotes", json!({"notes": found}));
    assert_eq!(
        ac.ok("findNotes", json!({"query": "deck:Smoke::Test"})),
        json!([])
    );

    assert_eq!(
        ac.call("guiBrowse", json!({"query": "deck:current"})),
        json!({"result": null, "error": "guiBrowse is not supported by the native shell yet"})
    );
    assert_eq!(
        ac.call("noSuchAction", json!({})),
        json!({"result": null, "error": "unsupported action"})
    );
}

#[test]
fn http_behaviour_matches_the_add_on() {
    let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), PathBuf::new());
    let addr =
        anki_shell::ankiconnect::serve(Arc::new(shell), "127.0.0.1:0".parse().unwrap()).unwrap();

    // a plain GET, as from a browser
    let resp = request(addr, "GET", &[], b"");
    assert_eq!(resp.status, 200);
    assert_eq!(
        serde_json::from_slice::<Value>(&resp.body).unwrap(),
        json!({"apiVersion": "AnkiConnect v.6"})
    );

    let resp = request(
        addr,
        "OPTIONS",
        &[
            ("Origin", "http://127.0.0.1:3000"),
            ("Access-Control-Request-Private-Network", "true"),
        ],
        b"",
    );
    assert_eq!(resp.status, 200);
    assert!(resp
        .headers
        .contains("access-control-allow-origin: http://127.0.0.1:3000"));
    assert!(resp
        .headers
        .contains("access-control-allow-private-network: true"));
    assert!(resp.body.is_empty());

    let body = br#"{"action": "version", "version": 6}"#;
    let resp = request(addr, "POST", &[("Origin", "https://example.com")], body);
    assert_eq!(resp.status, 403);
    assert!(resp.body.is_empty());

    let body = br#"{"action": "requestPermission", "version": 6}"#;
    let resp = request(addr, "POST", &[("Origin", "https://example.com")], body);
    assert_eq!(resp.status, 200);
    assert!(resp
        .headers
        .contains("access-control-allow-origin: https://example.com"));
    assert_eq!(
        serde_json::from_slice::<Value>(&resp.body).unwrap(),
        json!({"result": {"permission": "denied"}, "error": null})
    );

    let resp = request(addr, "POST", &[], br#"{"version": 6}"#);
    assert_eq!(
        serde_json::from_slice::<Value>(&resp.body).unwrap(),
        json!({"result": null, "error": "'action' is a required property"})
    );

    // the collection isn't open
    let resp = request(
        addr,
        "POST",
        &[],
        br#"{"action": "deckNames", "version": 6}"#,
    );
    assert_eq!(
        serde_json::from_slice::<Value>(&resp.body).unwrap(),
        json!({"result": null, "error": "collection is not available"})
    );
}

#[test]
fn a_busy_port_is_reported_not_fatal() {
    let taken = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let shell = Arc::new(Shell::new(
        Api::new(vec!["en".into()]).unwrap(),
        PathBuf::new(),
    ));
    let addr = taken.local_addr().unwrap();
    assert!(anki_shell::ankiconnect::serve(shell.clone(), addr).is_err());
    // spawn only logs
    anki_shell::ankiconnect::spawn(shell, addr.port(), Default::default());
}
