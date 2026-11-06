// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! WKWebView calls Tauri doesn't have.

use std::cell::Cell;
use std::ffi::c_void;

use block2::RcBlock;
use objc2::class;
use objc2::msg_send;
use objc2::rc::Retained;
use objc2::runtime::AnyObject;
use objc2::runtime::Bool;
use objc2_foundation::NSData;
use objc2_foundation::NSString;
use serde_json::Value;

/// `NSJSONWritingFragmentsAllowed`
const FRAGMENTS_ALLOWED: usize = 1 << 2;

/// `evaluateJavaScript:completionHandler:`, as Qt's `runJavaScript` with a
/// callback: `done` gets the script's completion value as JSON (null for
/// none, an error, or a value JSON can't hold). Page scripts can't do this
/// themselves: the editor's CSP has no `eval`.
///
/// # Safety
/// `webview` is a WKWebView, and this runs on the main thread.
pub unsafe fn evaluate(webview: *mut c_void, js: &str, done: Box<dyn FnOnce(Value) + Send>) {
    let webview = &*(webview as *const AnyObject);
    let js = NSString::from_str(js);
    let done = Cell::new(Some(done));
    let block = RcBlock::new(move |result: *mut AnyObject, _error: *mut AnyObject| {
        let value = if result.is_null() {
            Value::Null
        } else {
            to_json(&*result).unwrap_or(Value::Null)
        };
        if let Some(done) = done.take() {
            done(value);
        }
    });
    let _: () = msg_send![webview, evaluateJavaScript: &*js, completionHandler: &*block];
}

/// WebKit's result (NSString, NSNumber, NSArray, NSDictionary, NSNull,
/// NSDate) as JSON, through NSJSONSerialization, which checks it first.
unsafe fn to_json(result: &AnyObject) -> Option<Value> {
    let wrapped: Retained<AnyObject> = msg_send![class!(NSArray), arrayWithObject: result];
    let valid: Bool = msg_send![class!(NSJSONSerialization), isValidJSONObject: &*wrapped];
    if !valid.as_bool() {
        return None;
    }
    let data: Option<Retained<NSData>> = msg_send![
        class!(NSJSONSerialization),
        dataWithJSONObject: &*wrapped,
        options: FRAGMENTS_ALLOWED,
        error: std::ptr::null_mut::<*mut AnyObject>()
    ];
    let list: Vec<Value> = serde_json::from_slice(&data?.to_vec()).ok()?;
    list.into_iter().next()
}
