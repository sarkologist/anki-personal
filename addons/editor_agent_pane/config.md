# Editor Agent Pane

This prototype adds an editor-side chat pane backed by the OpenAI Responses API.

- `model`: OpenAI model name. The default is a high-quality agentic model, but you can change it if your account uses a different model.
- `project_folder`: Optional read-only source folder for the agent.
- `max_source_file_bytes`: Maximum bytes read from a single source file.
- `max_source_search_results`: Maximum source-search hits returned to the agent.
- `max_source_files_scanned`: Maximum number of files scanned per search.

The add-on does not persist API keys. Set `OPENAI_API_KEY` in the environment, or enter a key in the pane for the current Anki session.
