// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The add-on's `web.py`: CORS, preflight and request validation. Any
//! method and path is accepted, as there.

use axum::http::header;
use axum::http::HeaderMap;
use axum::http::HeaderValue;
use axum::http::Method;
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::response::Response;
use serde_json::json;
use serde_json::Value;

use super::handle_with;
use super::params::py_repr;
use super::Config;
use super::API_VERSION;
use crate::Shell;

pub(crate) struct WebResponse {
    pub status: StatusCode,
    pub cors_origin: String,
    pub private_network: bool,
    /// None for the 403 reply, which has no content type.
    pub body: Option<Vec<u8>>,
}

impl IntoResponse for WebResponse {
    fn into_response(self) -> Response {
        let mut resp = self.body.clone().unwrap_or_default().into_response();
        *resp.status_mut() = self.status;
        let headers = resp.headers_mut();
        headers.remove(header::CONTENT_TYPE);
        if self.body.is_some() {
            headers.insert(
                header::CONTENT_TYPE,
                HeaderValue::from_static("application/json"),
            );
        }
        if let Ok(origin) = HeaderValue::from_str(&self.cors_origin) {
            headers.insert(header::ACCESS_CONTROL_ALLOW_ORIGIN, origin);
        }
        headers.insert(
            header::ACCESS_CONTROL_ALLOW_HEADERS,
            HeaderValue::from_static("*"),
        );
        if self.private_network {
            headers.insert(
                "access-control-allow-private-network",
                HeaderValue::from_static("true"),
            );
        }
        resp
    }
}

pub(crate) fn respond(
    shell: &Shell,
    config: &Config,
    method: &Method,
    headers: &HeaderMap,
    body: &[u8],
) -> WebResponse {
    let origin = headers
        .get(header::ORIGIN)
        .map(|o| String::from_utf8_lossy(o.as_bytes()).into_owned());
    let (allowed, mut cors_origin) = allow_origin(config, origin.as_deref());
    let reply = |cors_origin: String, body: &Value| WebResponse {
        status: StatusCode::OK,
        cors_origin,
        private_network: false,
        body: Some(body.to_string().into_bytes()),
    };

    if method == Method::OPTIONS {
        return WebResponse {
            status: StatusCode::OK,
            cors_origin,
            private_network: headers
                .get("access-control-request-private-network")
                .is_some_and(|v| v == "true"),
            body: Some(vec![]),
        };
    }

    let mut request = match parse(body) {
        Ok(request) => request,
        Err(_) if allowed && body.is_empty() => {
            let version = format!("AnkiConnect v.{API_VERSION}");
            return reply(cors_origin, &json!({"apiVersion": version}));
        }
        Err(err) if allowed => return reply(cors_origin, &json!({"result": null, "error": err})),
        // turned into the 403 below
        Err(_) => json!({}),
    };
    let permission = request["action"] == "requestPermission";
    if !allowed && !permission {
        return WebResponse {
            status: StatusCode::FORBIDDEN,
            cors_origin,
            private_network: false,
            body: None,
        };
    }
    if permission {
        let origin = origin.unwrap_or_default();
        if !request["params"].is_object() {
            request["params"] = json!({});
        }
        request["params"]["allowed"] = json!(allowed);
        request["params"]["origin"] = json!(origin);
        if !allowed {
            cors_origin = origin;
        }
    }
    reply(cors_origin, &handle_with(shell, config, &request))
}

/// `allowOrigin`: whether the request may proceed, and the CORS origin to
/// reply with.
fn allow_origin(config: &Config, origin: Option<&str>) -> (bool, String) {
    let list = &config.cors_origins;
    if list.iter().any(|o| o == "*") {
        return (true, "*".into());
    }
    let Some(origin) = origin else {
        return (true, "http://localhost".into());
    };
    let local_ok = list.iter().any(|o| o == "http://localhost")
        && (origin == "http://127.0.0.1"
            || origin == "https://127.0.0.1"
            || origin.starts_with("http://127.0.0.1:")
            || origin.starts_with("chrome-extension://")
            || origin.starts_with("moz-extension://")
            || origin.starts_with("safari-web-extension://"));
    if local_ok || list.iter().any(|o| o == origin) {
        (true, origin.into())
    } else {
        (false, "http://localhost".into())
    }
}

/// `json.loads` plus the add-on's request schema. Messages follow
/// jsonschema's headline for the failing rule.
fn parse(body: &[u8]) -> Result<Value, String> {
    let request: Value = serde_json::from_slice(body).map_err(|e| e.to_string())?;
    let Some(map) = request.as_object() else {
        return Err(format!("{} is not of type 'object'", py_repr(&request)));
    };
    match map.get("action") {
        None => return Err("'action' is a required property".into()),
        Some(Value::String(action)) if action.is_empty() => {
            return Err("'' should be non-empty".into())
        }
        Some(Value::String(_)) => {}
        Some(other) => return Err(format!("{} is not of type 'string'", py_repr(other))),
    }
    if let Some(version) = map.get("version") {
        let integer = version.is_i64()
            || version.is_u64()
            || version.as_f64().is_some_and(|v| v.fract() == 0.0);
        if !integer {
            return Err(format!("{} is not of type 'integer'", py_repr(version)));
        }
    }
    if let Some(params) = map.get("params") {
        if !params.is_object() {
            return Err(format!("{} is not of type 'object'", py_repr(params)));
        }
    }
    Ok(request)
}

#[cfg(test)]
mod test {
    use super::*;

    fn config(origins: &[&str]) -> Config {
        Config {
            cors_origins: origins.iter().map(|o| o.to_string()).collect(),
            ..Config::default()
        }
    }

    #[test]
    fn origins_follow_the_add_on() {
        let default = Config::default();
        let check = |config: &Config, origin| allow_origin(config, origin);
        assert_eq!(check(&default, None), (true, "http://localhost".into()));
        assert_eq!(
            check(&default, Some("http://localhost")),
            (true, "http://localhost".into())
        );
        for origin in [
            "http://127.0.0.1:8080",
            "https://127.0.0.1",
            "chrome-extension://abc",
            "moz-extension://abc",
            "safari-web-extension://abc",
        ] {
            assert_eq!(check(&default, Some(origin)), (true, origin.into()));
        }
        assert_eq!(
            check(&default, Some("https://evil.example")),
            (false, "http://localhost".into())
        );
        assert_eq!(
            check(&config(&["*"]), Some("https://a.example")),
            (true, "*".into())
        );
        assert_eq!(
            check(&config(&["https://a.example"]), Some("https://a.example")),
            (true, "https://a.example".into())
        );
        // without localhost listed, local origins aren't special
        assert!(!check(&config(&["https://a.example"]), Some("http://127.0.0.1")).0);
    }

    #[test]
    fn schema_errors_name_the_rule() {
        assert_eq!(parse(b"[]").unwrap_err(), "[] is not of type 'object'");
        assert_eq!(
            parse(br#"{"action": ""}"#).unwrap_err(),
            "'' should be non-empty"
        );
        assert_eq!(
            parse(br#"{"action": 1}"#).unwrap_err(),
            "1 is not of type 'string'"
        );
        assert_eq!(
            parse(br#"{"action": "a", "version": "6"}"#).unwrap_err(),
            "'6' is not of type 'integer'"
        );
        assert_eq!(
            parse(br#"{"action": "a", "params": []}"#).unwrap_err(),
            "[] is not of type 'object'"
        );
        assert!(parse(br#"{"action": "a", "version": 6.0, "params": {}}"#).is_ok());
    }
}
