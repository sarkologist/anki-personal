# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import html
import json

from .sanitize import sanitize_html


def surface_body() -> str:
    return """
<style>
#agent-pane {
    box-sizing: border-box;
    min-height: 100vh;
    padding: 10px;
}
.agent-message,
.agent-activity,
.agent-proposal {
    border-bottom: 1px solid var(--border-subtle, #ddd);
    margin: 0 0 10px;
    padding: 0 0 10px;
}
.agent-role {
    color: var(--fg-subtle, #666);
    font-size: 0.82em;
    font-weight: 600;
    margin-bottom: 4px;
}
.agent-body {
    line-height: 1.45;
    overflow-wrap: anywhere;
}
.agent-body > :first-child {
    margin-top: 0;
}
.agent-body > :last-child {
    margin-bottom: 0;
}
.agent-message.user .agent-body {
    white-space: pre-wrap;
}
.agent-activity {
    color: var(--fg-subtle, #666);
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.88em;
}
.agent-activity.agent-activity-compact {
    font-family: inherit;
}
.agent-error .agent-body {
    color: var(--fg-danger, #b00020);
    white-space: pre-wrap;
}
.agent-proposal pre,
.agent-body pre {
    background: var(--canvas-elevated, rgba(0, 0, 0, 0.04));
    border: 1px solid var(--border, #ccc);
    border-radius: 4px;
    overflow-x: auto;
    padding: 8px;
    white-space: pre-wrap;
}
.agent-body code,
.agent-proposal code {
    font-family: ui-monospace, Menlo, Consolas, monospace;
}
.agent-proposal table,
.agent-body table {
    border-collapse: collapse;
}
.agent-proposal td,
.agent-proposal th,
.agent-body td,
.agent-body th {
    border: 1px solid var(--border, #ccc);
    padding: 3px 6px;
}
</style>
<div id="agent-pane">
    <div id="agent-transcript" aria-live="polite"></div>
    <div id="agent-proposal"></div>
</div>
<script>
window.agentPane = (() => {
    const transcript = document.getElementById("agent-transcript");
    const proposal = document.getElementById("agent-proposal");

    async function typeset(node) {
        if (!window.MathJax || !MathJax.startup) {
            return;
        }
        try {
            await MathJax.startup.promise;
            if (MathJax.typesetClear) {
                MathJax.typesetClear([node]);
            }
            await MathJax.typesetPromise([node]);
        } catch (error) {
            console.warn("MathJax render failed", error);
        }
    }

    function appendHtml(parent, html) {
        const template = document.createElement("template");
        template.innerHTML = html;
        const nodes = Array.from(template.content.childNodes);
        parent.appendChild(template.content);
        for (const node of nodes) {
            if (node.nodeType === Node.ELEMENT_NODE) {
                typeset(node);
            }
        }
        window.scrollTo(0, document.body.scrollHeight);
    }

    return {
        appendTranscript(html) {
            appendHtml(transcript, html);
        },
        appendToActivity(id, html) {
            const activity = document.getElementById(id);
            if (!activity) {
                return;
            }
            const body = activity.querySelector(".agent-body") || activity;
            appendHtml(body, html);
        },
        replaceElement(id, html) {
            const element = document.getElementById(id);
            if (!element) {
                return;
            }
            const template = document.createElement("template");
            template.innerHTML = html;
            const replacement = template.content.firstElementChild;
            if (replacement) {
                element.replaceWith(replacement);
                typeset(replacement);
            }
            window.scrollTo(0, document.body.scrollHeight);
        },
        setProposal(html) {
            proposal.innerHTML = html;
            typeset(proposal);
            window.scrollTo(0, document.body.scrollHeight);
        },
        clearProposal() {
            proposal.innerHTML = "";
        },
    };
})();
</script>
"""


def render_user_message(prompt: str) -> str:
    return _message("You", _escaped_text(prompt), css_class="user")


def render_assistant_message(message_html: str, fallback_text: str) -> str:
    body = sanitize_html(message_html).strip()
    if not body:
        body = _escaped_text(fallback_text)
    return _message("Assistant", body, css_class="assistant")


def render_error_message(error: str) -> str:
    return _message("Error", _escaped_text(error), css_class="agent-error")


def render_activity_start(activity_id: str) -> str:
    return f"""
<section class="agent-activity" id="{html.escape(activity_id, quote=True)}">
    <div class="agent-role">Codex</div>
    <div class="agent-body"><div>[Live Codex activity]</div></div>
</section>
"""


def render_activity_line(line: str) -> str:
    return f"<div>{html.escape(line)}</div>"


def render_activity_summary(activity_id: str, summary: str) -> str:
    return f"""
<section class="agent-activity agent-activity-compact" id="{html.escape(activity_id, quote=True)}">
    <div class="agent-role">Codex</div>
    <div class="agent-body">{_escaped_text(summary.strip())}</div>
</section>
"""


def render_proposal_diff(diff: str) -> str:
    return f"""
<section class="agent-proposal">
    <div class="agent-role">Proposed changes</div>
    <pre>{html.escape(diff)}</pre>
</section>
"""


def js_append_transcript(fragment: str) -> str:
    return f"window.agentPane.appendTranscript({json.dumps(fragment)});"


def js_append_to_activity(activity_id: str, fragment: str) -> str:
    return (
        "window.agentPane.appendToActivity("
        f"{json.dumps(activity_id)}, {json.dumps(fragment)});"
    )


def js_replace_element(element_id: str, fragment: str) -> str:
    return (
        "window.agentPane.replaceElement("
        f"{json.dumps(element_id)}, {json.dumps(fragment)});"
    )


def js_set_proposal(fragment: str) -> str:
    return f"window.agentPane.setProposal({json.dumps(fragment)});"


def js_clear_proposal() -> str:
    return "window.agentPane.clearProposal();"


def _message(role: str, body: str, *, css_class: str) -> str:
    return f"""
<section class="agent-message {css_class}">
    <div class="agent-role">{html.escape(role)}</div>
    <div class="agent-body">{body}</div>
</section>
"""


def _escaped_text(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")
