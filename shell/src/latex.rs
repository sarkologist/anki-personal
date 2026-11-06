// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! LaTeX on cards (`[latex]`, `[$]`, `[$$]`) rendered to images, as
//! pylib/anki/latex.py does from its `card_did_render` hook.

use std::cell::OnceCell;
use std::ffi::OsString;
use std::fs;
use std::fs::File;
use std::path::Path;
use std::path::PathBuf;
use std::process::Command;
use std::process::Stdio;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::sync::RwLock;

use anki::prelude::I18n;
use anki_proto::card_rendering::ExtractLatexRequest;
use anki_proto::card_rendering::ExtractLatexResponse;
use anki_proto::card_rendering::ExtractedLatex;
use anki_proto::config::config_key::Bool as BoolKey;
use anki_proto::config::GetConfigBoolRequest;
use anki_proto::generic;
use anki_proto::i18n::I18nResourcesRequest;
use anki_proto::media::AddMediaFileRequest;
use anki_proto::notetypes::Notetype;
use anki_proto::notetypes::NotetypeId;
use anyhow::Context;
use anyhow::Result;
use serde::Deserialize;
use serde::Serialize;

use crate::api::Api;

const PNG_COMMANDS: [&[&str]; 2] = [
    &["latex", "-interaction=nonstopmode", "tmp.tex"],
    &[
        "dvipng",
        "-bg",
        "Transparent",
        "-D",
        "200",
        "-T",
        "tight",
        "tmp.dvi",
        "-o",
        "tmp.png",
    ],
];

const SVG_COMMANDS: [&[&str]; 2] = [
    &["latex", "-interaction=nonstopmode", "tmp.tex"],
    &[
        "dvisvgm",
        "--no-fonts",
        "--exact",
        "-Z",
        "2",
        "tmp.dvi",
        "-o",
        "tmp.svg",
    ],
];

/// Standard TeX install locations, which a GUI app's minimal PATH lacks.
const MAC_TEX_DIRS: [&str; 2] = ["/usr/texbin", "/Library/TeX/texbin"];

/// latex.py's `pngCommands`/`svgCommands`, where they aren't its defaults
/// (an add-on such as "Edit LaTeX toolchain" replaces them). Each command
/// runs in the render folder with `tmp.tex`, and the last leaves `tmp.png`
/// or `tmp.svg`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct LatexCommands {
    pub png: Option<Vec<Vec<String>>>,
    pub svg: Option<Vec<Vec<String>>>,
}

impl LatexCommands {
    pub fn is_empty(&self) -> bool {
        self.png.is_none() && self.svg.is_none()
    }

    /// The commands for one kind of image, defaults included.
    pub fn for_kind(&self, svg: bool) -> Vec<Vec<String>> {
        let custom = if svg { &self.svg } else { &self.png };
        custom.clone().unwrap_or_else(|| {
            latex_commands(svg)
                .iter()
                .map(|c| c.iter().map(|s| s.to_string()).collect())
                .collect()
        })
    }
}

/// The toolchain from the settings, as latex.py's module globals.
static TOOLCHAIN: RwLock<LatexCommands> = RwLock::new(LatexCommands {
    png: None,
    svg: None,
});

pub fn set_toolchain(commands: LatexCommands) {
    *TOOLCHAIN.write().unwrap() = commands;
}

