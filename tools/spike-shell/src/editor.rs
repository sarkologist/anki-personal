// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! Load a real note into the stock editor page, as `Editor.loadNote` does.

use serde_json::json;
use serde_json::Value;

use crate::Driver;

pub(crate) fn run(driver: &Driver) {
    let note: anki_proto::notes::Note = driver
        .api
        .call("get_note", &anki_proto::notes::NoteId { nid: driver.note });
    let notetype: anki_proto::generic::Json = driver.api.call(
        "get_notetype_legacy",
        &anki_proto::notetypes::NotetypeId {
            ntid: note.notetype_id,
        },
    );
    let nt: Value = serde_json::from_slice(&notetype.json).unwrap();
    let flds = nt["flds"].as_array().unwrap();
    let names: Vec<&str> = flds.iter().map(|f| f["name"].as_str().unwrap()).collect();
    let fields: Vec<Value> = names
        .iter()
        .zip(&note.fields)
        .map(|(n, v)| json!([n, v]))
        .collect();
    let is_cloze = nt["type"].as_i64() == Some(1);
    let templates: Vec<Value> = nt["tmpls"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| json!([t["qfmt"], t["afmt"]]))
        .collect();
    let qfmt = templates
        .first()
        .map(|t| t[0].as_str().unwrap_or("").to_string())
        .unwrap_or_default();
    let cloze: Vec<bool> = names
        .iter()
        .map(|n| is_cloze && qfmt.contains(&format!("cloze:{n}}}")))
        .collect();
    let per_field = |key: &str, default: Value| -> Vec<Value> {
        flds.iter()
            .map(|f| f.get(key).cloned().unwrap_or(default.clone()))
            .collect()
    };
    let fonts: Vec<Value> = flds
        .iter()
        .map(|f| json!([f["font"], f["size"], f["rtl"]]))
        .collect();

    driver.eval("setupEditor('add')".into());
    let js = format!(
        r##"require("anki/ui").loaded.then(async () => {{
            saveSession();
            setMathjaxEnabled(true);
            setIsImageOcclusion(false);
            setNotetypeMeta({meta});
            await setNotetypeTemplates({templates});
            setNotetypeCss({css});
            setFields({fields});
            setCollapsed({collapsed});
            setClozeFields({cloze});
            setPlainTexts({plain});
            setDescriptions({descriptions});
            setFonts({fonts});
            setNoteId({nid});
            setColorButtons(["#0000ff", "#0000ff"]);
            setTags({tags});
            setTagsCollapsed(false);
            setShrinkImages(true);
            setCloseHTMLTags(true);
            requestAnimationFrame(() => requestAnimationFrame(() => pycmd("spike:note loaded")));
        }})"##,
        meta = json!({"id": note.notetype_id, "modTime": nt["mod"]}),
        templates = Value::Array(templates),
        css = nt["css"],
        fields = Value::Array(fields),
        collapsed = Value::Array(per_field("collapsed", json!(false))),
        cloze = json!(cloze),
        plain = Value::Array(per_field("plainText", json!(false))),
        descriptions = Value::Array(per_field("description", json!(""))),
        fonts = Value::Array(fonts),
        nid = note.id,
        tags = json!(note.tags),
    );
    driver.eval(js);
    std::thread::sleep(std::time::Duration::from_secs(3));
    driver.eval(format!(
        "pycmd('spike:' + {})",
        include_str!("../probe-editor.js")
            .trim_end()
            .trim_end_matches(';')
    ));
    std::thread::sleep(std::time::Duration::from_millis(500));
    driver.eval(
        "window.__spikeReport = (m) => pycmd('spike:' + m);".to_string()
            + include_str!("../bench-typing.js"),
    );
}
