# Editor Agent Pane

Repo-local Anki add-on prototype for an editor-side OpenAI chat pane.

To try it manually, copy or symlink `addons/editor_agent_pane` into your Anki
profile's `addons21` folder, restart Anki, and open Add Cards, Browser, or Edit
Current. Use the `Agent` editor toolbar button, or `Ctrl+Alt+Shift+E`, to show the
pane.

The add-on reads `OPENAI_API_KEY` from the environment. You can also enter an
API key into the pane for the current session. The key is not written to config.

The selected project folder is read-only and limited to text/code/markdown-like
files. Proposed note edits are shown as a diff and require explicit approval.
