# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import difflib
import html
import json
from collections.abc import Callable, Iterable

from .patches import (
    EditorSnapshot,
    MultiCardSnapshot,
    MultiNotePatch,
    MultiNoteUpdate,
    NotePatch,
    SelectedNoteSnapshot,
    SelectedTextSnapshot,
)
from .sanitize import sanitize_html

PreviewRenderer = Callable[[str], str]
SELECTION_CONTEXT_EXCERPT_MAX_CHARS = 120


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
.agent-selection-context {
    color: var(--fg-subtle, #666);
    font-size: 0.88em;
    margin-top: 6px;
    white-space: normal;
}
.agent-activity {
    color: var(--fg-subtle, #666);
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.88em;
}
.agent-activity.agent-activity-compact {
    font-family: inherit;
}
.agent-activity-details summary {
    cursor: pointer;
    overflow-wrap: anywhere;
}
.agent-activity-details .agent-body {
    font-family: ui-monospace, Menlo, Consolas, monospace;
    margin-top: 6px;
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
.agent-proposal-summary {
    margin: 0 0 10px;
}
.agent-change {
    border-top: 1px solid var(--border-subtle, #ddd);
    padding-top: 10px;
}
.agent-change + .agent-change {
    margin-top: 12px;
}
.agent-change-title {
    font-weight: 600;
    margin-bottom: 8px;
}
.agent-preview-grid {
    display: grid;
    gap: 8px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
}
.agent-preview-heading,
.agent-diff-heading {
    color: var(--fg-subtle, #666);
    font-size: 0.82em;
    font-weight: 600;
    margin-bottom: 4px;
}
.agent-preview-body {
    border: 1px solid var(--border, #ccc);
    border-radius: 4px;
    min-height: 32px;
    overflow-wrap: anywhere;
    padding: 8px;
}
.agent-tag-list {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
}
.agent-tag {
    background: var(--canvas-elevated, rgba(0, 0, 0, 0.04));
    border: 1px solid var(--border, #ccc);
    border-radius: 4px;
    padding: 2px 5px;
}
.agent-empty {
    color: var(--fg-subtle, #666);
    font-style: italic;
}
.agent-latex-error {
    color: var(--fg-danger, #b00020);
    margin-top: 8px;
}
.agent-unified-diff {
    border: 1px solid var(--border, #ccc);
    border-radius: 4px;
    font-family: ui-monospace, Menlo, Consolas, monospace;
    font-size: 0.9em;
    margin-top: 8px;
    overflow-x: auto;
}
.agent-diff-row {
    margin: 0;
    padding: 1px 8px;
    white-space: pre-wrap;
}
.agent-unified-html-diff {
    font-family: inherit;
}
.agent-unified-html-diff .agent-diff-file,
.agent-unified-html-diff .agent-diff-hunk,
.agent-diff-marker {
    font-family: ui-monospace, Menlo, Consolas, monospace;
}
.agent-diff-rich {
    column-gap: 4px;
    display: grid;
    grid-template-columns: 2ch minmax(0, 1fr);
    white-space: normal;
}
.agent-diff-marker {
    white-space: pre;
}
.agent-diff-content {
    min-width: 0;
    overflow-wrap: anywhere;
}
.agent-diff-content > :first-child {
    margin-top: 0;
}
.agent-diff-content > :last-child {
    margin-bottom: 0;
}
.agent-diff-add {
    background: rgba(50, 160, 90, 0.16);
}
.agent-diff-del {
    background: rgba(210, 70, 70, 0.16);
}
.agent-diff-hunk,
.agent-diff-file {
    color: var(--fg-subtle, #666);
    font-weight: 600;
}
@media (max-width: 520px) {
    .agent-preview-grid {
        grid-template-columns: 1fr;
    }
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
        clearTranscript() {
            transcript.innerHTML = "";
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


def selection_context_excerpt(
    text: str,
    *,
    max_chars: int = SELECTION_CONTEXT_EXCERPT_MAX_CHARS,
) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(0, max_chars - 3)].rstrip() + "..."


def selection_context_label_text(selected_text: SelectedTextSnapshot) -> str:
    return (
        f"{selected_text.field_name} - "
        f'"{selection_context_excerpt(selected_text.text)}"'
    )


def render_user_message(
    prompt: str,
    selected_text: SelectedTextSnapshot | None = None,
) -> str:
    body = _escaped_text(prompt)
    if selected_text is not None:
        body += _render_selection_context(selected_text)
    return _message("You", body, css_class="user")


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


def render_activity_summary(
    activity_id: str,
    summary: str,
    detail_lines: Iterable[str] = (),
) -> str:
    details = "".join(render_activity_line(line) for line in detail_lines)
    return f"""
<section class="agent-activity agent-activity-compact" id="{html.escape(activity_id, quote=True)}">
    <div class="agent-role">Codex</div>
    <details class="agent-activity-details">
        <summary>{html.escape(summary.strip())}</summary>
        <div class="agent-body">{details}</div>
    </details>
</section>
"""


def render_proposal_diff(
    snapshot: EditorSnapshot,
    patch: NotePatch,
    preview_renderer: PreviewRenderer | None = None,
) -> str:
    render_preview = preview_renderer or sanitize_html
    changes = [
        _render_field_change(snapshot, field_name, new_html, render_preview)
        for field_name, new_html in patch.field_updates.items()
    ]
    if patch.tag_patch.has_changes():
        changes.append(_render_tag_change(snapshot, patch))

    return f"""
<section class="agent-proposal">
    <div class="agent-role">Proposed changes</div>
    <div class="agent-proposal-summary">{html.escape(patch.summary)}</div>
    {"".join(changes)}
</section>
"""


def multi_note_patch_card_ids(
    snapshot: MultiCardSnapshot,
    patch: MultiNotePatch,
) -> tuple[int, ...]:
    affected = set(patch.affected_note_ids())
    return tuple(card.card_id for card in snapshot.cards if card.note_id in affected)


def render_multi_note_card_proposal_diff(
    snapshot: MultiCardSnapshot,
    patch: MultiNotePatch,
    card_id: int,
    preview_renderer: PreviewRenderer | None = None,
) -> str:
    render_preview = preview_renderer or sanitize_html
    card = snapshot.card_by_id(card_id)
    note = snapshot.note_by_id(card.note_id)
    update = patch.update_for_note(card.note_id)
    if update is None:
        changes = '<div class="agent-empty">No proposal for this card.</div>'
    else:
        changes = _render_multi_note_update_change(note, update, render_preview)
    sibling_count = len(snapshot.cards_for_note(card.note_id))
    sibling_text = (
        f"{sibling_count} selected sibling cards share this note"
        if sibling_count > 1
        else "1 selected card uses this note"
    )

    return f"""
<section class="agent-proposal">
    <div class="agent-role">Proposed changes</div>
    <div class="agent-proposal-summary">{html.escape(patch.summary)}</div>
    <div class="agent-change-title">
        Card {html.escape(str(card.card_id))} - {html.escape(card.template_name)}
        - note {html.escape(str(note.note_id))}
    </div>
    <div class="agent-selection-context">{html.escape(sibling_text)}</div>
    {changes}
</section>
"""


def js_append_transcript(fragment: str) -> str:
    return f"window.agentPane.appendTranscript({json.dumps(fragment)});"


def js_clear_transcript() -> str:
    return "window.agentPane.clearTranscript();"


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


def js_apply_agent_proposal(
    field_updates: Iterable[dict[str, object]],
    tags: list[str] | None,
) -> str:
    payload = {
        "fields": list(field_updates),
        "tags": tags,
    }
    return f"applyAgentProposal({json.dumps(payload)});"


def _message(role: str, body: str, *, css_class: str) -> str:
    return f"""
<section class="agent-message {css_class}">
    <div class="agent-role">{html.escape(role)}</div>
    <div class="agent-body">{body}</div>
</section>
"""


def _escaped_text(text: str) -> str:
    return html.escape(text).replace("\n", "<br>")


def _render_selection_context(selected_text: SelectedTextSnapshot) -> str:
    return (
        '<div class="agent-selection-context">'
        f"Selection sent from {html.escape(selected_text.field_name)}: "
        f'"{html.escape(selection_context_excerpt(selected_text.text))}"'
        "</div>"
    )


def _render_field_change(
    snapshot: EditorSnapshot,
    field_name: str,
    new_html: str,
    render_preview: PreviewRenderer,
) -> str:
    old_html = snapshot.field_html(field_name)
    diff = difflib.unified_diff(
        old_html.splitlines(),
        new_html.splitlines(),
        fromfile="current",
        tofile="proposed",
        lineterm="",
    )
    return f"""
<section class="agent-change">
    <div class="agent-change-title">Field: {html.escape(field_name)}</div>
    {_render_preview_grid(render_preview(old_html), render_preview(new_html))}
    {_render_unified_diff(diff, render_preview)}
</section>
"""


def _render_tag_change(snapshot: EditorSnapshot, patch: NotePatch) -> str:
    old_tags = tuple(snapshot.tags)
    new_tags = patch.tag_patch.apply(old_tags)
    old_text = " ".join(old_tags)
    new_text = " ".join(new_tags)
    diff = difflib.unified_diff(
        [old_text],
        [new_text],
        fromfile="current",
        tofile="proposed",
        lineterm="",
    )
    return f"""
<section class="agent-change">
    <div class="agent-change-title">Tags</div>
    {_render_preview_grid(_render_tags(old_tags), _render_tags(new_tags))}
    {_render_unified_diff(diff)}
</section>
"""


def _render_multi_note_update_change(
    note: SelectedNoteSnapshot,
    update: MultiNoteUpdate,
    render_preview: PreviewRenderer,
) -> str:
    changes = [
        _render_selected_note_field_change(note, field_name, new_html, render_preview)
        for field_name, new_html in update.field_updates.items()
    ]
    if update.tag_patch.has_changes():
        changes.append(_render_selected_note_tag_change(note, update))
    return "".join(changes)


def _render_selected_note_field_change(
    note: SelectedNoteSnapshot,
    field_name: str,
    new_html: str,
    render_preview: PreviewRenderer,
) -> str:
    old_html = note.field_html(field_name)
    diff = difflib.unified_diff(
        old_html.splitlines(),
        new_html.splitlines(),
        fromfile="current",
        tofile="proposed",
        lineterm="",
    )
    return f"""
<section class="agent-change">
    <div class="agent-change-title">Field: {html.escape(field_name)}</div>
    {_render_preview_grid(render_preview(old_html), render_preview(new_html))}
    {_render_unified_diff(diff, render_preview)}
</section>
"""


def _render_selected_note_tag_change(
    note: SelectedNoteSnapshot,
    update: MultiNoteUpdate,
) -> str:
    old_tags = tuple(note.tags)
    new_tags = update.tag_patch.apply(old_tags)
    old_text = " ".join(old_tags)
    new_text = " ".join(new_tags)
    diff = difflib.unified_diff(
        [old_text],
        [new_text],
        fromfile="current",
        tofile="proposed",
        lineterm="",
    )
    return f"""
<section class="agent-change">
    <div class="agent-change-title">Tags</div>
    {_render_preview_grid(_render_tags(old_tags), _render_tags(new_tags))}
    {_render_unified_diff(diff)}
</section>
"""


def _render_preview_grid(current: str, proposed: str) -> str:
    return f"""
<div class="agent-preview-grid">
    <div class="agent-preview-pane">
        <div class="agent-preview-heading">Current</div>
        <div class="agent-preview-body">{current or _empty_preview()}</div>
    </div>
    <div class="agent-preview-pane">
        <div class="agent-preview-heading">Proposed</div>
        <div class="agent-preview-body">{proposed or _empty_preview()}</div>
    </div>
</div>
"""


def _render_tags(tags: tuple[str, ...]) -> str:
    if not tags:
        return _empty_preview()
    return (
        '<div class="agent-tag-list">'
        + "".join(f'<span class="agent-tag">{html.escape(tag)}</span>' for tag in tags)
        + "</div>"
    )


def _render_unified_diff(
    lines: Iterable[str],
    render_content: PreviewRenderer | None = None,
) -> str:
    rows = "".join(_render_diff_line(line, render_content) for line in lines)
    if not rows:
        rows = '<div class="agent-diff-row agent-diff-context">No textual differences.</div>'
    container_class = "agent-unified-diff"
    if render_content is not None:
        container_class += " agent-unified-html-diff"
    return f"""
<div class="agent-diff-heading">Diff</div>
<div class="{container_class}">{rows}</div>
"""


def _render_diff_line(
    line: str,
    render_content: PreviewRenderer | None = None,
) -> str:
    if line.startswith(("--- ", "+++ ")):
        css_class = "agent-diff-file"
    elif line.startswith("@@"):
        css_class = "agent-diff-hunk"
    elif line.startswith("+"):
        css_class = "agent-diff-add"
    elif line.startswith("-"):
        css_class = "agent-diff-del"
    else:
        css_class = "agent-diff-context"
    if (
        render_content is not None
        and css_class in {"agent-diff-add", "agent-diff-del", "agent-diff-context"}
        and not line.startswith("\\")
    ):
        return _render_html_diff_line(line, css_class, render_content)
    return f'<div class="agent-diff-row {css_class}">{html.escape(line)}</div>'


def _render_html_diff_line(
    line: str,
    css_class: str,
    render_content: PreviewRenderer,
) -> str:
    marker = ""
    content = line
    if line.startswith(("+", "-", " ")):
        marker = line[0]
        content = line[1:]
    rendered_content = render_content(content) if content else "&nbsp;"
    return (
        f'<div class="agent-diff-row {css_class} agent-diff-rich">'
        f'<span class="agent-diff-marker">{html.escape(marker)}</span>'
        f'<div class="agent-diff-content">{rendered_content or "&nbsp;"}</div>'
        "</div>"
    )


def _empty_preview() -> str:
    return '<span class="agent-empty">(empty)</span>'
