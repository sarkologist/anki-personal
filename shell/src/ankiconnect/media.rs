// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Media actions, and the media a note being added or updated brings along.

use std::path::PathBuf;

use data_encoding::BASE64;
use serde_json::json;
use serde_json::Map;
use serde_json::Value;
use unicode_normalization::UnicodeNormalization;

use super::params::invalid;
use super::params::truthy;
use super::params::Params;
use super::Ctx;
use super::Result;

fn media_dir(ctx: &Ctx) -> Result<PathBuf> {
    ctx.col()?;
    ctx.shell
        .media_folder()
        .ok_or_else(|| "media is not available".into())
}

pub(crate) fn store_media_file(ctx: &Ctx, p: &Params) -> Result<Value> {
    let stored = store(
        ctx,
        p.str("filename")?,
        p.opt_str("data")?,
        p.opt_str("path")?,
        p.opt_str("url")?,
        p.opt_str("skipHash")?,
        p.flag("deleteExisting", true),
    )?;
    Ok(json!(stored))
}

/// The stored file's name, or None if `skip_hash` matched.
fn store(
    ctx: &Ctx,
    filename: &str,
    data: Option<&str>,
    path: Option<&str>,
    url: Option<&str>,
    skip_hash: Option<&str>,
    delete_existing: bool,
) -> Result<Option<String>> {
    // Python truthiness: empty strings count as missing
    let data = data.filter(|s| !s.is_empty());
    let path = path.filter(|s| !s.is_empty());
    let url = url.filter(|s| !s.is_empty());
    let bytes = if let Some(data) = data {
        decode_base64(data)?
    } else if let Some(path) = path {
        std::fs::read(path)?
    } else if url.is_some() {
        return Err("storeMediaFile with a url is not supported by the native shell yet".into());
    } else {
        return Err("You must provide a \"data\", \"path\", or \"url\" field.".into());
    };
    if skip_hash == Some(md5_hex(&bytes).as_str()) {
        return Ok(None);
    }
    if delete_existing {
        delete_media_file(ctx, filename)?;
    }
    let name: anki_proto::generic::String = ctx.call(
        "add_media_file",
        &anki_proto::media::AddMediaFileRequest {
            desired_name: filename.into(),
            data: bytes,
        },
    )?;
    Ok(Some(name.val))
}

/// Python's lenient `b64decode()`, which skips characters outside the
/// alphabet (such as line breaks).
fn decode_base64(data: &str) -> Result<Vec<u8>> {
    let clean: String = data
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, '+' | '/' | '='))
        .collect();
    BASE64
        .decode(clean.as_bytes())
        .map_err(|e| format!("invalid base64 data: {e}").into())
}

pub(crate) fn retrieve_media_file(ctx: &Ctx, filename: &str) -> Result<Value> {
    let name = filename.rsplit('/').next().unwrap_or_default();
    let name: String = name
        .nfc()
        .filter(|c| !"[]><:\"/?*^\\|\0\r\n".contains(*c))
        .collect();
    let path = media_dir(ctx)?.join(name);
    if !path.exists() {
        return Ok(json!(false));
    }
    Ok(json!(BASE64.encode(&std::fs::read(path)?)))
}

/// Python's `glob()` in the media folder: names only, and wildcards don't
/// match hidden files.
pub(crate) fn media_file_names(ctx: &Ctx, pattern: &str) -> Result<Value> {
    let glob = globset::GlobBuilder::new(pattern)
        .literal_separator(true)
        .build()
        .map_err(|e| e.to_string())?
        .compile_matcher();
    let mut names: Vec<String> = std::fs::read_dir(media_dir(ctx)?)?
        .flatten()
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| !name.starts_with('.') || pattern.starts_with('.'))
        .filter(|name| glob.is_match(name))
        .collect();
    names.sort_unstable();
    Ok(json!(names))
}

pub(crate) fn media_dir_path(ctx: &Ctx) -> Result<Value> {
    Ok(json!(std::path::absolute(media_dir(ctx)?)?))
}

pub(crate) fn delete_media_file(ctx: &Ctx, filename: &str) -> Result<()> {
    // rslib joins the name onto the media folder unchecked (as does the
    // add-on), so "../collection.anki2" would trash the collection
    if filename.is_empty() || filename == ".." || filename.contains(['/', '\\', '\0']) {
        return Err(format!("invalid media filename: {filename}").into());
    }
    ctx.run(
        "trash_media_files",
        &anki_proto::media::TrashMediaFilesRequest {
            fnames: vec![filename.into()],
        },
    )
}

