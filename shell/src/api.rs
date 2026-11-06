// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! The in-process Rust backend, called by method name the way pylib's
//! `_backend_generated.py` does.

use std::collections::HashMap;
use std::sync::LazyLock;
use std::sync::RwLock;

use anki::backend::init_backend;
use anki::backend::Backend;
use anki_proto::backend::BackendError;
use anyhow::anyhow;
use anyhow::Result;
use prost::Message;
use serde_json::Value;

include!(concat!(env!("OUT_DIR"), "/methods.rs"));

/// name -> (service, method, where its output's `OpChanges` are)
static METHOD_INDEX: LazyLock<HashMap<&'static str, (u32, u32, i32)>> = LazyLock::new(|| {
    METHODS
        .iter()
        .map(|&(name, service, method, changes)| (name, (service, method, changes)))
        .collect()
});

static METHOD_NAMES: LazyLock<HashMap<(u32, u32), &'static str>> = LazyLock::new(|| {
    METHODS
        .iter()
        .map(|&(name, service, method, _)| ((service, method), name))
        .collect()
});

/// Gets (method name, encoded `OpChanges`) after each of the shell's own
/// calls that changed something.
pub type ChangesObserver = Box<dyn Fn(&str, &[u8]) + Send + Sync>;

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
    observer: RwLock<Option<ChangesObserver>>,
}

impl Api {
    pub fn new(langs: Vec<String>) -> Result<Self> {
        let init = anki_proto::backend::BackendInit {
            preferred_langs: langs,
            locale_folder_path: String::new(),
            server: false,
        };
        let backend = init_backend(&init.encode_to_vec()).map_err(|e| anyhow!(e))?;
        Ok(Api {
            backend,
            observer: RwLock::new(None),
        })
    }

    pub fn has_method(name: &str) -> bool {
        METHOD_INDEX.contains_key(name)
    }

    pub fn method_name(service: u32, method: u32) -> Option<&'static str> {
        METHOD_NAMES.get(&(service, method)).copied()
    }

    pub fn set_changes_observer(&self, observer: ChangesObserver) {
        *self.observer.write().unwrap() = Some(observer);
    }

    /// Call a method with raw protobuf bytes. `name` is snake_case.
    pub fn raw(&self, name: &str, input: &[u8]) -> Result<Vec<u8>, ApiError> {
        let Some(&(service, method, changes)) = METHOD_INDEX.get(name) else {
            return Err(ApiError {
                kind: -1,
                message: format!("unknown method {name}"),
            });
        };
        let out = self
            .backend
            .run_service_method(service, method, input)
            .map_err(|e| ApiError::from_bytes(&e))?;
        if let Some(op_changes) = op_changes(&out, changes) {
            if let Some(observer) = self.observer.read().unwrap().as_ref() {
                observer(name, op_changes);
            }
        }
        Ok(out)
    }

    /// A call by index for the plugin host, with rslib's encoded
    /// `BackendError` on failure. Its changes aren't observed: the host
    /// reports its operations itself, once each is done.
    pub fn raw_indexed(&self, service: u32, method: u32, input: &[u8]) -> Result<Vec<u8>, Vec<u8>> {
        self.backend.run_service_method(service, method, input)
    }

    /// pylib's `db_command`, in its JSON bytes.
    pub fn db_command_raw(&self, input: &[u8]) -> Result<Vec<u8>, Vec<u8>> {
        self.backend.run_db_command_bytes(input)
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

/// The encoded `OpChanges` in a method's output: the output itself (field
/// 0), or its length-delimited field `field`.
fn op_changes(out: &[u8], field: i32) -> Option<&[u8]> {
    if field < 0 {
        return None;
    }
    if field == 0 {
        return Some(out);
    }
    fn varint(buf: &[u8], pos: &mut usize) -> Option<u64> {
        let mut value = 0u64;
        for shift in (0..64).step_by(7) {
            let byte = *buf.get(*pos)?;
            *pos += 1;
            value |= u64::from(byte & 0x7f) << shift;
            if byte & 0x80 == 0 {
                return Some(value);
            }
        }
        None
    }
    let mut pos = 0;
    while pos < out.len() {
        let key = varint(out, &mut pos)?;
        let len = match key & 7 {
            0 => {
                varint(out, &mut pos)?;
                0
            }
            1 => 8,
            2 => varint(out, &mut pos)? as usize,
            5 => 4,
            _ => return None,
        };
        let start = pos;
        pos = pos.checked_add(len).filter(|&end| end <= out.len())?;
        if key & 7 == 2 && key >> 3 == field as u64 {
            return Some(&out[start..pos]);
        }
    }
    None
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
    fn op_changes_are_found_in_wrapped_outputs() {
        use anki_proto::collection::OpChanges;
        use anki_proto::collection::OpChangesWithCount;
        let changes = OpChanges {
            card: true,
            ..Default::default()
        };
        let out = OpChangesWithCount {
            count: 300,
            changes: Some(changes.clone()),
        }
        .encode_to_vec();
        let (_, _, field) = METHOD_INDEX["remove_notes"];
        assert_eq!(
            OpChanges::decode(op_changes(&out, field).unwrap()).unwrap(),
            changes
        );
        let (_, _, field) = METHOD_INDEX["answer_card"];
        assert_eq!(field, 0);
        let (_, _, field) = METHOD_INDEX["get_card"];
        assert_eq!(op_changes(&changes.encode_to_vec(), field), None);
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
