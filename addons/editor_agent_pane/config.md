# Editor Agent Pane

This prototype adds an editor-side chat pane backed by the Codex CLI. Sign in to
Codex with ChatGPT to use your ChatGPT Codex subscription entitlement instead of
direct OpenAI API billing.

- `codex_path`: Optional path to the Codex CLI. Leave blank to use `/Applications/Codex.app/Contents/Resources/codex` when present, otherwise `codex` from `PATH`.
- `model`: Optional Codex model override. Leave blank to use Codex's default for your signed-in account.
- `project_folder`: Optional read-only source folder for the agent.
- `timeout_seconds`: Maximum time to wait for a Codex CLI response.
- `splitter_sizes`: Saved transcript/proposal/prompt pane sizes.

Run `codex login` first and choose ChatGPT sign-in. The add-on invokes
`codex exec --json` with a read-only sandbox, streams live activity while it is
running, then compacts that activity when the final response arrives. Anki never
applies note changes without your approval.

To smoke-test the real CLI integration manually, run the editor agent pane tests
with `ANKI_CODEX_CLI_INTEGRATION=1`. Optionally set `ANKI_CODEX_CLI_PATH` to a
specific Codex binary.
