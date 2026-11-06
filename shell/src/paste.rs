// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Pasting and dropping into the editor, as `editor.py` does it:
//! `_processMime` turns pasteboard contents into HTML, `_pastePreFilter`
//! cleans it and pulls remote/inlined images into the media folder, and
//! `EditorWebView` tells copies from a field apart from external ones.
//! AppKit access lives in `crate::pasteboard`; media goes through [`Media`],
//! so the logic here is testable without either.

use std::cell::OnceCell;
use std::cell::RefCell;
use std::fs;
use std::path::Path;
use std::path::PathBuf;
use std::sync::LazyLock;
use std::sync::Mutex;
use std::time::Duration;
use std::time::Instant;

use anki::prelude::I18n;
use anki_proto::config::config_key::Bool as BoolKey;
use anki_proto::config::GetConfigBoolRequest;
use anki_proto::generic;
use anki_proto::i18n::I18nResourcesRequest;
use anki_proto::media::AddMediaFileRequest;
use anyhow::anyhow;
use anyhow::Context;
use anyhow::Result;
use data_encoding::BASE64;
use data_encoding::HEXLOWER;
use dom_query::Document;
use regex::Captures;
use regex::Regex;
use sha1::Digest;
use sha1::Sha1;

use crate::api::Api;
use crate::pages::ORIGIN;
use crate::pasteboard;
use crate::pasteboard::Contents;
use crate::pasteboard::Image;
use crate::Shell;

/// `editor.pics`, lowercased (Qt lists both cases and lowercases the name).
pub const PICS: &[&str] = &["jpg", "jpeg", "png", "gif", "svg", "webp", "ico", "avif"];

/// `editor.audio`, lowercased.
pub const AUDIO: &[&str] = &[
    "3gp", "aac", "avi", "flac", "flv", "m4a", "mkv", "mov", "mp3", "mp4", "mpeg", "mpg", "oga",
    "ogg", "ogv", "ogx", "opus", "spx", "swf", "wav", "webm",
];

/// `Editor.removeTags`, dropped from external pastes.
const REMOVE_TAGS: &str = "script, iframe, object, style";

/// `HttpClient.timeout` as `_retrieveURL` sets it.
const DOWNLOAD_TIMEOUT: Duration = Duration::from_secs(30);

/// Where pasted media goes.
pub trait Media {
    /// `col.media.write_data`: add `data` as `desired_name`, returning the
    /// name it was stored under (the backend renames on a clash).
    fn write_data(&self, desired_name: &str, data: &[u8]) -> Result<String>;
    /// `col.media.add_file`: add a file under its own name.
    fn add_file(&self, path: &Path) -> Result<String>;
    /// The body and content type at `url` (`file://` or http(s)).
    fn fetch(&self, url: &str) -> Result<(Vec<u8>, Option<String>)>;
}

/// [`Media`] for the open collection. Failures are kept as Qt's warning
/// texts, for the caller to show (`showWarning` in `editor.py`).
pub struct ShellMedia<'a> {
    shell: &'a Shell,
    warnings: RefCell<Vec<String>>,
    tr: OnceCell<I18n>,
}

impl<'a> ShellMedia<'a> {
    pub fn new(shell: &'a Shell) -> Self {
        ShellMedia {
            shell,
            warnings: RefCell::default(),
            tr: OnceCell::new(),
        }
    }

    /// The warnings so far, oldest first.
    pub fn take_warnings(&self) -> Vec<String> {
        self.warnings.take()
    }

    fn tr(&self) -> &I18n {
        self.tr.get_or_init(|| backend_tr(&self.shell.api))
    }

    fn warn(&self, message: String) -> anyhow::Error {
        self.warnings.borrow_mut().push(message.clone());
        anyhow!(message)
    }

    fn opening_failed(&self, what: impl std::fmt::Display) -> anyhow::Error {
        let message = self
            .tr()
            .editing_an_error_occurred_while_opening(what.to_string())
            .into_owned();
        self.warn(message)
    }
}

impl Media for ShellMedia<'_> {
    fn write_data(&self, desired_name: &str, data: &[u8]) -> Result<String> {
        let name: generic::String = self
            .shell
            .api
            .call(
                "add_media_file",
                &AddMediaFileRequest {
                    desired_name: desired_name.to_string(),
                    data: data.to_vec(),
                },
            )
            .map_err(|err| self.warn(err.message))?;
        Ok(name.val)
    }

    fn add_file(&self, path: &Path) -> Result<String> {
        let data = fs::read(path)
            .map_err(|err| self.opening_failed(format!("{}: {err}", path.display())))?;
        let name = path
            .file_name()
            .with_context(|| format!("{} has no file name", path.display()))?;
        self.write_data(&name.to_string_lossy(), &data)
    }

    fn fetch(&self, url: &str) -> Result<(Vec<u8>, Option<String>)> {
        if is_file_url(url) {
            let path = local_path(url);
            return fs::read(&path)
                .map(|data| (data, None))
                .map_err(|err| self.opening_failed(format!("{}: {err}", path.display())));
        }
        http_get(url).map_err(|err| match err {
            HttpError::Status(code) => {
                let message = self.tr().qt_misc_unexpected_response_code(code);
                self.warn(message.into_owned())
            }
            HttpError::Request(err) => self.opening_failed(err),
        })
    }
}

enum HttpError {
    Status(u16),
    Request(String),
}

/// A GET as `HttpClient` makes it. Runs on a thread and runtime of its own,
/// so it can be called from anywhere, async contexts included.
fn http_get(url: &str) -> Result<(Vec<u8>, Option<String>), HttpError> {
    let request = |err: reqwest::Error| HttpError::Request(format!("{:#}", anyhow!(err)));
    let get = || {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .map_err(|err| HttpError::Request(err.to_string()))?;
        runtime.block_on(async {
            let client = reqwest::Client::builder()
                .connect_timeout(DOWNLOAD_TIMEOUT)
                .read_timeout(DOWNLOAD_TIMEOUT)
                .user_agent(format!("Anki {}", anki::version::version()))
                // httpclient.py's escape hatch for intercepting proxies
                .danger_accept_invalid_certs(std::env::var_os("ANKI_NOVERIFYSSL").is_some())
                .build()
                .map_err(request)?;
            let response = client.get(url).send().await.map_err(request)?;
            if response.status() != reqwest::StatusCode::OK {
                return Err(HttpError::Status(response.status().as_u16()));
            }
            let content_type = response
                .headers()
                .get(reqwest::header::CONTENT_TYPE)
                .and_then(|value| value.to_str().ok())
                .map(str::to_string);
            let body = response.bytes().await.map_err(request)?;
            Ok((body.to_vec(), content_type))
        })
    };
    std::thread::scope(|scope| scope.spawn(get).join())
        .unwrap_or_else(|_| Err(HttpError::Request("download thread panicked".into())))
}

