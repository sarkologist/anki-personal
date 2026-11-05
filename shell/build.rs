// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::fmt::Write;
use std::path::PathBuf;

use anki_proto_gen::descriptors_path;
use anki_proto_gen::get_services;
use anyhow::Result;
use prost_reflect::DescriptorPool;

/// Writes the backend method table (snake_case name -> service/method
/// index), the same indices `_backend_generated.py` uses.
fn write_method_table() -> Result<()> {
    // anki_proto is a build dependency so that its build script has written
    // the descriptors by now
    let path = descriptors_path();
    println!("cargo:rerun-if-changed={}", path.display());
    let pool = DescriptorPool::decode(std::fs::read(&path)?.as_ref())?;
    let (_, services) = get_services(&pool);
    let mut out = String::from("pub(crate) static METHODS: &[(&str, u32, u32)] = &[\n");
    for service in &services {
        for method in service.all_methods() {
            writeln!(
                out,
                "    ({:?}, {}, {}),",
                method.name, service.index, method.index
            )?;
        }
    }
    out.push_str("];\n");
    let dest = PathBuf::from(std::env::var("OUT_DIR")?).join("methods.rs");
    std::fs::write(dest, out)?;
    Ok(())
}

fn main() -> Result<()> {
    write_method_table()?;
    #[cfg(target_os = "macos")]
    tauri_build::build();
    Ok(())
}
