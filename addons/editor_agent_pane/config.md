# Editor Agent Pane

This prototype adds an editor-side chat pane backed by the Codex CLI. Sign in to
Codex with ChatGPT to use your ChatGPT Codex subscription entitlement instead of
direct OpenAI API billing.

- `codex_path`: Optional path to the Codex CLI. Leave blank to use `/Applications/Codex.app/Contents/Resources/codex` when present, otherwise `codex` from `PATH`.
- `model`: Optional Codex model override selected from the Model pulldown. `Codex default` stores an empty value and uses Codex's default for your signed-in account.
- `project_folder`: Optional source folder for the agent.
- `project_folder_access`: Project folder sandbox mode. `workspace-write` lets
  the agent edit files in the selected folder; `read-only` keeps it inspect-only.
- `recent_project_folders`: Recently used source folders shown in the project folder pulldown.
- `timeout_seconds`: Maximum time to wait for a Codex CLI response.
- `splitter_sizes`: Saved rich agent surface/prompt pane sizes.

Run `codex login` first and choose ChatGPT sign-in. The add-on invokes
`codex exec --json` with the selected project folder access, streams live
activity while it is running, then compacts that activity when the final response
arrives. Assistant responses and proposal previews render as sanitized HTML with
MathJax support. Anki never applies note changes without your approval.

To smoke-test the real CLI integration manually, run the editor agent pane tests
with `ANKI_CODEX_CLI_INTEGRATION=1`. Optionally set `ANKI_CODEX_CLI_PATH` to a
specific Codex binary.