/// Translations in the backend's languages.
fn backend_tr(api: &Api) -> I18n {
    let langs = api
        .call::<generic::Json>(
            "i18n_resources",
            &I18nResourcesRequest {
                modules: vec!["editing".into()],
            },
        )
        .ok()
        .and_then(|json| serde_json::from_slice::<serde_json::Value>(&json.json).ok())
        .and_then(|json| serde_json::from_value::<Vec<String>>(json["langs"].clone()).ok());
    match langs {
        Some(langs) => I18n::new(&langs),
        None => I18n::template_only(),
    }
}

/// `_pastePreFilter`: for external pastes, drop active content, turn `<p>`
/// into `<div>` and bring images into the media folder; for field copies,
/// make media references relative again.
pub fn pre_filter(html: &str, internal: bool, media: &dyn Media) -> String {
    // parsing would escape a stray '<' in pasted text
    if !html.contains('>') {
        return html.to_string();
    }
    let doc = Document::fragment(html);
    if !internal {
        doc.select(REMOVE_TAGS).remove();
        doc.select("p").rename("div");
    }
    for img in doc.select("img").nodes() {
        // mnemosyne drops the src of missing media
        let Some(src) = img.attr("src") else {
            continue;
        };
        let new_src = if let Some(name) = own_media_name(&src, internal) {
            Some(name.to_string())
        } else if internal {
            None
        } else if is_url(&src) {
            retrieve_url(&src, media)
        } else if src.starts_with("data:image/") {
            Some(inlined_image_to_filename(&src, media))
        } else {
            None
        };
        if let Some(new_src) = new_src {
            img.set_attr("src", &new_src);
        }
    }
    // the fragment's nodes sit in an <html> element
    let root = doc.root();
    match root.first_element_child() {
        Some(html) if html.has_name("html") => html.inner_html().to_string(),
        _ => root.inner_html().to_string(),
    }
}

/// The media file an image src from Anki's own pages names: the shell's
/// origin (WebKit copies absolute URLs), or in field copies, mediasrv.
fn own_media_name(src: &str, internal: bool) -> Option<&str> {
    static MEDIASRV: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"^http://127\.0\.0\.1:\d+/(.*)$").unwrap());
    if let Some(name) = src.strip_prefix(ORIGIN) {
        // bundled assets aren't media
        return (!name.starts_with("_anki/")).then_some(name);
    }
    if !internal {
        return None;
    }
    MEDIASRV
        .captures(src)
        .and_then(|caps| caps.get(1))
        .map(|name| name.as_str())
}

/// `_processMime`: HTML for `contents`, and whether it counts as internal
/// (anything but HTML, which is filtered as external).
pub fn mime_to_html(
    contents: &Contents,
    extended: bool,
    media: &dyn Media,
) -> Option<(String, bool)> {
    if let Some(html) = contents.html.as_ref().filter(|html| !html.is_empty()) {
        return Some((html.clone(), false));
    }
    let urls = || process_urls(&contents.urls, &[], media);
    let image = || {
        contents
            .image
            .as_ref()
            .and_then(|i| process_image(i, media))
    };
    let text = || {
        contents
            .text
            .as_ref()
            .map(|text| process_text(text, extended, media))
    };
    // favour a url if it's a local file
    let local = contents
        .urls
        .first()
        .is_some_and(|url| url.starts_with("file://"));
    let order: [&dyn Fn() -> Option<String>; 3] = if local {
        [&urls, &image, &text]
    } else {
        [&image, &urls, &text]
    };
    order
        .iter()
        .find_map(|process| process().filter(|html| !html.is_empty()))
        .map(|html| (html, true))
}

/// `_processUrls`: a link (or media tag) per url; None without urls.
/// `allowed_suffixes` as for [`url_to_link`].
fn process_urls(urls: &[String], allowed_suffixes: &[&str], media: &dyn Media) -> Option<String> {
    if urls.is_empty() {
        return None;
    }
    // chrome likes to give us the URL twice with a \n
    Some(
        urls.iter()
            .filter_map(|url| url.lines().next())
            .map(|url| url_to_link(url, allowed_suffixes, media))
            .collect(),
    )
}

/// `_processImage`: store image data as `paste-<sha1>.<ext>`.
fn process_image(image: &Image, media: &dyn Media) -> Option<String> {
    add_pasted_image(&image.data, image.ext, media).map(|name| fname_to_link(&name))
}

/// `_processText`: escaped text with whitespace kept and lines joined by
/// `<br>`. `extended` imports media urls and inlined images and links
/// other urls.
pub fn process_text(text: &str, extended: bool, media: &dyn Media) -> String {
    static TOKEN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\S+").unwrap());
    let token_html = |token: &str| {
        if extended && token.starts_with("data:image/") {
            inlined_image_to_link(token, media)
        } else if extended && is_url(token) {
            url_to_link(token, &[], media)
        } else {
            plain_text_html(token)
        }
    };
    text.split('\n')
        .map(|line| {
            let mut out = String::new();
            let mut last = 0;
            for token in TOKEN.find_iter(line) {
                out.push_str(&plain_text_html(&line[last..token.start()]));
                out.push_str(&token_html(token.as_str()));
                last = token.end();
            }
            out.push_str(&plain_text_html(&line[last..]));
            out
        })
        .collect::<Vec<_>>()
        .join("<br>")
}

/// Escaped, with tabs as four spaces and all but the last of a run of
/// spaces non-breaking.
fn plain_text_html(text: &str) -> String {
    static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::new(" ( +)").unwrap());
    let text = html_escape(text, true).replace('\t', "    ");
    SPACES
        .replace_all(&text, |caps: &Captures| {
            format!("{} ", "&nbsp;".repeat(caps[1].len()))
        })
        .into_owned()
}

