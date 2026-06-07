# Editor Agent Pane

This prototype adds an editor-side chat pane backed by the Codex CLI. Sign in to
Codex with ChatGPT to use your ChatGPT Codex subscription entitlement instead of
direct OpenAI API billing.

- `provider`: Agent backend selected in the Provider pulldown. `codex` uses the Codex CLI; `ollama` uses a local Ollama model.
- `codex_path`: Optional path to the Codex CLI. Leave blank to use `/Applications/Codex.app/Contents/Resources/codex` when present, otherwise `codex` from `PATH`.
- `codex_model`: Optional Codex model override selected from the Model pulldown. `Codex default` stores an empty value and uses Codex's default for your signed-in account.
- `ollama_path`: Optional path to the Ollama CLI. Leave blank to use `ollama` from `PATH`, then common macOS Ollama install locations.
- `ollama_host`: Ollama host used for model discovery and as `OLLAMA_HOST` for `ollama run`. Defaults to `http://127.0.0.1:11434`.
- `ollama_model`: Local Ollama model selected from discovered models. The pane discovers installed models from `/api/tags`, falling back to `ollama list`.
- `reasoning_effort`: Optional Codex reasoning effort override selected from
  the Effort pulldown. `Codex default` stores an empty value and uses your
  normal Codex CLI reasoning effort setting. The pane does not offer
  `minimal`, because Codex rejects it when default hosted tools are available.
- `custom_instructions`: Legacy/default instructions fallback inserted into the agent prompt when the selected provider/model does not have scoped instructions.
- `custom_instructions_by_model`: Instructions saved per provider and model. The map is keyed first by provider (`codex` or `ollama`), then by the selected model value; an empty model key stores the provider's default model choice.
- `project_folder`: Optional source folder for the agent. The pane stores an
  empty value when "Don't work in a folder" is selected.
- `project_folder_access`: Project folder sandbox mode. `workspace-write` lets
  the agent edit files in the selected folder; `read-only` keeps it inspect-only.
- `recent_project_folders`: Recently used source folders shown in the project folder pulldown.
- `fast_mode`: Whether the pane forces Codex CLI Fast mode for runs by passing
  `features.fast_mode=true` and `service_tier="fast"`. Leave off to preserve
  your normal Codex CLI speed setting.
- `stream_reasoning_summaries`: Whether the pane asks Codex for reasoning
  summaries and shows summary events in the activity stream. This only uses
  summary fields from the JSON stream; hidden chain-of-thought/private
  scratchpad content is not displayed.
- `timeout_seconds`: Maximum time to wait for a Codex CLI response.
- `splitter_sizes`: Saved rich agent surface/prompt pane sizes.

For Codex, run `codex login` first and choose ChatGPT sign-in. The add-on invokes
`codex exec --json` with the selected project folder access and your normal
Codex CLI auth, config, and skills. The pane streams live activity while Codex
is running, including tool, web-search, status, and safe event metadata when
Codex provides it, then compacts that activity when the final response arrives.
Reasoning display is still limited to the concise summary fields controlled by
`stream_reasoning_summaries`; hidden chain-of-thought and private scratchpad
content are not displayed. Assistant responses and proposal previews render as
sanitized HTML with MathJax support. Anki never applies note changes without
your approval.

For Ollama, install/start Ollama locally and select the Ollama provider. The
pane invokes `ollama run MODEL --format json --hidethinking --nowordwrap`.
Ollama runs use only the provided Anki editor context; they do not get Codex
tooling, web access, or project folder access.

To smoke-test the real CLI integration manually, run the editor agent pane tests
with `ANKI_CODEX_CLI_INTEGRATION=1`. Optionally set `ANKI_CODEX_CLI_PATH` to a
specific Codex binary.