/// The add-on's `addMediaFromNote`: store the note's `audio`, `video` and
/// `picture` files and reference them in the listed fields. Failures are
/// written into those fields rather than failing the note.
pub(crate) fn add_media_from_note(
    ctx: &Ctx,
    fields: &mut [String],
    names: &[String],
    note: &Map<String, Value>,
) {
    for (key, picture) in [("audio", false), ("video", false), ("picture", true)] {
        let items = match note.get(key) {
            None | Some(Value::Null) => continue,
            Some(Value::Array(items)) => items.iter().collect(),
            Some(item) => vec![item],
        };
        for item in items.into_iter().filter(|i| !i.is_null()) {
            let targets: Vec<usize> = item
                .get("fields")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(|f| f.as_str())
                .filter_map(|f| names.iter().position(|n| n == f))
                .collect();
            let text = match store_note_media(ctx, item) {
                Ok(None) => continue,
                Ok(Some(name)) if picture => format!("<img src=\"{name}\">"),
                Ok(Some(name)) => format!("[sound:{name}]"),
                Err(err) => err
                    .message
                    .replace('&', "&amp;")
                    .replace('<', "&lt;")
                    .replace('>', "&gt;"),
            };
            for idx in targets {
                fields[idx] += &text;
            }
        }
    }
}

fn store_note_media(ctx: &Ctx, item: &Value) -> Result<Option<String>> {
    let text = |key: &str| -> Result<Option<&str>> {
        match item.get(key) {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) => Ok(Some(s)),
            Some(_) => Err(invalid(key, "a string")),
        }
    };
    let filename = text("filename")?.ok_or("'filename'")?;
    store(
        ctx,
        filename,
        text("data")?,
        text("path")?,
        text("url")?,
        text("skipHash")?,
        // unlike storeMediaFile, existing files are kept unless asked
        item.get("deleteExisting").is_some_and(truthy),
    )
}

/// RFC 1321 MD5, for `skipHash`.
fn md5_hex(data: &[u8]) -> String {
    const S: [u32; 64] = [
        7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 5, 9, 14, 20, 5, 9, 14, 20, 5,
        9, 14, 20, 5, 9, 14, 20, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 6, 10,
        15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21,
    ];
    let k: Vec<u32> = (0..64)
        .map(|i| ((i as f64 + 1.0).sin().abs() * 4294967296.0) as u32)
        .collect();
    let mut message = data.to_vec();
    message.push(0x80);
    while message.len() % 64 != 56 {
        message.push(0);
    }
    message.extend_from_slice(&((data.len() as u64).wrapping_mul(8)).to_le_bytes());
    let mut state: [u32; 4] = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476];
    for chunk in message.chunks(64) {
        let m: Vec<u32> = chunk
            .chunks(4)
            .map(|w| u32::from_le_bytes([w[0], w[1], w[2], w[3]]))
            .collect();
        let [mut a, mut b, mut c, mut d] = state;
        for i in 0..64 {
            let (f, g) = match i / 16 {
                0 => ((b & c) | (!b & d), i),
                1 => ((d & b) | (!d & c), (5 * i + 1) % 16),
                2 => (b ^ c ^ d, (3 * i + 5) % 16),
                _ => (c ^ (b | !d), (7 * i) % 16),
            };
            let f = f.wrapping_add(a).wrapping_add(k[i]).wrapping_add(m[g]);
            a = d;
            d = c;
            c = b;
            b = b.wrapping_add(f.rotate_left(S[i]));
        }
        for (s, v) in state.iter_mut().zip([a, b, c, d]) {
            *s = s.wrapping_add(v);
        }
    }
    state
        .iter()
        .flat_map(|s| s.to_le_bytes())
        .map(|b| format!("{b:02x}"))
        .collect()
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn md5_matches_rfc_1321() {
        assert_eq!(md5_hex(b""), "d41d8cd98f00b204e9800998ecf8427e");
        assert_eq!(md5_hex(b"abc"), "900150983cd24fb0d6963f7d28e17f72");
        assert_eq!(
            md5_hex(
                b"12345678901234567890123456789012345678901234567890123456789012345678901234567890"
            ),
            "57edf4a22be3c955ac49da2e2107b67a"
        );
    }

    #[test]
    fn base64_is_lenient_like_python() {
        assert_eq!(decode_base64("aGVs\nbG8=").unwrap(), b"hello");
        assert!(decode_base64("aGVsbG8").is_err());
    }
}