pub fn toolchain() -> LatexCommands {
    TOOLCHAIN.read().unwrap().clone()
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct LatexConfig {
    pub latex_pre: String,
    pub latex_post: String,
    pub svg: bool,
    /// The commands to run (latex.py's by default).
    pub toolchain: LatexCommands,
}

/// The notetype's LaTeX header/footer and svg flag (from the backend's
/// get_notetype: Notetype.config latex_pre / latex_post / latex_svg), with
/// the toolchain.
pub fn latex_config(api: &Api, notetype_id: i64) -> Result<LatexConfig> {
    latex_config_with(api, notetype_id, &toolchain())
}

pub(crate) fn latex_config_with(
    api: &Api,
    notetype_id: i64,
    toolchain: &LatexCommands,
) -> Result<LatexConfig> {
    let notetype: Notetype = api.call("get_notetype", &NotetypeId { ntid: notetype_id })?;
    let config = notetype.config.unwrap_or_default();
    Ok(LatexConfig {
        latex_pre: config.latex_pre,
        latex_post: config.latex_post,
        svg: config.latex_svg,
        toolchain: toolchain.clone(),
    })
}

/// latex.py's render_latex_returning_errors: rewrite LaTeX in `html` into
/// <img> tags (backend extract_latex, expand_clozes=false), generate any image
/// missing from `media_dir`, and return (html, errors). Errors are HTML
/// messages as latex.py builds them (tr strings + escaped log).
pub fn render_latex(
    api: &Api,
    media_dir: &Path,
    html: &str,
    cfg: &LatexConfig,
) -> (String, Vec<String>) {
    render_latex_expanding(api, media_dir, html, cfg, false)
}

/// [render_latex], optionally with clozes expanded first (as the media
/// check finds LaTeX images, and `render_all_latex` makes them).
pub fn render_latex_expanding(
    api: &Api,
    media_dir: &Path,
    html: &str,
    cfg: &LatexConfig,
    expand_clozes: bool,
) -> (String, Vec<String>) {
    match try_render_latex(api, media_dir, html, cfg, expand_clozes) {
        Ok(out) => out,
        // latex.py would raise; keep the card and show why
        Err(err) => (html.to_string(), vec![html_escape(&format!("{err:#}"))]),
    }
}

fn try_render_latex(
    api: &Api,
    media_dir: &Path,
    html: &str,
    cfg: &LatexConfig,
    expand_clozes: bool,
) -> Result<(String, Vec<String>)> {
    let out: ExtractLatexResponse = api.call(
        "extract_latex",
        &ExtractLatexRequest {
            text: html.to_string(),
            svg: cfg.svg,
            expand_clozes,
        },
    )?;
    let mut errors = vec![];
    if out.latex.is_empty() {
        return Ok((out.text, errors));
    }
    let enabled: generic::Bool = api.call(
        "get_config_bool",
        &GetConfigBoolRequest {
            key: BoolKey::RenderLatex as i32,
        },
    )?;
    let tr = OnceCell::new();
    let tr = || tr.get_or_init(|| backend_tr(api));
    for latex in &out.latex {
        if media_dir.join(&latex.filename).exists() {
            continue;
        }
        if !enabled.val {
            errors.push(tr().preferences_latex_generation_disabled().into_owned());
            return Ok((out.text, errors));
        }
        match save_latex_image(api, tr, latex, cfg) {
            Ok(None) => {}
            Ok(Some(err)) => errors.push(err),
            // latex.py would raise; report it with the other errors
            Err(err) => errors.push(html_escape(&format!("{err:#}"))),
        }
    }
    Ok((out.text, errors))
}

/// Translations in the backend's languages.
pub fn backend_tr(api: &Api) -> I18n {
    let langs = api
        .call::<generic::Json>(
            "i18n_resources",
            &I18nResourcesRequest {
                modules: vec!["media".into()],
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

/// Render one image into the media folder. Returns latex.py's error message
/// if a command fails.
fn save_latex_image<'a>(
    api: &Api,
    tr: impl FnOnce() -> &'a I18n,
    extracted: &ExtractedLatex,
    cfg: &LatexConfig,
) -> Result<Option<String>> {
    let data = match render_image(&extracted.latex_body, cfg)? {
        Ok(data) => data,
        Err(failed) => return Ok(Some(failed.html(tr()))),
    };
    let _: generic::String = api.call(
        "add_media_file",
        &AddMediaFileRequest {
            desired_name: extracted.filename.clone(),
            data,
        },
    )?;
    Ok(None)
}

/// A LaTeX command that failed, with the files to look at.
struct Failed {
    command: String,
    tex: PathBuf,
    log: PathBuf,
}

impl Failed {
    fn log(&self) -> Option<String> {
        fs::read(&self.log)
            .ok()
            .and_then(|bytes| String::from_utf8(bytes).ok())
            .filter(|log| !log.is_empty())
    }

    /// latex.py's _err_msg.
    fn html(&self, tr: &I18n) -> String {
        let mut msg = format!("{}<br>", tr.media_error_executing(self.command.as_str()));
        msg += &format!(
            "{}<br>",
            tr.media_generated_file(self.tex.display().to_string())
        );
        match self.log() {
            Some(log) => msg += &format!("<small><pre>{}</pre></small>", html_escape(&log)),
            None => msg += &tr.media_have_you_installed_latex_and_dvipngdvisvgm(),
        }
        msg
    }

    /// The editor preview's plain-text version (`_latex_error_text`).
    fn text(&self, tr: &I18n) -> String {
        let mut msg = format!("{}\n", tr.media_error_executing(self.command.as_str()));
        msg += &format!(
            "{}\n",
            tr.media_generated_file(self.tex.display().to_string())
        );
        match self.log() {
            Some(log) => msg += &log,
            None => msg += &tr.media_have_you_installed_latex_and_dvipngdvisvgm(),
        }
        msg
    }
}

/// Run latex and dvipng/dvisvgm on `body` in a fresh folder; the image's
/// bytes, or the command that failed (whose folder is kept, so the paths
/// in the message stay valid).
fn render_image(body: &str, cfg: &LatexConfig) -> Result<std::result::Result<Vec<u8>, Failed>> {
    let ext = if cfg.svg { "svg" } else { "png" };
    let dir = render_dir()?;
    let tex = dir.join("tmp.tex");
    let log_path = dir.join("latex_log.txt");
    fs::write(&tex, tex_source(&cfg.latex_pre, body, &cfg.latex_post))
        .with_context(|| format!("writing {}", tex.display()))?;
    let log =
        File::create(&log_path).with_context(|| format!("creating {}", log_path.display()))?;
    let path = tex_path();
    for command in cfg
        .toolchain
        .for_kind(cfg.svg)
        .iter()
        .filter(|c| !c.is_empty())
    {
        if !run(command, &dir, &path, &log)? {
            return Ok(Err(Failed {
                command: command[0].clone(),
                tex,
                log: log_path,
            }));
        }
    }
    let image = dir.join(format!("tmp.{ext}"));
    let data = fs::read(&image).with_context(|| format!("reading {}", image.display()))?;
    let _ = fs::remove_dir_all(&dir);
    Ok(Ok(data))
}

/// An editor LaTeX preview (`editor_latex_preview.py`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Preview {
    /// A media file name, or a data URL for an image not saved.
    pub src: String,
    pub alt: String,
    pub svg: bool,
}

/// `render_legacy_latex_preview`: the image for one `[$]`/`[$$]` (`kind`
/// "inline" or "display") being edited. The image goes to the media folder
/// only if a saved field already has this LaTeX (in Add mode none is
/// saved); otherwise it comes back as a data URL, so typing doesn't fill
/// the media folder. Errors are plain text.
pub fn render_preview(
    api: &Api,
    media_dir: Option<&Path>,
    cfg: &LatexConfig,
    kind: &str,
    html: &str,
    saved_fields: &[String],
) -> std::result::Result<Preview, String> {
    let wrapped = if kind == "display" {
        format!("[$$]{html}[/$$]")
    } else {
        format!("[$]{html}[/$]")
    };
    let extract = |text: &str| {
        api.call::<ExtractLatexResponse>(
            "extract_latex",
            &ExtractLatexRequest {
                text: text.to_string(),
                svg: cfg.svg,
                expand_clozes: false,
            },
        )
        .map_err(|e| e.to_string())
    };
    let Some(latex) = extract(&wrapped)?.latex.into_iter().next() else {
        return Err("No LaTeX to preview".into());
    };
    let ready = |src: String| Preview {
        src,
        alt: latex.latex_body.clone(),
        svg: cfg.svg,
    };
    if media_dir.is_some_and(|dir| dir.join(&latex.filename).exists()) {
        return Ok(ready(latex.filename.clone()));
    }
    let enabled: generic::Bool = api
        .call(
            "get_config_bool",
            &GetConfigBoolRequest {
                key: BoolKey::RenderLatex as i32,
            },
        )
        .map_err(|e| e.to_string())?;
    let tr = backend_tr(api);
    if !enabled.val {
        return Err(tr.preferences_latex_generation_disabled().into_owned());
    }
    let saved = saved_fields.iter().any(|field| {
        extract(field).is_ok_and(|out| out.latex.iter().any(|l| l.filename == latex.filename))
    });
    if saved {
        return match save_latex_image(api, || &tr, &latex, cfg) {
            Ok(None) => Ok(ready(latex.filename.clone())),
            // an HTML message, from the card path
            Ok(Some(err)) => Err(err),
            Err(err) => Err(format!("{err:#}")),
        };
    }
    match render_image(&latex.latex_body, cfg) {
        Ok(Ok(data)) => {
            let mime = if cfg.svg {
                "image/svg+xml"
            } else {
                "image/png"
            };
            Ok(ready(format!(
                "data:{mime};base64,{}",
                data_encoding::BASE64.encode(&data)
            )))
        }
        Ok(Err(failed)) => Err(failed.text(&tr)),
        Err(err) => Err(format!("{err:#}")),
    }
}

/// Whether the command ran and succeeded (a missing binary counts as a
/// failure, as in anki.utils.call).
fn run(command: &[String], dir: &Path, path: &OsString, log: &File) -> Result<bool> {
    let status = Command::new(&command[0])
        .args(&command[1..])
        .current_dir(dir)
        .env("PATH", path)
        .stdin(Stdio::null())
        .stdout(log.try_clone()?)
        .stderr(log.try_clone()?)
        .status();
    Ok(status.is_ok_and(|status| status.success()))
}

/// A fresh folder per render, so overlapping renders don't share tmp.tex.
fn render_dir() -> Result<PathBuf> {
    static NEXT: AtomicU64 = AtomicU64::new(0);
    let parent = std::env::temp_dir().join("anki-latex");
    fs::create_dir_all(&parent).with_context(|| format!("creating {}", parent.display()))?;
    loop {
        let n = NEXT.fetch_add(1, Ordering::Relaxed);
        let dir = parent.join(format!("{}-{n}", std::process::id()));
        match fs::create_dir(&dir) {
            Ok(()) => return Ok(dir),
            // left by an earlier process with the same pid
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err).with_context(|| format!("creating {}", dir.display())),
        }
    }
}

fn latex_commands(svg: bool) -> [&'static [&'static str]; 2] {
    if svg {
        SVG_COMMANDS
    } else {
        PNG_COMMANDS
    }
}

