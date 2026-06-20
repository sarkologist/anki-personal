# Editor Agent Pane

Repo-local Anki add-on prototype for an editor-side Codex chat pane.

To try it manually, copy or symlink `addons/editor_agent_pane` into your Anki
profile's `addons21` folder, restart Anki, and open Add Cards, Browser, or Edit
Current. Use the `Agent` editor toolbar button, or `Ctrl+Alt+Shift+E`, to show the
pane.

For Codex, run `codex login` first and choose ChatGPT sign-in. The add-on shells
out to `codex exec`, so it uses your Codex CLI account instead of a direct
OpenAI API key.

For local models, choose the Ollama provider. The pane discovers installed
models from local Ollama, then shells out to
`ollama run MODEL --format json --hidethinking --nowordwrap`.

For Claude, choose the Claude provider. Sign in with the Claude Code CLI first
(run `claude` once to log in, or `claude setup-token`). The pane shells out to
`claude -p`, so it uses your Claude CLI account. Like Codex, Claude can work in
an optional project folder; with no folder selected it answers from the card
context alone.

The selected project folder is writable by default; use the Access menu to switch
it to read-only mode, or choose "Don't work in a folder" to use card context only.
Add optional custom instructions in the pane to steer the agent while keeping the
fixed JSON response and patch safety rules in place. Press Enter to send and
Shift+Enter for a newline. Proposed note edits are shown as a diff and require
explicit approval.
