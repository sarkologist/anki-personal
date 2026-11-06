// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! NSPasteboard and the other AppKit bits the editor's paste/drop needs:
//! what Qt's `QMimeData`/`QClipboard`, `KeyboardModifiersPressed` and
//! `getFile` give `editor.py`. Pasteboards are safe to read off the main
//! thread; the open panel is run on it.

use std::path::PathBuf;
use std::sync::mpsc;

use objc2::msg_send;
use objc2::rc::Retained;
use objc2::runtime::AnyObject;
use objc2::ClassType;
use objc2::MainThreadMarker;
use objc2_app_kit::NSBitmapImageFileType;
use objc2_app_kit::NSBitmapImageRep;
use objc2_app_kit::NSEvent;
use objc2_app_kit::NSEventModifierFlags;
use objc2_app_kit::NSImageCompressionFactor;
use objc2_app_kit::NSModalResponseOK;
use objc2_app_kit::NSOpenPanel;
use objc2_app_kit::NSPasteboard;
use objc2_app_kit::NSPasteboardType;
use objc2_app_kit::NSPasteboardTypeHTML;
use objc2_app_kit::NSPasteboardTypePNG;
use objc2_app_kit::NSPasteboardTypeString;
use objc2_app_kit::NSPasteboardTypeTIFF;
use objc2_foundation::NSArray;
use objc2_foundation::NSData;
use objc2_foundation::NSDictionary;
use objc2_foundation::NSNumber;
use objc2_foundation::NSString;
use objc2_foundation::NSURL;

/// `_read_pasted_image`'s JPEG quality.
const JPEG_QUALITY: f64 = 0.8;

/// Pasted image data, re-encoded as `_read_pasted_image` does.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Image {
    pub data: Vec<u8>,
    /// "png" or "jpg"
    pub ext: &'static str,
}

/// The flavours of a pasteboard the editor understands: public.html,
/// public.utf8-plain-text, file/web urls and PNG/TIFF images.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Contents {
    pub html: Option<String>,
    pub text: Option<String>,
    pub urls: Vec<String>,
    pub image: Option<Image>,
}

/// The general pasteboard, or a named one.
fn pasteboard(name: Option<&str>) -> Retained<NSPasteboard> {
    match name {
        None => NSPasteboard::generalPasteboard(),
        Some(name) => NSPasteboard::pasteboardWithName(&NSString::from_str(name)),
    }
}

/// What `pasteboard` (the general one for `None`) holds. Images are
/// re-encoded as PNG if `png` (the PASTE_IMAGES_AS_PNG setting), else JPEG.
pub fn read(pasteboard: Option<&str>, png: bool) -> Contents {
    let pb = self::pasteboard(pasteboard);
    // SAFETY: AppKit's constant strings
    let (html, string) = unsafe { (NSPasteboardTypeHTML, NSPasteboardTypeString) };
    Contents {
        html: pb.stringForType(html).map(|s| s.to_string()),
        text: pb.stringForType(string).map(|s| s.to_string()),
        urls: read_urls(&pb),
        image: read_image(&pb, png),
    }
}

/// Just the pasteboard's HTML (cheaper than [read]: no image re-encoding).
pub fn read_html(pasteboard: Option<&str>) -> Option<String> {
    let pb = self::pasteboard(pasteboard);
    // SAFETY: AppKit's constant string
    let html = unsafe { NSPasteboardTypeHTML };
    pb.stringForType(html).map(|s| s.to_string())
}

/// Every item's file or web url (public.file-url / public.url), file
/// reference urls as paths.
fn read_urls(pb: &NSPasteboard) -> Vec<String> {
    let classes = NSArray::from_slice(&[NSURL::class()]);
    // SAFETY: an array of classes and no options, as documented
    let Some(objects) = (unsafe { pb.readObjectsForClasses_options(&classes, None) }) else {
        return vec![];
    };
    objects
        .iter()
        .filter_map(|object| object.downcast::<NSURL>().ok())
        .filter_map(|url| {
            let url = if url.isFileReferenceURL() {
                url.filePathURL()?
            } else {
                url
            };
            url.absoluteString().map(|s| s.to_string())
        })
        .collect()
}