/// `urlToLink`: import `url` if it ends in one of `allowed_suffixes` (all
/// pictures and sounds when empty) and link to it, else an `<a>`.
pub fn url_to_link(url: &str, allowed_suffixes: &[&str], media: &dyn Media) -> String {
    let all = [PICS, AUDIO].concat();
    let allowed = if allowed_suffixes.is_empty() {
        &all
    } else {
        allowed_suffixes
    };
    match url_to_file(url, allowed, media) {
        Some(name) => fname_to_link(&name),
        None => format!(
            r#"<a href="{url}">{}</a>"#,
            html_escape(&unquote(url), true)
        ),
    }
}

/// `urlToFile`: the stored name, if `url` has an allowed suffix and could
/// be fetched.
fn url_to_file(url: &str, allowed_suffixes: &[&str], media: &dyn Media) -> Option<String> {
    let lower = url.to_lowercase();
    allowed_suffixes
        .iter()
        .any(|suffix| lower.ends_with(&format!(".{suffix}")))
        .then(|| retrieve_url(url, media))
        .flatten()
}

/// `_retrieveURL`: fetch `url` into the media folder and return its name.
/// The name is the url's last segment (the file's own name for local
/// files), with an extension from the content type if it has none.
fn retrieve_url(url: &str, media: &dyn Media) -> Option<String> {
    let (data, content_type) = media.fetch(url).ok()?;
    let mut name = url_file_name(url);
    if let Some(content_type) = content_type {
        name = add_extension_based_on_mime(&name, &content_type);
    }
    media.write_data(&name, &data).ok()
}

fn url_file_name(url: &str) -> String {
    let url = if is_file_url(url) {
        unquote(url)
    } else {
        unquote(url.split('?').next().unwrap_or_default())
    };
    match url.rsplit('/').next() {
        Some(name) if !name.trim().is_empty() => name.to_string(),
        _ => "paste".into(),
    }
}

/// Whether `fname` has a picture extension (any case).
pub fn is_picture(fname: &str) -> bool {
    let ext = fname.rsplit('.').next().unwrap_or_default().to_lowercase();
    PICS.contains(&ext.as_str())
}

