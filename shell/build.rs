// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

use std::fmt::Write;
use std::path::PathBuf;

use anki_proto_gen::descriptors_path;
use anki_proto_gen::get_services;
use anyhow::Result;
use prost_reflect::DescriptorPool;

/// Where a method's output has its `OpChanges`: -1 if it has none, 0 if
/// the output is one, else the number of its `changes` field.
fn changes_field(method: &anki_proto_gen::Method) -> i32 {
    const OP_CHANGES: &str = "anki.collection.OpChanges";
    let output = method.proto.output();
    if output.full_name() == OP_CHANGES {
        return 0;
    }
    output
        .get_field_by_name("changes")
        .filter(|f| f.kind().as_message().map(|m| m.full_name()) == Some(OP_CHANGES))
        .map_or(-1, |f| f.number() as i32)
}

/// Writes the backend method table (snake_case name -> service/method
/// index, and where its output's `OpChanges` are), with the same indices
/// `_backend_generated.py` uses.
fn write_method_table() -> Result<()> {
    // anki_proto is a build dependency so that its build script has written
    // the descriptors by now
    let path = descriptors_path();
    println!("cargo:rerun-if-changed={}", path.display());
    let pool = DescriptorPool::decode(std::fs::read(&path)?.as_ref())?;
    let (_, services) = get_services(&pool);
    let mut out = String::from("pub(crate) static METHODS: &[(&str, u32, u32, i32)] = &[\n");
    for service in &services {
        for method in service.all_methods() {
            writeln!(
                out,
                "    ({:?}, {}, {}, {}),",
                method.name,
                service.index,
                method.index,
                changes_field(method)
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
    tauri_build::build();
    Ok(())
}
