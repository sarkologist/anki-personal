// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Action parameters, with the errors Python gives for `method(**params)`,
//! since clients may show or match on them.

use serde_json::Map;
use serde_json::Value;

use super::Error;
use super::Result;

pub(crate) struct Params<'a> {
    map: &'a Map<String, Value>,
}

impl<'a> Params<'a> {
    pub fn parse(
        action: &str,
        params: &'a Value,
        required: &[&str],
        optional: &[&str],
    ) -> Result<Self> {
        let Value::Object(map) = params else {
            return Err(format!(
                "AnkiConnect.{action}() argument after ** must be a mapping, not {}",
                py_type(params)
            )
            .into());
        };
        // Python reports unexpected arguments before missing ones
        if let Some(key) = map
            .keys()
            .find(|k| !required.contains(&k.as_str()) && !optional.contains(&k.as_str()))
        {
            return Err(format!(
                "AnkiConnect.{action}() got an unexpected keyword argument '{key}'"
            )
            .into());
        }
        let missing: Vec<String> = required
            .iter()
            .filter(|k| !map.contains_key(**k))
            .map(|k| format!("'{k}'"))
            .collect();
        if !missing.is_empty() {
            let list = match missing.as_slice() {
                [one] => one.clone(),
                [a, b] => format!("{a} and {b}"),
                [rest @ .., last] => format!("{}, and {last}", rest.join(", ")),
                [] => unreachable!(),
            };
            return Err(format!(
                "AnkiConnect.{action}() missing {} required positional argument{}: {list}",
                missing.len(),
                if missing.len() == 1 { "" } else { "s" },
            )
            .into());
        }
        Ok(Params { map })
    }

    /// The value, treating null like an absent optional argument.
    pub fn get(&self, name: &str) -> Option<&'a Value> {
        self.map.get(name).filter(|v| !v.is_null())
    }

    pub fn str(&self, name: &str) -> Result<&'a str> {
        self.opt_str(name)?.ok_or_else(|| invalid(name, "a string"))
    }

    pub fn opt_str(&self, name: &str) -> Result<Option<&'a str>> {
        self.get(name)
            .map(|v| v.as_str().ok_or_else(|| invalid(name, "a string")))
            .transpose()
    }

    pub fn id(&self, name: &str) -> Result<i64> {
        self.get(name)
            .and_then(Value::as_i64)
            .ok_or_else(|| invalid(name, "an integer"))
    }

    pub fn ids(&self, name: &str) -> Result<Vec<i64>> {
        self.opt_ids(name)?
            .ok_or_else(|| invalid(name, "a list of integers"))
    }

    pub fn opt_ids(&self, name: &str) -> Result<Option<Vec<i64>>> {
        self.get(name).map(|v| ids(v, name)).transpose()
    }

    pub fn list(&self, name: &str) -> Result<&'a Vec<Value>> {
        self.get(name)
            .and_then(Value::as_array)
            .ok_or_else(|| invalid(name, "a list"))
    }

    pub fn object(&self, name: &str) -> Result<&'a Map<String, Value>> {
        self.get(name)
            .and_then(Value::as_object)
            .ok_or_else(|| invalid(name, "an object"))
    }

    /// Python truthiness, for flags that default to `default`.
    pub fn flag(&self, name: &str, default: bool) -> bool {
        self.map.get(name).map_or(default, truthy)
    }
}

pub(crate) fn ids(value: &Value, name: &str) -> Result<Vec<i64>> {
    value
        .as_array()
        .and_then(|items| items.iter().map(Value::as_i64).collect())
        .ok_or_else(|| invalid(name, "a list of integers"))
}

pub(crate) fn invalid(name: &str, expected: &str) -> Error {
    format!("invalid value for '{name}': expected {expected}").into()
}

pub(crate) fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64() != Some(0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// The name of a JSON value's Python type, for error messages.
pub(crate) fn py_type(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(n) if n.is_f64() => "float",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Python's `repr()` of a JSON value, as the add-on's messages embed it.
pub(crate) fn py_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => n.to_string(),
        Value::String(s) => py_str_repr(s),
        Value::Array(items) => format!(
            "[{}]",
            items.iter().map(py_repr).collect::<Vec<_>>().join(", ")
        ),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter()
                .map(|(k, v)| format!("{}: {}", py_str_repr(k), py_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
    }
}

pub(crate) fn py_str_repr(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32));
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod test {
    use serde_json::json;

    use super::*;

    #[test]
    fn missing_arguments_are_listed_like_python() {
        let err = |required: &[&str]| {
            Params::parse("f", &json!({}), required, &[])
                .err()
                .unwrap()
                .message
        };
        assert_eq!(
            err(&["a", "b", "c"]),
            "AnkiConnect.f() missing 3 required positional arguments: 'a', 'b', and 'c'"
        );
    }

    #[test]
    fn reprs_match_python() {
        assert_eq!(
            py_repr(&json!([null, true, 1, "it's", "a'b\"c", {"k": "\n"}])),
            r#"[None, True, 1, "it's", 'a\'b"c', {'k': '\n'}]"#
        );
    }
}
