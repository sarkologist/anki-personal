// Copyright: Ankitects Pty Ltd and contributors
// License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

//! `pycmd`/`bridgeCommand`: pages send strings like `key:0:123:text` or
//! `domDone`. Handlers are registered by command name (the part before the
//! first `:`) and get the rest. Unknown commands are logged and recorded, so
//! upstream page changes that add commands show up (see the plan's risks).

use std::collections::BTreeMap;
use std::collections::HashMap;
use std::sync::Mutex;
use std::sync::RwLock;

use serde_json::Value;

use crate::Shell;

pub type Handler = Box<dyn Fn(&Shell, &str, &str) -> Value + Send + Sync>;

/// Installed in every page before its own scripts. Commands are sent in
/// order (Qt's channel is ordered and the editor relies on it), and `cb`
/// gets the handler's JSON result, as with Qt.
pub fn init_script() -> String {
    format!(
        r#"(() => {{
    // installed in every frame; web frames in cards (a YouTube embed) get
    // nothing
    if (location.protocol !== "anki:") {{
        return;
    }}
    let chain = Promise.resolve();
    const send = (arg) => fetch("/{path}", {{
        method: "POST",
        headers: {{ "Content-Type": "text/plain", "{header}": "1" }},
        body: String(arg),
    }}).then((r) => r.json());
    window.pycmd = window.bridgeCommand = function (arg, cb) {{
        const result = (chain = chain.then(() => send(arg)).catch((e) => {{
            console.error("bridge", arg, e);
            return null;
        }}));
        if (cb) {{
            result.then(cb);
        }}
        return false;
    }};
    window.pycmdPromise = (arg) => new Promise((resolve) => window.pycmd(arg, resolve));
    // The main window's own pages got a capability in their URL, which
    // their API calls must carry; only this document can read it.
    const cap = window.top === window && new URLSearchParams(location.search).get("{cap_param}");
    if (cap) {{
        const fetch = window.fetch.bind(window);
        window.fetch = (input, init) => {{
            const url = new URL(input instanceof Request ? input.url : String(input), location.href);
            if (url.origin === location.origin && url.pathname.startsWith("/_anki/")) {{
                init = {{ ...(init || {{}}) }};
                const headers = new Headers(init.headers || (input instanceof Request ? input.headers : undefined));
                headers.set("{cap_header}", cap);
                init.headers = headers;
            }}
            return fetch(input, init);
        }};
    }}
    if (window.top === window) {{
        // as Qt's bridge script, which runs at DocumentReady
        document.addEventListener("DOMContentLoaded", () => window.pycmd("domDone"));
        // links to the web open in the browser, not in place of the page
        document.addEventListener("click", (event) => {{
            const link = event.target instanceof Element && event.target.closest("a[href]");
            if (!link || event.defaultPrevented) {{
                return;
            }}
            const url = new URL(link.href, location.href);
            if (["http:", "https:", "mailto:"].includes(url.protocol)) {{
                event.preventDefault();
                window.pycmd("openUrl:" + url.href);
            }}
        }});
    }}
}})();"#,
        path = crate::protocol::BRIDGE_PATH,
        header = crate::protocol::BRIDGE_HEADER,
        cap_param = crate::pages::CAP_PARAM,
        cap_header = crate::pages::CAP_HEADER,
    )
}

#[derive(Default)]
pub struct Bridge {
    handlers: RwLock<HashMap<String, Handler>>,
    unknown: Mutex<BTreeMap<String, usize>>,
    /// Commands the shell has asked a page to send back (after a save),
    /// so each runs once: from the page, or after a timeout.
    pending: Mutex<std::collections::HashSet<String>>,
}

impl Bridge {
    pub fn register(
        &self,
        name: &str,
        handler: impl Fn(&Shell, &str, &str) -> Value + Send + Sync + 'static,
    ) {
        self.handlers
            .write()
            .unwrap()
            .insert(name.to_string(), Box::new(handler));
    }

    pub fn dispatch(&self, shell: &Shell, label: &str, cmd: &str) -> Value {
        let (name, rest) = cmd.split_once(':').unwrap_or((cmd, ""));
        if let Some(handler) = self.handlers.read().unwrap().get(name) {
            tracing::trace!(label, name, "bridge command");
            let out = handler(shell, label, rest);
            tracing::trace!(label, name, "bridge command done");
            return out;
        }
        tracing::warn!(label, name, "unknown bridge command");
        *self
            .unknown
            .lock()
            .unwrap()
            .entry(name.to_string())
            .or_default() += 1;
        Value::Null
    }

    pub fn add_pending(&self, cmd: String) {
        self.pending.lock().unwrap().insert(cmd);
    }

    /// Whether `cmd` was pending; it no longer is.
    pub fn take_pending(&self, cmd: &str) -> bool {
        self.pending.lock().unwrap().remove(cmd)
    }

    /// Unhandled command names and how often each was sent.
    pub fn unknown_commands(&self) -> BTreeMap<String, usize> {
        self.unknown.lock().unwrap().clone()
    }
}