fn read_image(pb: &NSPasteboard, png: bool) -> Option<Image> {
    // SAFETY: AppKit's constant strings
    let types: [&NSPasteboardType; 2] = unsafe { [NSPasteboardTypePNG, NSPasteboardTypeTIFF] };
    let data = types.iter().find_map(|kind| pb.dataForType(kind))?;
    let (kind, ext, properties) = if png {
        (NSBitmapImageFileType::PNG, "png", NSDictionary::new())
    } else {
        let quality = NSNumber::new_f64(JPEG_QUALITY);
        let properties = NSDictionary::from_slices(
            // SAFETY: AppKit's constant string
            &[unsafe { NSImageCompressionFactor }],
            &[&*quality as &AnyObject],
        );
        (NSBitmapImageFileType::JPEG, "jpg", properties)
    };
    let rep = NSBitmapImageRep::imageRepWithData(&data)?;
    // SAFETY: the properties dictionary maps property keys to NSNumbers
    let encoded = unsafe { rep.representationUsingType_properties(kind, &properties) }?;
    Some(Image {
        data: encoded.to_vec(),
        ext,
    })
}

/// `changeCount`, which goes up by one each time the pasteboard's contents
/// are replaced.
pub fn change_count(pasteboard: Option<&str>) -> isize {
    self::pasteboard(pasteboard).changeCount()
}

/// Whether shift is held now (`KeyboardModifiersPressed().shift`).
pub fn shift_held() -> bool {
    NSEvent::modifierFlags_class().contains(NSEventModifierFlags::Shift)
}

/// `QFileDialog.getExistingDirectory`: a modal open panel for a folder,
/// starting in `folder`. Blocks as [choose_file].
pub fn choose_folder(app: &tauri::AppHandle, title: &str, folder: &str) -> Option<PathBuf> {
    let title = title.to_string();
    let folder = folder.to_string();
    on_main(app, move || {
        let mtm = MainThreadMarker::new().expect("on the main thread");
        let panel = NSOpenPanel::openPanel(mtm);
        panel.setCanChooseFiles(false);
        panel.setCanChooseDirectories(true);
        panel.setCanCreateDirectories(true);
        panel.setAllowsMultipleSelection(false);
        let title = NSString::from_str(&title);
        panel.setTitle(Some(&title));
        panel.setMessage(Some(&title));
        if !folder.is_empty() {
            if let Some(url) = NSURL::from_directory_path(&folder) {
                panel.setDirectoryURL(Some(&url));
            }
        }
        if panel.runModal() != NSModalResponseOK {
            return None;
        }
        panel.URL()?.to_file_path()
    })
}

/// Run `f` on the main thread and wait for it (or run it, if this is it).
fn on_main<T: Send + 'static>(
    app: &tauri::AppHandle,
    f: impl FnOnce() -> Option<T> + Send + 'static,
) -> Option<T> {
    if MainThreadMarker::new().is_some() {
        return f();
    }
    let (tx, rx) = mpsc::channel();
    app.run_on_main_thread(move || {
        let _ = tx.send(f());
    })
    .ok()?;
    rx.recv().ok().flatten()
}

/// `getFile`: a modal open panel for one file with one of `extensions`.
/// Blocks until it closes; call it off the main thread (on it, the panel
/// is run directly).
pub fn choose_file(app: &tauri::AppHandle, title: &str, extensions: &[&str]) -> Option<PathBuf> {
    let title = title.to_string();
    let extensions: Vec<String> = extensions.iter().map(|e| e.to_string()).collect();
    let run = move || {
        let mtm = MainThreadMarker::new().expect("on the main thread");
        let panel = NSOpenPanel::openPanel(mtm);
        panel.setCanChooseFiles(true);
        panel.setCanChooseDirectories(false);
        panel.setAllowsMultipleSelection(false);
        let title = NSString::from_str(&title);
        panel.setTitle(Some(&title));
        panel.setMessage(Some(&title));
        let extensions: Vec<_> = extensions.iter().map(|e| NSString::from_str(e)).collect();
        // allowedContentTypes needs UniformTypeIdentifiers
        #[allow(deprecated)]
        panel.setAllowedFileTypes(Some(&NSArray::from_retained_slice(&extensions)));
        if panel.runModal() != NSModalResponseOK {
            return None;
        }
        panel.URL()?.to_file_path()
    };
    on_main(app, run)
}

/// A uniquely named pasteboard, released on drop: for tests, which must
/// never touch the general pasteboard.
pub struct ScratchPasteboard {
    pasteboard: Retained<NSPasteboard>,
    name: String,
}

impl ScratchPasteboard {
    #[allow(clippy::new_without_default)]
    pub fn new() -> Self {
        let pasteboard = NSPasteboard::pasteboardWithUniqueName();
        let name = pasteboard.name().to_string();
        ScratchPasteboard { pasteboard, name }
    }

    /// For [`read`] and [`change_count`].
    pub fn name(&self) -> &str {
        &self.name
    }

