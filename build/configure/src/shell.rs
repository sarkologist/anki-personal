// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use anyhow::Result;
use ninja_gen::cargo::CargoBuild;
use ninja_gen::cargo::RustOutput;
use ninja_gen::command::RunCommand;
use ninja_gen::glob;
use ninja_gen::hashmap;
use ninja_gen::inputs;
use ninja_gen::python::python_format;
use ninja_gen::python::PythonTest;
use ninja_gen::Build;

/// `./ninja shell`: the native macOS shell (see native-shell-port-plan.md),
/// bundled as out/shell/AnkiShell.app with the built web assets.
pub fn build_shell(build: &mut Build) -> Result<()> {
    build.add_action(
        "shell:bin",
        CargoBuild {
            inputs: inputs![glob!["{shell,rslib}/**"], ":rslib:proto"],
            outputs: &[RustOutput::Binary("anki-shell")],
            target: None,
            extra_args: "-p anki-shell",
            release_override: None,
        },
    )?;
    build.add_action(
        "shell",
        RunCommand {
            command: "shell/bundle.sh",
            args: "$bin $builddir/qt/_aqt/data/web $plist $icon $builddir/pylib/anki \
                   $builddir/qt/_aqt/hooks.py $out",
            inputs: hashmap! {
                "bin" => inputs![":shell:bin"],
                "plist" => inputs!["shell/Info.plist"],
                "icon" => inputs!["shell/icons/icon.icns"],
                "" => inputs![
                    ":qt:aqt:data:web",
                    ":pylib:anki",
                    ":qt:aqt:hooks.py",
                    glob!["{shell/plugin_host,pylib/anki,addons/suspended_card_priority}/**"]
                ],
            },
            outputs: hashmap! {
                "out" => vec!["shell/AnkiShell.app/Contents/MacOS/anki-shell"],
            },
        },
    )?;
    Ok(())
}

pub fn check_shell(build: &mut Build) -> Result<()> {
    python_format(
        build,
        "plugin_host",
        inputs![glob!("shell/plugin_host/**/*.py")],
    )?;
    build.add_action(
        "check:pytest:plugin_host",
        PythonTest {
            folder: "shell/plugin_host/tests",
            python_path: &["$builddir/pylib"],
            deps: inputs![
                ":pylib:anki",
                ":qt:aqt:hooks.py",
                glob!["{shell/plugin_host,pylib/anki}/**"]
            ],
        },
    )?;
    Ok(())
}
