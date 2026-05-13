# Editor Agent Pane

Repo-local Anki add-on prototype for an editor-side Codex chat pane.

To try it manually, copy or symlink `addons/editor_agent_pane` into your Anki
profile's `addons21` folder, restart Anki, and open Add Cards, Browser, or Edit
Current. Use the `Agent` editor toolbar button, or `Ctrl+Alt+Shift+E`, to show the
pane.

Run `codex login` first and choose ChatGPT sign-in. The add-on shells out to
`codex exec`, so it uses your Codex CLI account instead of a direct OpenAI API
key.

The selected project folder is passed to Codex in read-only mode. Press Enter to
send and Shift+Enter for a newline. Proposed note edits are shown as a diff and
require explicit approval.