    /// Replace the contents, as one change.
    pub fn write(&self, html: Option<&str>, text: Option<&str>, urls: &[&str], png: Option<&[u8]>) {
        let pb = &self.pasteboard;
        pb.clearContents();
        let urls: Vec<_> = urls
            .iter()
            .filter_map(|url| NSURL::URLWithString(&NSString::from_str(url)))
            .map(objc2::runtime::ProtocolObject::from_retained)
            .collect();
        if !urls.is_empty() {
            pb.writeObjects(&NSArray::from_retained_slice(&urls));
        }
        // SAFETY: AppKit's constant strings
        let (html_type, string_type, png_type) = unsafe {
            (
                NSPasteboardTypeHTML,
                NSPasteboardTypeString,
                NSPasteboardTypePNG,
            )
        };
        if let Some(html) = html {
            pb.setString_forType(&NSString::from_str(html), html_type);
        }
        if let Some(text) = text {
            pb.setString_forType(&NSString::from_str(text), string_type);
        }
        if let Some(png) = png {
            pb.setData_forType(Some(&NSData::with_bytes(png)), png_type);
        }
    }
}

// SAFETY: NSPasteboard is used from any thread here (see `read`); the
// struct only holds the pasteboard and its name
unsafe impl Send for ScratchPasteboard {}
unsafe impl Sync for ScratchPasteboard {}

impl Drop for ScratchPasteboard {
    fn drop(&mut self) {
        // SAFETY: releaseGlobally takes no arguments and returns nothing
        let _: () = unsafe { msg_send![&*self.pasteboard, releaseGlobally] };
    }
}

#[cfg(test)]
mod test {
    use super::*;

    /// A 2x1 PNG: one opaque red pixel, one opaque blue.
    const PNG: &[u8] = &[
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44,
        0x52, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x01, 0x08, 0x02, 0x00, 0x00, 0x00, 0x7b,
        0x40, 0xe8, 0xdd, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9c, 0x63, 0xf8,
        0xcf, 0x00, 0x04, 0xff, 0x01, 0x07, 0x00, 0x01, 0xff, 0xe2, 0x23, 0x9e, 0x59, 0x00, 0x00,
        0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
    ];

    #[test]
    fn reads_back_what_was_written() {
        let pb = ScratchPasteboard::new();
        assert!(!pb.name().is_empty());
        let before = change_count(Some(pb.name()));
        pb.write(
            Some("<b>ü</b>"),
            Some("plain ü"),
            &["file:///tmp/a%20b.png"],
            Some(PNG),
        );
        assert_eq!(change_count(Some(pb.name())), before + 1);

        let contents = read(Some(pb.name()), true);
        assert_eq!(contents.html.as_deref(), Some("<b>ü</b>"));
        assert_eq!(contents.text.as_deref(), Some("plain ü"));
        assert_eq!(contents.urls, ["file:///tmp/a%20b.png"]);
        let image = contents.image.unwrap();
        assert_eq!(image.ext, "png");
        assert!(image.data.starts_with(b"\x89PNG"));
    }

    #[test]
    fn images_are_jpeg_unless_png_is_wanted() {
        let pb = ScratchPasteboard::new();
        pb.write(None, None, &[], Some(PNG));
        let image = read(Some(pb.name()), false).image.unwrap();
        assert_eq!(image.ext, "jpg");
        assert!(image.data.starts_with(&[0xff, 0xd8, 0xff]), "JPEG magic");
    }

    #[test]
    fn several_urls_and_web_urls() {
        let pb = ScratchPasteboard::new();
        pb.write(
            None,
            None,
            &["https://example.com/a?b=c", "file:///tmp/x.mp3"],
            None,
        );
        let contents = read(Some(pb.name()), false);
        assert_eq!(
            contents.urls,
            ["https://example.com/a?b=c", "file:///tmp/x.mp3"]
        );
        assert_eq!(contents.image, None);
    }

    #[test]
    fn text_that_looks_like_a_url_is_only_text() {
        let pb = ScratchPasteboard::new();
        pb.write(None, Some("https://example.com/"), &[], None);
        let contents = read(Some(pb.name()), false);
        assert_eq!(contents.text.as_deref(), Some("https://example.com/"));
        assert!(contents.urls.is_empty());
        assert_eq!(contents.html, None);
    }

    #[test]
    fn empty_pasteboard_reads_as_nothing() {
        let pb = ScratchPasteboard::new();
        assert_eq!(read(Some(pb.name()), false), Contents::default());
    }

    #[test]
    fn scratch_pasteboards_are_distinct() {
        let a = ScratchPasteboard::new();
        let b = ScratchPasteboard::new();
        assert_ne!(a.name(), b.name());
        a.write(None, Some("a"), &[], None);
        assert_eq!(read(Some(b.name()), false).text, None);
    }
}
