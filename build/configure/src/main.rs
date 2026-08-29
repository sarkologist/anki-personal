// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

mod aqt;
mod audio;
mod installer;
mod launcher;
mod platform;
mod pylib;
mod python;
mod rust;
mod web;

use std::env;

use anyhow::Context;
use anyhow::Result;
use aqt::build_and_check_aqt;
use audio::build_audio;
use installer::build_installer;
use launcher::build_launcher;
use ninja_gen::glob;
use ninja_gen::inputs;
use ninja_gen::protobuf::check_proto;
use ninja_gen::protobuf::setup_protoc;
use ninja_gen::python::setup_uv;
use ninja_gen::Build;
use platform::overriden_python_venv_platform;
use pylib::build_pylib;
use pylib::check_pylib;
use python::check_python;
use python::setup_venv;
use rust::build_rust;
use rust::check_minilints;
use rust::check_rust;
use web::build_and_check_web;
use web::check_sql;

fn anki_version() -> String {
    std::fs::read_to_string(".version")
        .unwrap()
        .trim()
        .to_string()
}

/// Neither n2 nor ninja prunes outputs a previous configuration produced, and
/// the wheels package everything under out/qt/_aqt, so a file that stops being
/// built lingers in existing build folders - and in wheels built from them -
/// until we delete it ourselves. This runs when build.ninja is regenerated,
/// which is whenever the list below changes.
fn remove_obsolete_outputs(build: &Build) -> Result<()> {
    for relative_path in [
        // ts/editable is bundled for its CSS only; it used to be built as a page
        "qt/_aqt/data/web/pages/editable.js",
        "qt/_aqt/data/web/pages/editable.css",
        "ts/editable/editable.js",
        // MathJax a11y components we no longer vendor: they can't load, as their
        // own dependencies (input/mml.js, a11y/sre.js) were never shipped
        "qt/_aqt/data/web/js/vendor/mathjax/a11y/complexity.js",
        "qt/_aqt/data/web/js/vendor/mathjax/a11y/explorer.js",
        "qt/_aqt/data/web/js/vendor/mathjax/a11y/semantic-enrich.js",
    ] {
        let path = build.buildroot.join(relative_path);
        if path.exists() {
            std::fs::remove_file(&path).with_context(|| format!("removing {path}"))?;
        }
    }
    // the mathmaps under it are only read by the a11y/sre.js we never shipped
    let sre = build
        .buildroot
        .join("qt/_aqt/data/web/js/vendor/mathjax/sre");
    if sre.exists() {
        std::fs::remove_dir_all(&sre).with_context(|| format!("removing {sre}"))?;
    }
    Ok(())
}

fn main() -> Result<()> {
    let mut build = Build::new()?;
    let build = &mut build;

    remove_obsolete_outputs(build)?;

    setup_protoc(build)?;
    check_proto(build, inputs![glob!["proto/**/*.proto"]])?;

    if env::var("OFFLINE_BUILD").is_err() {
        setup_uv(
            build,
            overriden_python_venv_platform().unwrap_or(build.host_platform),
        )?;
    }
    setup_venv(build)?;

    build_rust(build)?;
    build_pylib(build)?;
    build_and_check_web(build)?;
    build_and_check_aqt(build)?;

    if env::var("OFFLINE_BUILD").is_err() {
        build_launcher(build)?;
        build_installer(build)?;
        build_audio(build)?;
    }

    check_rust(build)?;
    check_pylib(build)?;
    check_python(build)?;

    check_sql(build)?;
    check_minilints(build)?;

    build.trailing_text = "default pylib qt\n".into();

    build.write_build_file()?;

    Ok(())
}