/// `fnameToLink`, without playing the sound: an `<img>` for pictures,
/// else a `[sound:]` tag.
pub fn fname_to_link(fname: &str) -> String {
    if is_picture(fname) {
        format!(r#"<img src="{}">"#, quote(fname))
    } else {
        format!("[sound:{}]", html_escape(fname, false))
    }
}

/// `isURL`.
pub fn is_url(s: &str) -> bool {
    let s = s.to_lowercase();
    ["http://", "https://", "ftp://", "file://"]
        .iter()
        .any(|scheme| s.starts_with(scheme))
}

fn is_file_url(url: &str) -> bool {
    url.get(..7)
        .is_some_and(|scheme| scheme.eq_ignore_ascii_case("file://"))
}

/// The path a `file://` url names.
fn local_path(url: &str) -> PathBuf {
    let rest = &url[7..];
    let rest = rest.strip_prefix("localhost").unwrap_or(rest);
    PathBuf::from(unquote(rest))
}

/// `inlinedImageToFilename`: store a base64 jpeg/png/gif data url; empty
/// for other data (Qt raises on bad base64; this drops it the same way as
/// an unsupported type).
fn inlined_image_to_filename(txt: &str, media: &dyn Media) -> String {
    for ext in ["jpg", "jpeg", "png", "gif"] {
        let Some(data) = txt.strip_prefix(&format!("data:image/{ext};base64,")) else {
            continue;
        };
        let Ok(data) = BASE64.decode(data.trim().as_bytes()) else {
            return String::new();
        };
        let ext = if ext == "jpeg" { "jpg" } else { ext };
        return add_pasted_image(&data, ext, media).unwrap_or_default();
    }
    String::new()
}

/// `inlinedImageToLink`.
fn inlined_image_to_link(src: &str, media: &dyn Media) -> String {
    match inlined_image_to_filename(src, media) {
        name if name.is_empty() => name,
        name => fname_to_link(&name),
    }
}

/// `_pasted_image_filename`.
fn pasted_image_filename(data: &[u8], ext: &str) -> String {
    format!("paste-{}.{ext}", HEXLOWER.encode(&Sha1::digest(data)))
}

/// `_addPastedImage`.
fn add_pasted_image(data: &[u8], ext: &str, media: &dyn Media) -> Option<String> {
    media
        .write_data(&pasted_image_filename(data, ext), data)
        .ok()
}

/// `Editor._addMedia`: add a file (the attach button, a recording) and
/// return the tag for it.
pub fn add_media(media: &dyn Media, path: &Path) -> Result<String> {
    Ok(fname_to_link(&media.add_file(path)?))
}

/// `col.media.add_extension_based_on_mime`: add an extension for common
/// audio and image types if `fname` has none.
pub fn add_extension_based_on_mime(fname: &str, content_type: &str) -> String {
    // os.path.splitext: leading dots don't start an extension
    if fname.trim_start_matches('.').contains('.') {
        return fname.to_string();
    }
    let ext = match content_type {
        "audio/mpeg" => ".mp3",
        "audio/ogg" => ".oga",
        "audio/opus" => ".opus",
        "audio/wav" => ".wav",
        "audio/webm" => ".weba",
        "audio/aac" => ".aac",
        "image/jpeg" => ".jpg",
        "image/png" => ".png",
        "image/svg+xml" => ".svg",
        "image/webp" => ".webp",
        "image/avif" => ".avif",
        _ => "",
    };
    format!("{fname}{ext}")
}

/// Python's `html.escape`.
fn html_escape(text: &str, quote: bool) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' if quote => out.push_str("&quot;"),
            '\'' if quote => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

/// `urllib.parse.quote` of the UTF-8 bytes (`/` kept).
fn quote(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for &b in text.as_bytes() {
        if b.is_ascii_alphanumeric() || b"_.-~/".contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// `urllib.parse.unquote`: percent escapes as UTF-8, invalid bytes replaced.
fn unquote(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let hex = bytes.get(i + 1..i + 3).and_then(|hex| {
            std::str::from_utf8(hex)
                .ok()
                .and_then(|hex| u8::from_str_radix(hex, 16).ok())
        });
        match (bytes[i], hex) {
            (b'%', Some(byte)) => {
                out.push(byte);
                i += 3;
            }
            (byte, _) => {
                out.push(byte);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// `_wantsExtendedPaste`: keep formatting unless the collection strips it,
/// with shift flipping that.
pub fn wants_extended(strips_formatting: bool, shift: bool) -> bool {
    strips_formatting == shift
}

/// [`wants_extended`] with the collection's setting.
pub fn wants_extended_for(shell: &Shell, shift: bool) -> bool {
    wants_extended(config_bool(shell, BoolKey::PasteStripsFormatting), shift)
}

fn config_bool(shell: &Shell, key: BoolKey) -> bool {
    shell
        .api
        .call::<generic::Bool>("get_config_bool", &GetConfigBoolRequest { key: key as i32 })
        .is_ok_and(|value| value.val)
}

/// `EditorWebView`'s tracking of copies from a field, whose HTML is pasted
/// unfiltered. The page reports a cut/copy around when WebKit writes the
/// pasteboard (before or after; the bridge is asynchronous), so, as Qt does
/// on its clipboard-change signal, the pasteboard is captured a moment
/// later ([COPY_SETTLE]). A paste is the field's while the pasteboard
/// still holds that capture.
#[derive(Default)]
pub struct InternalPaste(Mutex<CopyState>);

/// How long after a reported copy WebKit has surely written the pasteboard.
pub const COPY_SETTLE: Duration = Duration::from_millis(200);

#[derive(Default)]
enum CopyState {
    #[default]
    None,
    /// A copy was reported at this time, and not captured yet.
    Pending(Instant),
    /// What the pasteboard held after the copy: (change count, HTML).
    Captured(isize, String),
}

impl InternalPaste {
    /// `user_cut_or_copied`. Call [InternalPaste::settle] after
    /// [COPY_SETTLE].
    pub fn on_cut_or_copy(&self) {
        *self.0.lock().unwrap() = CopyState::Pending(Instant::now());
    }

    /// Capture the pasteboard for a pending copy (`read` gives its change
    /// count and HTML), waiting first if the copy was just reported.
    pub fn settle(&self, read: impl FnOnce() -> (isize, Option<String>)) {
        let since = match *self.0.lock().unwrap() {
            CopyState::Pending(at) => at.elapsed(),
            _ => return,
        };
        if since < COPY_SETTLE {
            std::thread::sleep(COPY_SETTLE - since);
        }
        let (count, html) = read();
        let mut state = self.0.lock().unwrap();
        if matches!(*state, CopyState::Pending(_)) {
            *state = match html {
                Some(html) => CopyState::Captured(count, html),
                None => CopyState::None,
            };
        }
    }

    /// The field's HTML, if `contents` (read at `change_count`) is still
    /// what the field wrote; otherwise the copy is forgotten.
    pub fn field_html(&self, change_count: isize, contents: &Contents) -> Option<String> {
        let mut state = self.0.lock().unwrap();
        match &*state {
            CopyState::Captured(count, html)
                if *count == change_count && contents.html.as_ref() == Some(html) =>
            {
                Some(html.clone())
            }
            CopyState::Captured(..) => {
                *state = CopyState::None;
                None
            }
            _ => None,
        }
    }
}

/// HTML for the page's `pasteHTML(html, internal, extended)`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Paste {
    pub html: String,
    pub internal: bool,
    pub extended: bool,
    /// Download/import failures, for `showWarning`.
    pub warnings: Vec<String>,
}

/// `_onPaste` and `doPaste` up to the eval: the pasteboard (the general one
/// for `None`) as filtered HTML, or None if there's nothing to paste.
pub fn paste_html(
    shell: &Shell,
    internal: &InternalPaste,
    pasteboard: Option<&str>,
) -> Option<Paste> {
    internal.settle(|| {
        let count = pasteboard::change_count(pasteboard);
        (count, pasteboard::read_html(pasteboard))
    });
    let count = pasteboard::change_count(pasteboard);
    let contents = pasteboard::read(pasteboard, config_bool(shell, BoolKey::PasteImagesAsPng));
    // what was read may be newer than `count`; then it isn't the field's
    let field = (pasteboard::change_count(pasteboard) == count)
        .then(|| internal.field_html(count, &contents))
        .flatten();
    let extended = wants_extended_for(shell, pasteboard::shift_held());
    let media = ShellMedia::new(shell);
    let (html, internal) = match field {
        Some(html) => (html, true),
        None => mime_to_html(&contents, extended, &media)?,
    };
    Some(Paste {
        html: pre_filter(&html, internal, &media),
        internal,
        extended,
        warnings: media.take_warnings(),
    })
}

/// `select_image_from_clipboard_and_occlude`'s lookup: a pasted picture
/// file or url, imported into the media folder, else pasted image data
/// in a temporary file.
pub fn occlusion_image_from_pasteboard(shell: &Shell, pasteboard: Option<&str>) -> Option<PathBuf> {
    let contents = pasteboard::read(pasteboard, config_bool(shell, BoolKey::PasteImagesAsPng));
    let media = ShellMedia::new(shell);
    let path = imported_picture(shell, &contents.urls, &media).or_else(|| {
        let image = contents.image.as_ref()?;
        write_temp_image(image)
            .inspect_err(|err| tracing::warn!(%err, "saving pasted image failed"))
            .ok()
    });
    log_warnings(&media);
    path
}

/// `_processUrls(allowed_suffixes=pics)` + `extract_img_path_from_html`,
/// stopping at the first picture rather than importing them all.
fn imported_picture(shell: &Shell, urls: &[String], media: &dyn Media) -> Option<PathBuf> {
    let name = urls
        .iter()
        .filter_map(|url| url.lines().next())
        .find_map(|url| url_to_file(url, PICS, media))?;
    Some(shell.media_folder()?.join(name))
}

/// `_read_pasted_image`'s temporary file.
fn write_temp_image(image: &Image) -> Result<PathBuf> {
    let dir = std::env::temp_dir().join("anki-paste");
    fs::create_dir_all(&dir).with_context(|| format!("creating {}", dir.display()))?;
    let path = dir.join(pasted_image_filename(&image.data, image.ext));
    fs::write(&path, &image.data).with_context(|| format!("writing {}", path.display()))?;
    Ok(path)
}

fn log_warnings(media: &ShellMedia) {
    for warning in media.take_warnings() {
        tracing::warn!(warning, "image occlusion paste");
    }
}

#[cfg(test)]
mod test {
    use std::cell::RefCell;
    use std::collections::HashMap;

    use anyhow::anyhow;

    use super::*;
    use crate::pasteboard::Image;

    /// Records writes; serves canned fetches.
    #[derive(Default)]
    struct FakeMedia {
        written: RefCell<Vec<(String, Vec<u8>)>>,
        fetched: RefCell<Vec<String>>,
        responses: HashMap<String, (Vec<u8>, Option<String>)>,
    }

    impl FakeMedia {
        fn serving(url: &str, data: &[u8], content_type: Option<&str>) -> Self {
            let mut media = FakeMedia::default();
            media.responses.insert(
                url.to_string(),
                (data.to_vec(), content_type.map(str::to_string)),
            );
            media
        }

        fn names(&self) -> Vec<String> {
            self.written
                .borrow()
                .iter()
                .map(|(n, _)| n.clone())
                .collect()
        }
    }

    impl Media for FakeMedia {
        fn write_data(&self, desired_name: &str, data: &[u8]) -> Result<String> {
            self.written
                .borrow_mut()
                .push((desired_name.to_string(), data.to_vec()));
            Ok(desired_name.to_string())
        }

        fn add_file(&self, path: &Path) -> Result<String> {
            let name = path.file_name().unwrap().to_string_lossy().into_owned();
            self.write_data(&name, &std::fs::read(path)?)
        }

        fn fetch(&self, url: &str) -> Result<(Vec<u8>, Option<String>)> {
            self.fetched.borrow_mut().push(url.to_string());
            self.responses
                .get(url)
                .cloned()
                .ok_or_else(|| anyhow!("no route to {url}"))
        }
    }

    fn sha1_hex(data: &[u8]) -> String {
        use sha1::Digest;
        data_encoding::HEXLOWER.encode(&sha1::Sha1::digest(data))
    }

    #[test]
    fn external_paste_drops_active_content_and_turns_p_into_div() {
        let media = FakeMedia::default();
        let html = "<p>a</p><script>x()</script><style>b{}</style>\
                    <iframe src=\"y\"></iframe><object></object><b>c</b>";
        assert_eq!(pre_filter(html, false, &media), "<div>a</div><b>c</b>");
    }

    #[test]
    fn internal_paste_keeps_tags() {
        let media = FakeMedia::default();
        let html = "<p>a</p><style>b{}</style>";
        assert_eq!(pre_filter(html, true, &media), html);
    }

    #[test]
    fn internal_paste_makes_mediasrv_srcs_relative() {
        let media = FakeMedia::default();
        assert_eq!(
            pre_filter(
                r#"<img src="http://127.0.0.1:4000/a%20b.png">"#,
                true,
                &media
            ),
            r#"<img src="a%20b.png">"#
        );
        // the shell's own origin, which WebKit writes when copying a field
        assert_eq!(
            pre_filter(r#"<img src="anki://localhost/c.png">"#, true, &media),
            r#"<img src="c.png">"#
        );
        assert!(media.fetched.borrow().is_empty());
    }

    #[test]
    fn external_paste_of_own_media_refers_to_it_by_name() {
        let media = FakeMedia::default();
        assert_eq!(
            pre_filter(r#"<img src="anki://localhost/c.png">"#, false, &media),
            r#"<img src="c.png">"#
        );
        assert!(media.fetched.borrow().is_empty());
    }

    #[test]
    fn external_paste_downloads_remote_images() {
        let url = "https://example.com/pics/cat.jpg?size=2";
        let media = FakeMedia::serving(url, b"jpeg", Some("image/jpeg"));
        let html = format!(r#"<div><img src="{url}" alt="x"></div>"#);
        assert_eq!(
            pre_filter(&html, false, &media),
            r#"<div><img src="cat.jpg" alt="x"></div>"#
        );
        assert_eq!(
            *media.written.borrow(),
            [("cat.jpg".into(), b"jpeg".to_vec())]
        );
    }

    #[test]
    fn failed_download_keeps_the_src() {
        let media = FakeMedia::default();
        let html = r#"<img src="https://example.com/gone.png">"#;
        assert_eq!(pre_filter(html, false, &media), html);
        assert!(media.written.borrow().is_empty());
    }

    #[test]
    fn internal_paste_does_not_download() {
        let media = FakeMedia::default();
        let html = r#"<img src="https://example.com/x.png">"#;
        assert_eq!(pre_filter(html, true, &media), html);
        assert!(media.fetched.borrow().is_empty());
    }

    #[test]
    fn inlined_images_become_media_files() {
        let media = FakeMedia::default();
        let html = r#"<img src="data:image/jpeg;base64,aW1n">"#;
        let name = format!("paste-{}.jpg", sha1_hex(b"img"));
        assert_eq!(
            pre_filter(html, false, &media),
            format!(r#"<img src="{name}">"#)
        );
        assert_eq!(*media.written.borrow(), [(name, b"img".to_vec())]);
    }

    #[test]
    fn unsupported_inlined_images_lose_their_src_like_qt() {
        let media = FakeMedia::default();
        let html = r#"<img src="data:image/webp;base64,aW1n">"#;
        assert_eq!(pre_filter(html, false, &media), r#"<img src="">"#);
    }

    #[test]
    fn imgs_without_src_are_left_alone() {
        let media = FakeMedia::default();
        assert_eq!(
            pre_filter("<img alt=\"a\">", false, &media),
            "<img alt=\"a\">"
        );
    }

    #[test]
    fn text_without_a_tag_end_passes_through() {
        let media = FakeMedia::default();
        // html5ever would turn this into "&lt;b" and "&amp;"
        assert_eq!(pre_filter("a <b & c", false, &media), "a <b & c");
    }

    #[test]
    fn output_is_a_fragment() {
        let media = FakeMedia::default();
        let html = "<html><head><meta charset=\"utf-8\"></head><body><!--StartFragment--><b>x</b></body></html>";
        assert_eq!(
            pre_filter(html, false, &media),
            "<meta charset=\"utf-8\"><!--StartFragment--><b>x</b>"
        );
    }

    #[test]
    fn text_is_escaped_with_spaces_and_tabs_kept() {
        let media = FakeMedia::default();
        assert_eq!(
            process_text("a<b> & 'c\"\tx   y", false, &media),
            "a&lt;b&gt; &amp; &#x27;c&quot;&nbsp;&nbsp;&nbsp; x&nbsp;&nbsp; y"
        );
    }

    #[test]
    fn text_lines_are_joined_with_br() {
        let media = FakeMedia::default();
        assert_eq!(process_text("a\nb\n", false, &media), "a<br>b<br>");
    }

    #[test]
    fn plain_text_leaves_urls_alone() {
        let media = FakeMedia::default();
        assert_eq!(
            process_text("see https://example.com/a.png", false, &media),
            "see https://example.com/a.png"
        );
        assert!(media.fetched.borrow().is_empty());
    }

    #[test]
    fn extended_text_links_urls_and_imports_media() {
        let media = FakeMedia::serving("https://example.com/a.png", b"png", None);
        assert_eq!(
            process_text(
                "see https://example.com/a.png and https://example.com/a%20b",
                true,
                &media
            ),
            "see <img src=\"a.png\"> and \
             <a href=\"https://example.com/a%20b\">https://example.com/a b</a>"
        );
    }

    #[test]
    fn extended_text_imports_inlined_images() {
        let media = FakeMedia::default();
        let name = format!("paste-{}.png", sha1_hex(b"img"));
        assert_eq!(
            process_text("x data:image/png;base64,aW1n", true, &media),
            format!("x <img src=\"{name}\">")
        );
    }

    #[test]
    fn non_media_urls_become_links() {
        let media = FakeMedia::default();
        assert_eq!(
            url_to_link("https://example.com/a%3Cb%3E", &[], &media),
            "<a href=\"https://example.com/a%3Cb%3E\">https://example.com/a&lt;b&gt;</a>"
        );
        assert!(media.fetched.borrow().is_empty());
    }

    #[test]
    fn allowed_suffixes_limit_what_is_imported() {
        let media = FakeMedia::serving("file:///tmp/a.mp3", b"mp3", None);
        assert_eq!(
            url_to_link("file:///tmp/a.mp3", PICS, &media),
            "<a href=\"file:///tmp/a.mp3\">file:///tmp/a.mp3</a>"
        );
        assert_eq!(
            url_to_link("file:///tmp/a.mp3", &[], &media),
            "[sound:a.mp3]"
        );
    }

    #[test]
    fn suffixes_match_case_insensitively() {
        let media = FakeMedia::serving("https://example.com/A.PNG", b"png", None);
        assert_eq!(
            url_to_link("https://example.com/A.PNG", &[], &media),
            "<img src=\"A.PNG\">"
        );
    }

    #[test]
    fn downloaded_names_are_unquoted_and_get_an_extension_from_the_mime_type() {
        let media = FakeMedia::serving(
            "https://example.com/my%20pic.png?x=1",
            b"png",
            Some("image/png"),
        );
        assert_eq!(
            url_to_link("https://example.com/my%20pic.png?x=1", &[], &media),
            "<a href=\"https://example.com/my%20pic.png?x=1\">https://example.com/my pic.png?x=1</a>",
            "a query string hides the suffix, as in Qt"
        );
        let media = FakeMedia::serving("https://example.com/my%20pic.png", b"png", None);
        assert_eq!(
            url_to_link("https://example.com/my%20pic.png", &[], &media),
            "<img src=\"my%20pic.png\">"
        );
        assert_eq!(media.names(), ["my pic.png"]);
    }

    #[test]
    fn local_files_keep_their_names() {
        let url = "file:///Users/x/50%25%20off%23.png";
        let media = FakeMedia::serving(url, b"png", None);
        assert_eq!(
            url_to_link(url, &[], &media),
            "<img src=\"50%25%20off%23.png\">"
        );
        assert_eq!(media.names(), ["50% off#.png"]);
    }

    #[test]
    fn fname_links_quote_pictures_and_escape_sounds() {
        assert_eq!(fname_to_link("a b/ü.JPG"), "<img src=\"a%20b/%C3%BC.JPG\">");
        assert_eq!(
            fname_to_link("a&b<\"c\">.mp3"),
            "[sound:a&amp;b&lt;\"c\"&gt;.mp3]"
        );
        assert_eq!(fname_to_link("noext"), "[sound:noext]");
    }

    #[test]
    fn pictures_are_recognised_by_extension() {
        assert!(is_picture("a.PNG"));
        assert!(is_picture("a.b.avif"));
        assert!(!is_picture("a.mp3"));
    }

    #[test]
    fn urls_are_recognised_by_scheme() {
        for url in ["http://a", "HTTPS://a", "ftp://a", "file:///a"] {
            assert!(is_url(url), "{url}");
        }
        for s in ["data:image/png", "anki://localhost/a", "www.example.com"] {
            assert!(!is_url(s), "{s}");
        }
    }

    #[test]
    fn extensions_come_from_the_mime_type_only_when_missing() {
        assert_eq!(add_extension_based_on_mime("a", "image/jpeg"), "a.jpg");
        assert_eq!(add_extension_based_on_mime("a", "audio/webm"), "a.weba");
        assert_eq!(add_extension_based_on_mime("a", "image/svg+xml"), "a.svg");
        assert_eq!(add_extension_based_on_mime("a.gif", "image/jpeg"), "a.gif");
        assert_eq!(add_extension_based_on_mime("a", "text/html"), "a");
        assert_eq!(
            add_extension_based_on_mime(".bashrc", "image/png"),
            ".bashrc.png"
        );
    }

    #[test]
    fn shift_flips_the_strip_formatting_setting() {
        assert!(wants_extended(false, false));
        assert!(!wants_extended(true, false));
        assert!(!wants_extended(false, true));
        assert!(wants_extended(true, true));
    }

    fn contents() -> Contents {
        Contents::default()
    }

    fn png_image() -> Image {
        Image {
            data: b"img".to_vec(),
            ext: "png",
        }
    }

    #[test]
    fn html_beats_everything_and_is_external() {
        let media = FakeMedia::default();
        let c = Contents {
            html: Some("<b>x</b>".into()),
            text: Some("x".into()),
            urls: vec!["file:///a.png".into()],
            image: Some(png_image()),
        };
        assert_eq!(
            mime_to_html(&c, false, &media),
            Some(("<b>x</b>".into(), false))
        );
    }

    #[test]
    fn local_file_url_beats_image() {
        let media = FakeMedia::serving("file:///tmp/a.gif", b"gif", None);
        let c = Contents {
            text: Some("a.gif".into()),
            urls: vec!["file:///tmp/a.gif".into()],
            image: Some(png_image()),
            ..contents()
        };
        assert_eq!(
            mime_to_html(&c, false, &media),
            Some(("<img src=\"a.gif\">".into(), true))
        );
    }

    #[test]
    fn image_beats_web_url() {
        let media = FakeMedia::default();
        let c = Contents {
            urls: vec!["https://example.com/a.png".into()],
            image: Some(png_image()),
            ..contents()
        };
        let name = format!("paste-{}.png", sha1_hex(b"img"));
        assert_eq!(
            mime_to_html(&c, false, &media),
            Some((format!("<img src=\"{name}\">"), true))
        );
        assert!(media.fetched.borrow().is_empty());
    }

    #[test]
    fn web_url_beats_text() {
        let media = FakeMedia::default();
        let c = Contents {
            text: Some("Example".into()),
            urls: vec!["https://example.com/\nhttps://example.com/".into()],
            ..contents()
        };
        assert_eq!(
            mime_to_html(&c, false, &media),
            Some((
                "<a href=\"https://example.com/\">https://example.com/</a>".into(),
                true
            ))
        );
    }

    #[test]
    fn text_is_the_fallback() {
        let media = FakeMedia::default();
        let c = Contents {
            text: Some("a  b".into()),
            ..contents()
        };
        assert_eq!(
            mime_to_html(&c, false, &media),
            Some(("a&nbsp; b".into(), true))
        );
        assert_eq!(mime_to_html(&contents(), false, &media), None);
    }

    fn with_html(html: &str) -> Contents {
        Contents {
            html: Some(html.into()),
            ..contents()
        }
    }

    fn captured(tracker: &InternalPaste, count: isize, html: Option<&str>) {
        tracker.settle(|| (count, html.map(Into::into)));
    }

    #[test]
    fn copy_from_a_field_is_internal_until_the_pasteboard_changes_again() {
        let tracker = InternalPaste::default();
        let field = with_html("<b>field</b>");
        assert_eq!(tracker.field_html(5, &field), None, "no copy yet");
        tracker.on_cut_or_copy();
        assert_eq!(tracker.field_html(5, &field), None, "not captured yet");
        captured(&tracker, 6, Some("<b>field</b>"));
        assert_eq!(
            tracker.field_html(6, &field).as_deref(),
            Some("<b>field</b>")
        );
        assert_eq!(
            tracker.field_html(6, &field).as_deref(),
            Some("<b>field</b>"),
            "pasted twice"
        );
        assert_eq!(tracker.field_html(7, &with_html("<i>other</i>")), None);
        assert_eq!(tracker.field_html(6, &field), None, "forgotten");
    }

    #[test]
    fn same_html_from_elsewhere_is_external() {
        let tracker = InternalPaste::default();
        tracker.on_cut_or_copy();
        captured(&tracker, 3, Some("x"));
        assert_eq!(tracker.field_html(4, &with_html("x")), None);
    }

    #[test]
    fn copy_without_html_is_not_internal() {
        let tracker = InternalPaste::default();
        tracker.on_cut_or_copy();
        captured(&tracker, 2, None);
        let text_only = Contents {
            text: Some("x".into()),
            ..contents()
        };
        assert_eq!(tracker.field_html(2, &text_only), None);
    }

    #[test]
    fn settling_waits_for_the_pasteboard_write() {
        let tracker = InternalPaste::default();
        tracker.on_cut_or_copy();
        let start = Instant::now();
        captured(&tracker, 1, Some("y"));
        assert!(start.elapsed() >= COPY_SETTLE - Duration::from_millis(5));
        // only a pending copy is captured
        let start = Instant::now();
        captured(&tracker, 9, Some("z"));
        assert!(start.elapsed() < COPY_SETTLE);
        assert_eq!(tracker.field_html(1, &with_html("y")).as_deref(), Some("y"));
    }
}

#[cfg(test)]
mod shell_test {
    use std::io::Read;
    use std::io::Write;
    use std::net::TcpListener;

    use anki_proto::config::SetConfigBoolRequest;
    use anki_proto::generic::Empty;

    use super::*;
    use crate::api::Api;
    use crate::pasteboard::ScratchPasteboard;
    use crate::profile::ProfilePaths;

    /// A 1x1 PNG.
    const PNG: &[u8] = &[
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x02, 0x00, 0x00, 0x00, 0x90,
        0x77, 0x53, 0xde, 0x00, 0x00, 0x00, 0x0c, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0xf8,
        0xcf, 0xc0, 0x00, 0x00, 0x03, 0x01, 0x01, 0x00, 0xc9, 0xfe, 0x92, 0xef, 0x00, 0x00, 0x00,
        0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
    ];

    fn shell() -> (tempfile::TempDir, Shell) {
        let dir = tempfile::tempdir().unwrap();
        let shell = Shell::new(Api::new(vec!["en".into()]).unwrap(), Default::default());
        shell
            .open_profile(ProfilePaths::new(dir.path(), "User 1"))
            .unwrap();
        (dir, shell)
    }

    fn set_config(shell: &Shell, key: BoolKey, value: bool) {
        let _: Empty = shell
            .api
            .call(
                "set_config_bool",
                &SetConfigBoolRequest {
                    key: key as i32,
                    value,
                    undoable: false,
                },
            )
            .unwrap();
    }

    /// Serves one request on loopback with `response`, returning its url.
    fn serve_once(response: &'static [u8]) -> String {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut buf = [0; 4096];
            let _ = stream.read(&mut buf);
            stream.write_all(response).unwrap();
        });
        format!("http://127.0.0.1:{port}/files/pic")
    }

    #[test]
    fn text_paste_is_escaped() {
        let (_dir, shell) = shell();
        let pb = ScratchPasteboard::new();
        pb.write(None, Some("a<b"), &[], None);
        let paste = paste_html(&shell, &InternalPaste::default(), Some(pb.name())).unwrap();
        assert_eq!(paste.html, "a&lt;b");
        assert!(paste.internal);
    }

    #[test]
    fn html_paste_is_filtered_unless_copied_from_a_field() {
        let (_dir, shell) = shell();
        let pb = ScratchPasteboard::new();
        let tracker = InternalPaste::default();
        pb.write(Some("<p>x</p>"), None, &[], None);
        let paste = paste_html(&shell, &tracker, Some(pb.name())).unwrap();
        assert_eq!(
            (paste.html.as_str(), paste.internal),
            ("<div>x</div>", false)
        );

        // as WebKit: the copy is reported, and the pasteboard written
        tracker.on_cut_or_copy();
        pb.write(Some("<p>y</p>"), None, &[], None);
        let paste = paste_html(&shell, &tracker, Some(pb.name())).unwrap();
        assert_eq!((paste.html.as_str(), paste.internal), ("<p>y</p>", true));
    }

    #[test]
    fn nothing_to_paste() {
        let (_dir, shell) = shell();
        let pb = ScratchPasteboard::new();
        assert_eq!(
            paste_html(&shell, &InternalPaste::default(), Some(pb.name())),
            None
        );
    }

    #[test]
    fn pasted_images_go_into_the_media_folder() {
        let (dir, shell) = shell();
        set_config(&shell, BoolKey::PasteImagesAsPng, true);
        let pb = ScratchPasteboard::new();
        pb.write(None, None, &[], Some(PNG));
        let paste = paste_html(&shell, &InternalPaste::default(), Some(pb.name())).unwrap();
        let name = paste
            .html
            .strip_prefix("<img src=\"paste-")
            .and_then(|rest| rest.strip_suffix(".png\">"))
            .unwrap_or_else(|| panic!("{}", paste.html));
        let stored = ProfilePaths::new(dir.path(), "User 1")
            .media()
            .join(format!("paste-{name}.png"));
        assert!(std::fs::read(stored).unwrap().starts_with(b"\x89PNG"));
    }

    #[test]
    fn dropped_html_keeps_strip_formatting_setting() {
        let (_dir, shell) = shell();
        assert!(wants_extended_for(&shell, false));
        set_config(&shell, BoolKey::PasteStripsFormatting, true);
        assert!(!wants_extended_for(&shell, false));
        assert!(wants_extended_for(&shell, true));
    }

    #[test]
    fn occlusion_takes_a_picture_file_into_the_media_folder() {
        let (dir, shell) = shell();
        let file = dir.path().join("my pic.png");
        std::fs::write(&file, PNG).unwrap();
        let pb = ScratchPasteboard::new();
        let url = format!("file://{}", quote(&file.to_string_lossy()));
        pb.write(None, None, &[&url], Some(PNG));
        let path = occlusion_image_from_pasteboard(&shell, Some(pb.name())).unwrap();
        assert_eq!(
            path,
            ProfilePaths::new(dir.path(), "User 1")
                .media()
                .join("my pic.png")
        );
        assert_eq!(std::fs::read(path).unwrap(), PNG);
    }

    #[test]
    fn occlusion_falls_back_to_image_data_in_a_temp_file() {
        let (_dir, shell) = shell();
        let pb = ScratchPasteboard::new();
        pb.write(None, None, &["https://example.com/page"], Some(PNG));
        let path = occlusion_image_from_pasteboard(&shell, Some(pb.name())).unwrap();
        let name = path.file_name().unwrap().to_string_lossy();
        assert!(
            name.starts_with("paste-") && name.ends_with(".jpg"),
            "{name}"
        );
        assert!(std::fs::read(&path)
            .unwrap()
            .starts_with(&[0xff, 0xd8, 0xff]));
        assert_eq!(
            occlusion_image_from_pasteboard(&shell, Some(ScratchPasteboard::new().name())),
            None
        );
    }

    #[test]
    fn downloads_report_bad_status_like_qt() {
        let (_dir, shell) = shell();
        let media = ShellMedia::new(&shell);
        let url = serve_once(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n");
        assert!(media.fetch(&url).is_err());
        let warnings: Vec<String> = media
            .take_warnings()
            .iter()
            .map(|w| w.replace(['\u{2068}', '\u{2069}'], ""))
            .collect();
        assert_eq!(warnings, ["Unexpected response code: 404"]);
    }

    #[test]
    fn downloads_carry_the_content_type() {
        let (_dir, shell) = shell();
        let media = ShellMedia::new(&shell);
        let url = serve_once(
            b"HTTP/1.1 200 OK\r\nContent-Type: image/png\r\nContent-Length: 3\r\n\r\npng",
        );
        assert_eq!(
            media.fetch(&url).unwrap(),
            (b"png".to_vec(), Some("image/png".to_string()))
        );
    }

    #[test]
    fn missing_local_files_are_reported() {
        let (dir, shell) = shell();
        let media = ShellMedia::new(&shell);
        let missing = dir.path().join("gone.png");
        assert!(media
            .fetch(&format!("file://{}", missing.display()))
            .is_err());
        let warnings = media.take_warnings();
        assert_eq!(warnings.len(), 1);
        assert!(
            warnings[0].starts_with("An error occurred while opening")
                && warnings[0].contains("gone.png"),
            "{warnings:?}"
        );
    }

    #[test]
    fn attached_files_keep_their_names_unless_taken() {
        let (dir, shell) = shell();
        let media = ShellMedia::new(&shell);
        let file = dir.path().join("a.mp3");
        std::fs::write(&file, b"one").unwrap();
        assert_eq!(add_media(&media, &file).unwrap(), "[sound:a.mp3]");
        std::fs::write(&file, b"two").unwrap();
        assert_ne!(add_media(&media, &file).unwrap(), "[sound:a.mp3]");
    }
}
