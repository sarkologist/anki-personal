# Chinese Frontier configuration

Use **Tools → Chinese Frontier** to choose a deck, pause or enable automation, and
switch between Codex and Claude. The advanced settings below are available by
editing the add-on's `meta.json` configuration, or the defaults before packaging.

- `provider`: `codex` (default) or `claude`. No cross-provider automatic fallback.
- `codex_path`, `claude_path`: optional executable paths; common macOS locations
  are discovered automatically even when Anki's PATH lacks the CLI.
- `codex_model`, `claude_model`: optional model name. Empty uses the CLI's built-in
  default; Codex user config is intentionally not loaded.
- `timeout_seconds`: per generation or language-review request; default 240.
- `batch_size`: 1–12 targets per batch; default 4.
- `minimum_success_days`: Good/Easy study days since the last Again required for
  a context word; default 2, minimum 1.
- `poll_seconds`: periodic rescan; default 300, minimum 30.
- `auto_fill`: true by default; refill unused native new-card slots automatically.
- `max_batches_per_cycle`: maximum batches before cooldown; default 32, range 1–128.
  The actual allowance adapts to remaining capacity and observed approval yield.
- `refill_retry_seconds`: same-day cycle cooldown; default 1800, minimum 30.
- `lookahead_days`: future days of new cards kept generated, so a day's study
  never waits on generation; default 2, range 0–7. With 10 new cards/day, 2
  keeps up to 30 unseen generated cards, topped up as they are introduced.
  Anki's daily new limit still decides how many are shown each day; only
  today's share is checked against the review limit. Shared by French.
- `max_batches_per_day`: default 8, used only when `auto_fill` is false. Failed
  attempts count. Auto-fill uses bounded cycles instead of a fixed daily cutoff.
- `known_words`: optional whole Chinese words explicitly declared familiar. Empty
  by default. These are overrides, not automatically inferred common vocabulary.
- `front_field`, `back_field`: source Basic fields; defaults `Front` and `Back`.
- `initial_deck_name`, `initial_collection_path`: optional first-install bootstrap.
  Both are required, and existing collection settings always take precedence.
- `initial_source_deck_names`, `initial_vocabulary_deck_names`: one-time extra scope
  bootstrap for the exact collection above. Existing selections (including empty
  lists) and pause settings are preserved. The installer exposes repeatable
  `--source-deck` and `--vocabulary-deck` options.

Collection configuration key `chineseFrontier` contains `enabled`, `deck_id`, and
optional `source_deck_ids` and `vocabulary_deck_ids`. Extra source replacements go
to `deck_id` and share its daily limit. Extra sources supply targets only. Context
evidence comes from `deck_id` and the explicitly selected `vocabulary_deck_ids`.
All scopes include descendant decks. These settings sync; CLI paths and the provider are
desktop-local. Pause before undoing a batch to prevent automatic regeneration.

French settings live in the `french` object. `provider`, `codex_model`,
`claude_model`, CLI paths, `batch_size`, `timeout_seconds`,
`minimum_success_days` and `poll_seconds` override their
shared defaults for French only. Tools → French Frontier saves its own collection
scope. Installer-only `initial_french_collection_path` and
`initial_french_deck_name` initialize the exact authorized profile once.
See [French Frontier](french/README.md) for evidence, morphology and source details.

French `auto_fill` defaults to true. `max_batches_per_cycle` (32) bounds each
refill cycle, with automatic same-day retries after `refill_retry_seconds` (1800).
The allowance adapts to remaining native slots and observed approval yield.
`max_batches_per_day` (32) applies to French only when `auto_fill` is false.
Chinese uses the same adaptive refill policy with its own persisted counters.
No retry changes quality requirements.

- French `minimum_vocabulary_level`: 0 estimates target difficulty from reviewed
  content words; 1–6 explicitly select an A1–C2 vocabulary floor. Already covered
  targets remain excluded, and context evidence/quality checks are unchanged.
