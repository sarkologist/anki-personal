// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The in-process Rust backend, called by method name the way pylib's
//! `_backend_generated.py` does.

use std::collections::HashMap;
use std::sync::LazyLock;

use anki::backend::init_backend;
use anki::backend::Backend;
use anki_proto::backend::BackendError;
use anyhow::anyhow;
use anyhow::Result;
use prost::Message;
use serde_json::Value;

include!(concat!(env!("OUT_DIR"), "/methods.rs"));

static METHOD_INDEX: LazyLock<HashMap<&'static str, (u32, u32)>> = LazyLock::new(|| {
    METHODS
        .iter()
        .map(|&(name, service, method)| (name, (service, method)))
        .collect()
});

/// A backend error, as rslib reports it.
#[derive(Debug, Clone)]
pub struct ApiError {
    pub kind: i32,
    pub message: String,
}

impl ApiError {
    fn from_bytes(bytes: &[u8]) -> Self {
        match BackendError::decode(bytes) {
            Ok(err) => ApiError {
                kind: err.kind,
                message: err.message,
            },
            Err(_) => ApiError {
                kind: -1,
                message: String::from_utf8_lossy(bytes).into_owned(),
            },
        }
    }

    pub fn is_db_locked(&self) -> bool {
        use anki_proto::backend::backend_error::Kind;
        // rslib's DbErrorKind::Locked message; the kind alone covers all db errors
        self.kind == Kind::DbError as i32 && self.message.contains("already open")
    }
}

impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ApiError {}

pub struct Api {
    backend: Backend,
}

impl Api {
    pub fn new(langs: Vec<String>) -> Result<Self> {
        let init = anki_proto::backend::BackendInit {
            preferred_langs: langs,
            locale_folder_path: String::new(),
            server: false,
        };
        let backend = init_backend(&init.encode_to_vec()).map_err(|e| anyhow!(e))?;
        Ok(Api { backend })
    }

    pub fn has_method(name: &str) -> bool {
        METHOD_INDEX.contains_key(name)
    }

    /// Call a method with raw protobuf bytes. `name` is snake_case.
    pub fn raw(&self, name: &str, input: &[u8]) -> Result<Vec<u8>, ApiError> {
        let Some(&(service, method)) = METHOD_INDEX.get(name) else {
            return Err(ApiError {
                kind: -1,
                message: format!("unknown method {name}"),
            });
        };
        self.backend
            .run_service_method(service, method, input)
            .map_err(|e| ApiError::from_bytes(&e))
    }

    pub fn call<T: Message + Default>(
        &self,
        name: &str,
        input: &impl Message,
    ) -> Result<T, ApiError> {
        let out = self.raw(name, &input.encode_to_vec())?;
        T::decode(out.as_slice()).map_err(|e| ApiError {
            kind: -1,
            message: format!("{name}: {e}"),
        })
    }

    /// pylib's `db_command`: `{"kind": "query", "sql": ..., "args": [...],
    /// "first_row_only": false}` and friends.
    pub fn db_command(&self, request: &Value) -> Result<Value, ApiError> {
        let out = self
            .backend
            .run_db_command_bytes(request.to_string().as_bytes())
            .map_err(|e| ApiError::from_bytes(&e))?;
        serde_json::from_slice(&out).map_err(|e| ApiError {
            kind: -1,
            message: e.to_string(),
        })
    }

    /// Rows of a read-only SQL query.
    pub fn query(&self, sql: &str, args: &[Value]) -> Result<Vec<Vec<Value>>, ApiError> {
        let out = self.db_command(&serde_json::json!({
            "kind": "query",
            "sql": sql,
            "args": args,
            "first_row_only": false,
        }))?;
        Ok(serde_json::from_value(out).unwrap_or_default())
    }
}

pub fn camel_to_snake(name: &str) -> String {
    let mut out = String::with_capacity(name.len() + 4);
    for c in name.chars() {
        if c.is_ascii_uppercase() {
            out.push('_');
            out.push(c.to_ascii_lowercase());
        } else {
            out.push(c);
        }
    }
    out
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn method_table_matches_pylib_names() {
        assert!(Api::has_method("open_collection"));
        assert!(Api::has_method("get_scheduling_states_with_context"));
        assert!(!Api::has_method("openCollection"));
    }

    #[test]
    fn camel_case_endpoints_map_to_snake_case() {
        assert_eq!(camel_to_snake("i18nResources"), "i18n_resources");
        assert_eq!(
            camel_to_snake("getSchedulingStatesWithContext"),
            "get_scheduling_states_with_context"
        );
    }
}