fn tex_source(header: &str, body: &str, footer: &str) -> String {
    format!("{header}\n{body}\n{footer}")
}

/// The inherited PATH plus the standard TeX locations, as latex.py does.
fn tex_path() -> OsString {
    let inherited = std::env::var_os("PATH").unwrap_or_default();
    let dirs = std::env::split_paths(&inherited)
        .filter(|dir| !dir.as_os_str().is_empty())
        .chain(MAC_TEX_DIRS.iter().map(PathBuf::from));
    std::env::join_paths(dirs).unwrap_or(inherited)
}

/// Python's html.escape(quote=True).
pub(crate) fn html_escape(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

#[cfg(test)]
mod test {
    use std::path::PathBuf;

    use anki_proto::config::SetConfigBoolRequest;
    use anki_proto::generic::Empty;
    use anki_proto::notetypes::NotetypeNames;

    use super::*;
    use crate::profile::open_collection;
    use crate::profile::ProfilePaths;

    struct Fixture {
        _dir: tempfile::TempDir,
        api: Api,
        media: PathBuf,
    }

    fn collection(render_latex: bool) -> Fixture {
        let dir = tempfile::tempdir().unwrap();
        let paths = ProfilePaths::new(dir.path(), "User 1");
        std::fs::create_dir_all(&paths.folder).unwrap();
        let api = Api::new(vec!["en".into()]).unwrap();
        open_collection(&api, &paths).unwrap();
        let _: Empty = api
            .call(
                "set_config_bool",
                &SetConfigBoolRequest {
                    key: BoolKey::RenderLatex as i32,
                    value: render_latex,
                    undoable: false,
                },
            )
            .unwrap();
        Fixture {
            media: paths.media(),
            _dir: dir,
            api,
        }
    }

    fn stock_config(api: &Api) -> LatexConfig {
        let names: NotetypeNames = api.call("get_notetype_names", &Empty {}).unwrap();
        latex_config(api, names.entries[0].id).unwrap()
    }

    fn media_files(media: &Path) -> Vec<String> {
        let mut names: Vec<String> = std::fs::read_dir(media)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        names.sort();
        names
    }

    /// The real-render tests need a TeX install; skip them without one.
    fn have_tools(tools: &[&str]) -> bool {
        let found = tools
            .iter()
            .all(|tool| std::env::split_paths(&tex_path()).any(|dir| dir.join(tool).is_file()));
        if !found {
            eprintln!("skipping: {tools:?} not found");
        }
        found
    }

    #[test]
    fn commands_match_latex_py() {
        assert_eq!(
            latex_commands(false),
            [
                &["latex", "-interaction=nonstopmode", "tmp.tex"][..],
                &[
                    "dvipng",
                    "-bg",
                    "Transparent",
                    "-D",
                    "200",
                    "-T",
                    "tight",
                    "tmp.dvi",
                    "-o",
                    "tmp.png"
                ][..],
            ]
        );
        assert_eq!(
            latex_commands(true),
            [
                &["latex", "-interaction=nonstopmode", "tmp.tex"][..],
                &[
                    "dvisvgm",
                    "--no-fonts",
                    "--exact",
                    "-Z",
                    "2",
                    "tmp.dvi",
                    "-o",
                    "tmp.svg"
                ][..],
            ]
        );
    }

    #[test]
    fn tex_file_is_header_body_footer() {
        assert_eq!(
            tex_source("\\begin{document}", "$x$", "\\end{document}"),
            "\\begin{document}\n$x$\n\\end{document}"
        );
    }

    #[test]
    fn path_includes_mac_tex_locations() {
        let path = tex_path();
        let dirs: Vec<_> = std::env::split_paths(&path).collect();
        assert!(dirs.contains(&PathBuf::from("/Library/TeX/texbin")));
        assert!(dirs.contains(&PathBuf::from("/usr/texbin")));
    }

    #[test]
    fn log_is_escaped_like_python_html_escape() {
        assert_eq!(
            html_escape(r#"<a href="x">'&"#),
            "&lt;a href=&quot;x&quot;&gt;&#x27;&amp;"
        );
    }

    #[test]
    fn missing_log_suggests_installing_latex() {
        let dir = tempfile::tempdir().unwrap();
        let log = dir.path().join("latex_log.txt");
        std::fs::write(&log, "").unwrap();
        let tex = dir.path().join("tmp.tex");
        let msg = Failed {
            command: "latex".into(),
            tex: tex.clone(),
            log,
        }
        .html(&I18n::template_only());
        assert_eq!(
            msg,
            format!(
                "Error executing latex.<br>Generated file: {}<br>\
                 Have you installed latex and dvipng/dvisvgm?",
                tex.display()
            )
        );
    }

    #[test]
    fn a_custom_toolchain_makes_the_image() {
        // as "Edit LaTeX toolchain" sets: pdflatex, pdfcrop and sips here
        let fx = collection(true);
        let cfg = LatexConfig {
            toolchain: LatexCommands {
                png: Some(vec![
                    vec!["sh".into(), "-c".into(), "test -s tmp.tex".into()],
                    vec!["sh".into(), "-c".into(), "printf custom > tmp.png".into()],
                ]),
                svg: None,
            },
            ..stock_config(&fx.api)
        };
        let (html, errors) = render_latex(&fx.api, &fx.media, "[$]x[/$]", &cfg);
        assert!(errors.is_empty(), "{errors:?}");
        let files = media_files(&fx.media);
        assert_eq!(files.len(), 1, "{files:?}");
        assert!(html.contains(&format!("src=\"{}\"", files[0])), "{html}");
        assert_eq!(std::fs::read(fx.media.join(&files[0])).unwrap(), b"custom");
    }

    #[test]
    fn a_failing_custom_command_is_named() {
        let fx = collection(true);
        let cfg = LatexConfig {
            toolchain: LatexCommands {
                png: Some(vec![vec!["true".into()], vec!["false".into(), "x".into()]]),
                svg: None,
            },
            ..stock_config(&fx.api)
        };
        let (_, errors) = render_latex(&fx.api, &fx.media, "[$]x[/$]", &cfg);
        assert_eq!(errors.len(), 1, "{errors:?}");
        let err = errors[0].replace(['\u{2068}', '\u{2069}'], "");
        assert!(err.starts_with("Error executing false."), "{err}");
        assert!(media_files(&fx.media).is_empty());
    }

    #[test]
    fn the_toolchain_gives_commands_per_image_kind() {
        let fx = collection(true);
        let names: NotetypeNames = fx.api.call("get_notetype_names", &Empty {}).unwrap();
        let toolchain = LatexCommands {
            png: Some(vec![vec!["pdflatex".into(), "tmp.tex".into()]]),
            svg: None,
        };
        let cfg = latex_config_with(&fx.api, names.entries[0].id, &toolchain).unwrap();
        assert_eq!(cfg.toolchain, toolchain);
        assert_eq!(toolchain.for_kind(false), vec![vec!["pdflatex", "tmp.tex"]]);
        // a kind the toolchain leaves alone keeps latex.py's commands
        assert_eq!(toolchain.for_kind(true)[1][0], "dvisvgm");
        assert_eq!(LatexCommands::default().for_kind(false)[1][0], "dvipng");
    }

    #[test]
    fn notetype_config_comes_from_the_backend() {
        let fx = collection(true);
        let cfg = stock_config(&fx.api);
        assert!(cfg.latex_pre.contains("\\documentclass"), "{cfg:?}");
        assert!(cfg.latex_post.contains("\\end{document}"), "{cfg:?}");
        assert!(!cfg.svg);
    }

    #[test]
    fn disabled_generation_reports_and_stops() {
        let fx = collection(false);
        let cfg = stock_config(&fx.api);
        let (html, errors) = render_latex(&fx.api, &fx.media, "[$]x[/$] [$]y[/$]", &cfg);
        assert!(html.contains("src=\"latex-"), "{html}");
        assert_eq!(
            errors,
            ["LaTeX image generation is disabled in the preferences."]
        );
        assert!(media_files(&fx.media).is_empty());
    }

    #[test]
    fn existing_image_is_not_regenerated() {
        let fx = collection(false);
        let cfg = stock_config(&fx.api);
        let (html, _) = render_latex(&fx.api, &fx.media, "[$]x^2[/$]", &cfg);
        let name = html
            .split("src=\"")
            .nth(1)
            .unwrap()
            .split('"')
            .next()
            .unwrap();
        std::fs::write(fx.media.join(name), b"existing").unwrap();
        // generation is disabled, so any attempt would report an error
        let (again, errors) = render_latex(&fx.api, &fx.media, "[$]x^2[/$]", &cfg);
        assert_eq!(again, html);
        assert!(errors.is_empty(), "{errors:?}");
        assert_eq!(std::fs::read(fx.media.join(name)).unwrap(), b"existing");
    }

    fn render_x_squared(svg: bool) -> (Fixture, String, Vec<String>) {
        let fx = collection(true);
        let cfg = LatexConfig {
            svg,
            ..stock_config(&fx.api)
        };
        let (html, errors) = render_latex(&fx.api, &fx.media, "[$]x^2[/$]", &cfg);
        (fx, html, errors)
    }

    #[test]
    fn renders_png_into_media_folder() {
        if !have_tools(&["latex", "dvipng"]) {
            return;
        }
        let (fx, html, errors) = render_x_squared(false);
        assert!(errors.is_empty(), "{errors:?}");
        let files = media_files(&fx.media);
        assert_eq!(files.len(), 1, "{files:?}");
        let name = &files[0];
        assert!(name.starts_with("latex-") && name.ends_with(".png"));
        assert!(html.contains(&format!("src=\"{name}\"")), "{html}");
        let data = std::fs::read(fx.media.join(name)).unwrap();
        assert!(data.starts_with(b"\x89PNG"));
    }

    #[test]
    fn renders_svg_into_media_folder() {
        if !have_tools(&["latex", "dvisvgm"]) {
            return;
        }
        let (fx, html, errors) = render_x_squared(true);
        assert!(errors.is_empty(), "{errors:?}");
        let files = media_files(&fx.media);
        assert_eq!(files.len(), 1, "{files:?}");
        let name = &files[0];
        assert!(name.starts_with("latex-") && name.ends_with(".svg"));
        assert!(html.contains(&format!("src=\"{name}\"")), "{html}");
        let data = std::fs::read_to_string(fx.media.join(name)).unwrap();
        assert!(data.contains("<svg"));
    }

    #[test]
    fn broken_latex_reports_the_log() {
        if !have_tools(&["latex"]) {
            return;
        }
        let fx = collection(true);
        let cfg = stock_config(&fx.api);
        let (_, errors) = render_latex(&fx.api, &fx.media, "[$]\\nosuchmacro[/$]", &cfg);
        assert_eq!(errors.len(), 1, "{errors:?}");
        // the backend's translations wrap arguments in Unicode isolation marks
        let err = errors[0].replace(['\u{2068}', '\u{2069}'], "");
        assert!(
            err.starts_with("Error executing latex.<br>Generated file: "),
            "{err}"
        );
        assert!(err.contains("tmp.tex<br><small><pre>"), "{err}");
        assert!(err.contains("Undefined control sequence"), "{err}");
        assert!(media_files(&fx.media).is_empty());
    }

    #[test]
    fn preview_without_latex_or_with_generation_off_explains() {
        let fx = collection(false);
        let cfg = stock_config(&fx.api);
        let none = render_preview(&fx.api, Some(&fx.media), &cfg, "inline", "", &[]);
        assert_eq!(none, Err("No LaTeX to preview".into()));
        let off = render_preview(&fx.api, Some(&fx.media), &cfg, "inline", "x^2", &[]);
        assert!(off.unwrap_err().contains("LaTeX"));
    }

    #[test]
    fn preview_of_unsaved_latex_is_a_data_url() {
        if !have_tools(&["latex", "dvipng"]) {
            return;
        }
        let fx = collection(true);
        let cfg = stock_config(&fx.api);
        let p = render_preview(&fx.api, Some(&fx.media), &cfg, "display", "x^2", &[]).unwrap();
        assert!(p.src.starts_with("data:image/png;base64,"), "{}", p.src);
        // as in Qt, the alt text is the LaTeX as rendered
        assert_eq!(p.alt, "\\begin{displaymath}x^2\\end{displaymath}");
        assert!(media_files(&fx.media).is_empty());
    }

    #[test]
    fn preview_of_saved_latex_goes_to_the_media_folder() {
        if !have_tools(&["latex", "dvipng"]) {
            return;
        }
        let fx = collection(true);
        let cfg = stock_config(&fx.api);
        let saved = vec!["before [$]x^2[/$]".to_string()];
        let p = render_preview(&fx.api, Some(&fx.media), &cfg, "inline", "x^2", &saved).unwrap();
        assert_eq!(media_files(&fx.media), vec![p.src.clone()]);
        assert!(p.src.starts_with("latex-"));
    }

    #[test]
    fn broken_preview_reports_the_log_as_text() {
        if !have_tools(&["latex"]) {
            return;
        }
        let fx = collection(true);
        let cfg = stock_config(&fx.api);
        let err =
            render_preview(&fx.api, Some(&fx.media), &cfg, "inline", "\\nosuch", &[]).unwrap_err();
        assert!(err.contains("Undefined control sequence"), "{err}");
        assert!(!err.contains("<br>"), "{err}");
    }
}
