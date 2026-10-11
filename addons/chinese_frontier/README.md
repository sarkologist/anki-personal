# Chinese Frontier

An Anki desktop add-on that gradually replaces suspended Chinese word notes with
one-word sentence clozes. It runs automatically while desktop Anki is open. Original
notes stay suspended and intact; replacements are linked through their `Source`
field and `chinese-frontier::source::*` tags. Newly introduced sentence cards have
their own scheduling history, including when the original was a suspended review.

## Installation

Run `python tools/package_chinese_frontier.py` from this repository, then install
`out/addons/chinese_frontier.ankiaddon` through Anki's Add-ons dialog and restart.
Choose **Tools → Chinese Frontier**, select the source deck, and Save. There are no
daily approvals, tagging steps, or manual unsuspensions. That dialog also pauses
conversion, switches providers, and displays progress and deferred targets.

For an automatic, profile-specific installation:

```sh
python tools/package_chinese_frontier.py \
  --install-to "/path/to/Anki2/addons21" \
  --deck chinese \
  --collection "/path/to/Anki2/User 1/collection.anki2"
```

The installer preserves existing configuration and `user_files`. Initial deck
configuration applies only to that exact collection and never overrides saved
settings, including a paused converter. Desktop Anki must be open to generate and
admit cards. Once synced, sentence cards work on ordinary Anki clients.

## Subscription generation

Codex is the default. Run `codex login` and choose ChatGPT sign-in. Generation uses
`codex exec --output-schema`, not the OpenAI API. Claude uses `claude -p
--json-schema`; sign in to the Claude Code CLI first. Both consume your existing
subscription allowances. There is no automatic fallback to a paid API or to the
other provider. API-key environment variables are removed from child processes.

The CLI receives target definitions and an index of established Chinese words and
their meanings. Generation and an independent language-review pass happen in
temporary directories with no collection access. Claude tools and MCP servers are
disabled. Codex ignores user configuration, disables shell, apps, multi-agent and
web-search features, and uses a read-only sandbox. Optional model settings select
the CLI model; a blank model uses the CLI's built-in default, not custom user config.
Closing the profile or changing settings cancels generation. Account, timeout, and
quota errors retain prepared drafts and back off rather than block studying.

## Vocabulary and validation

By default, a context word needs a live card and Good/Easy on two separate Anki
study days since its last Again. Buried cards are still eligible; unseen and
suspended cards are not. Only the explicitly tested Front or live cloze ordinal
provides evidence. Context text, other cloze siblings, and Extra do not establish
words. Existing cloze text and Extra can provide meaning metadata only.

Basic note sources need an all-Chinese single word in Front, a nonempty Back, and
all sibling cards suspended. Leech and `chinese-frontier::exclude` notes are skipped.
Other field names are configurable. The chosen deck includes subdecks. Optional
`vocabulary_deck_ids` in collection settings can include other Chinese decks.

On first use, the add-on downloads and caches CC-CEDICT headwords from
[MDBG](https://www.mdbg.net/chinese/dictionary?page=cc-cedict). CC-CEDICT is licensed
under [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The cached
headword list is derived from CC-CEDICT and carries that license; code is AGPLv3.
No dictionary data is bundled. A download failure defers generation; it never
silently replaces word segmentation with character matching.

Dictionary maximum matching is deliberately conservative: knowing 学 and 生 does
not establish 学生. Ambiguous segmentation can reject a valid sentence. The
generator tries alternatives instead of relaxing vocabulary checks. Both vocabulary
and source contents are rechecked before admission. Context senses, naturalness,
grammar, translation, and pinyin receive a separate model review, but remain model
judgments rather than guarantees. Prefer simple everyday constructions. Unknown
targets are deferred with backoff and retried when vocabulary changes.

## Daily limits and recovery

Each replacement produces exactly one new `c1` card, clozing the whole target.
Pronunciation and full translation appear only on the answer side. Pending new
cards (including buried siblings), today's introductions, and due reviews reserve
admission capacity. The add-on reads preset, deck, and today-only limits, and
honors the review-limit switch and Anki's study-day boundary. All ancestor budgets
are reserved conservatively, even when Anki permits studying a child independently.
Filtered cards and sibling burying may therefore cause under-admission. Native Anki
scheduling remains the final authority over which admitted cards are shown.

Retries and profile reopenings do not add duplicate replacements. Linked keys live
in synced note tags, while prepared drafts, failures, and usage counters live in
profile-specific `user_files` JSON. Admission is a native collection transaction
with a grouped undo entry. Original cards and their history are never edited.
Missed days do not accumulate extra introductions. Use only one desktop generator
per collection; concurrent unsynced writers cannot share an admission reservation.

During review, right-click a generated card and choose **Replace this sentence
automatically**. The add-on regenerates that note in place, preserving its card ID,
review history, and scheduling. This consumes no new-card slot. Pausing in Tools
also pauses requested repairs. Restoring the old source is as simple as suspending
the replacement and unsuspending its original; the linked replacement is not
automatically recreated. To undo a conversion permanently, pause automation first.

Generation is bounded to eight batches of up to four targets per Anki day by
default, with up to three proposed sentences per target. It does not make CLI calls
when daily capacity is unavailable. Repairs can run without new-card capacity.
The add-on rescans on startup, after sync or review changes, and every five minutes.
Routine runs stay quiet; persistent errors appear once and in the status dialog.

## Verification

```sh
PYTHONPATH=pylib:out/pylib:out/qt out/pyenv/bin/python -m pytest \
  qt/tests/test_chinese_frontier.py
```

Tests use isolated native Anki collections and cover ordinal-specific vocabulary,
word segmentation, new-card and review budgets, source changes during generation,
idempotency, transaction rollback, undo, and schedule-preserving sentence repair.
