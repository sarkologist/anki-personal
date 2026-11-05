// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

#[cfg(target_os = "macos")]
fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "anki_shell=info,warn".into()),
        )
        .with_writer(std::io::stderr)
        .init();
    let args = anki_shell::app::Args::parse(std::env::args().skip(1))?;
    anki_shell::app::run(args)
}

#[cfg(not(target_os = "macos"))]
fn main() {
    eprintln!("the native shell is macOS only");
    std::process::exit(1);
}
